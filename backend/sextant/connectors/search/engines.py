"""Web and document search through official search APIs (or your own SearXNG). Result pages of search
engines are never scraped; without a configured provider the UI offers one-click manual search links.

All providers share one pipeline: generated queries → results → canonical-URL dedupe → categorisation →
"which of the target's identifiers does this result contain?" → website/document entities (plus
accounts when a result is a known profile URL).
"""

from __future__ import annotations

import abc
import re
from urllib.parse import quote_plus, urlsplit

from ...discovery.profiles import handle_name_overlap, parse_profile, title_identity
from ...discovery.queries import HANDLE_CAP, PERSON_CAP, PlannedQuery, plan_handle, plan_person
from ...registry.platforms import user_platforms
from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NeedsCredentials, SourceUnavailable
from ...sdk.http import FetchError
from ...sdk.errors import TransformError
from ...util.categorize import categorize_url
from ...util.extract import SeedMatcher
from ...util.normalize import canonical_url, name_tokens, parse_phone, url_host
from .._common import account_key, accepts_named_person, person_names

RESULTS_PER_QUERY = 10
DOC_FILTER = "(filetype:pdf OR filetype:doc OR filetype:docx OR filetype:xls OR filetype:xlsx OR filetype:csv)"


def q(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def person_queries(entity, info) -> list[tuple[str, str, str]]:
    """(query, purpose, kind) for a person: the target gets context from the seeds; others from their record."""
    names = person_names(entity, info)
    if not names:
        return []
    name = names[0]
    out = []
    city = info.locations[0].split(",")[0].strip() if info.locations and entity.is_seed else None
    if city:
        out.append((f"{q(name)} {q(city)}", "Name together with the target's city", "web"))
    out.append((q(name), "Pages mentioning the full name (expect namesakes)", "web"))
    if entity.is_seed:
        for u in list(info.seeds.get("username", ()))[:1]:
            out.append((f"{q(name)} {q(u)}", "Name together with the username", "web"))
        for d in [e.split("@", 1)[1] for e in info.seeds.get("email", ()) if "@" in e][:1]:
            out.append((f"{q(name)} {q('@' + d)}", "Name together with the email domain", "web"))
        out.append((f"{q(name)} (site:linkedin.com/in OR site:xing.com)", "Professional profiles with the name", "web"))
        out.append((f"{q(name)} (site:facebook.com OR site:instagram.com OR site:x.com OR site:tiktok.com)",
                    "Social profiles with the name (results only — the platforms are not queried)", "web"))
    for emp in [str(v) for v in entity.values("employer")][:1]:
        out.append((f"{q(name)} {q(emp)}", "Name together with the employer", "web"))
    for alias in names[1:2]:
        out.append((q(alias), "Pages mentioning an alias", "web"))
    return out


def identifier_queries(entity) -> list[tuple[str, str, str]]:
    if entity.type == "phone":
        info = parse_phone(entity.value)
        out = [(q(info.international if info else entity.value), "Pages with the phone number (international format)", "web")]
        if info and info.national != info.international:
            out.append((q(info.national), "Pages with the phone number (national format)", "web"))
        return out
    if entity.type == "domain":
        return [(f"{q(entity.value)} -site:{entity.value}", "Other sites referencing the domain", "web")]
    if entity.type == "username":
        return [(q(entity.display), "Pages and profiles using the username", "web")]
    return [(q(entity.value), f"Pages containing the exact {entity.type}", "web")]


def document_queries(entity, info) -> list[tuple[str, str, str]]:
    if entity.type == "person":
        names = person_names(entity, info)
        return [(f"{q(names[0])} {DOC_FILTER}", "Documents mentioning the name", "document")] if names else []
    if entity.type == "domain":
        return [(f"site:{entity.value} {DOC_FILTER}", "Documents published on the domain", "document")]
    if entity.type == "phone":
        info2 = parse_phone(entity.value)
        return [(f"{q(info2.international if info2 else entity.value)} {DOC_FILTER}", "Documents with the phone number", "document")]
    return [(f"{q(entity.value)} {DOC_FILTER}", f"Documents containing the {entity.type}", "document")]


def _accepts_discovery(_self, entity, info) -> bool:
    if entity.type == "person":
        return entity.is_seed and bool(person_names(entity, info))
    # handles: ones you typed and ones discovered in results; never name-derived guesses (the platform checks test those)
    return not entity.get("hypothesis") and (entity.is_seed or bool(entity.get("discovered")))


def manual_links(query: str) -> dict:
    e = quote_plus(query)
    return {"google": f"https://www.google.com/search?q={e}", "bing": f"https://www.bing.com/search?q={e}",
            "duckduckgo": f"https://duckduckgo.com/?q={e}", "yandex": f"https://yandex.com/search/?text={e}"}


class SearchConnector(Connector, abc.ABC):
    """Base for search providers; subclasses implement ``fetch``. The engine routes automatic runs to one provider at a
    time (``discovery/router.py``), so the providers below are interchangeable."""

    @abc.abstractmethod
    async def fetch(self, query: str, ctx) -> list[dict]:
        """[{url, title, snippet, date}] for one query, in the provider's ranking order."""

    def _matcher(self, info) -> SeedMatcher:
        phones = {}
        for p in info.seeds.get("phone", ()):
            pi = parse_phone(p)
            phones[p] = pi.search_variants if pi else [p]
        return SeedMatcher(names=list(info.target_names[:3]), emails=list(info.seeds.get("email", ())), phones=phones,
                           usernames=list(info.seeds.get("username", ())), domains=list(info.seeds.get("domain", ())))

    async def _run(self, entity, ctx, plan: list) -> None:
        cap = int(ctx.info.option("search_query_cap", 25))
        results: dict[str, dict] = {}
        errors: list[str] = []
        ran = 0
        for item in plan:
            pq = item if isinstance(item, PlannedQuery) else PlannedQuery(item[0], item[1], item[2], None, 0.0, "")
            if not ctx.take("queries", cap):
                ctx.out.set_status("budget_exhausted", f"Search query budget of {cap} per investigation used up")
                ctx.journal({"provider": self.manifest.name, "query": pq.text, "purpose": pq.purpose, "kind": pq.kind,
                             "platform": pq.platform, "status": "budget_exhausted", "results": 0, "profiles": []})
                break
            try:
                rows = await self.fetch(pq.text, ctx)
                ran += 1
            except (TransformError, FetchError) as exc:
                ctx.journal({"provider": self.manifest.name, "query": pq.text, "purpose": pq.purpose, "kind": pq.kind,
                             "platform": pq.platform, "status": "error", "error": str(exc)[:200], "results": 0,
                             "profiles": []})
                if isinstance(exc, NeedsCredentials):
                    raise
                errors.append(str(exc))
                continue
            profiles = []
            for rank, r in enumerate(rows[:RESULTS_PER_QUERY], start=1):
                c = canonical_url(str(r.get("url") or ""))
                if not c:
                    continue
                cur = results.get(c)
                if cur is None:
                    cur = results[c] = {**r, "canonical": c, "queries": [], "kind": pq.kind, "purpose": pq.purpose,
                                        "rank": rank, "query": pq.text, "query_kind": pq.kind}
                elif rank < cur["rank"]:
                    cur.update(rank=rank, query=pq.text, query_kind=pq.kind, purpose=pq.purpose)
                if pq.text not in cur["queries"]:
                    cur["queries"].append(pq.text)
                hit = parse_profile(str(r.get("url") or ""))
                if hit:
                    profiles.append(f"{hit.platform.id}:{hit.username}")
            ctx.journal({"provider": self.manifest.name, "query": pq.text, "purpose": pq.purpose, "kind": pq.kind,
                         "platform": pq.platform, "status": "ok", "results": len(rows[:RESULTS_PER_QUERY]),
                         "profiles": profiles[:20]})
        if not ran and errors:
            raise SourceUnavailable(errors[0])
        matcher = self._matcher(ctx.info)
        for c, r in results.items():
            self._emit(entity, ctx, c, r, matcher)

    def _emit(self, entity, ctx, canonical: str, r: dict, matcher: SeedMatcher) -> None:
        url, title, snippet = str(r.get("url")), str(r.get("title") or "")[:300], str(r.get("snippet") or "")[:1000]
        category = categorize_url(url, title)
        src = ctx.out.source(f"{self.manifest.website}#q={quote_plus(r['query'])}", title=f"{self.manifest.title} results",
                             kind="api", request=f"query {r['query']}")
        matched = sorted(matcher.match(f"{title} {snippet}", url))
        etype = "document" if category == "document" else "website"
        props = {"url": url, "title": title, "snippet": snippet, "category": category, "host": url_host(url),
                 "published": r.get("date"), "matched": matched, "queries": r["queries"], "why": r.get("purpose"),
                 "search_rank": r["rank"], "search_query": r["query"], "search_provider": self.manifest.name}
        if etype == "document":
            props["file_type"] = urlsplit(url).path.rsplit(".", 1)[-1].lower()[:6]
            props["note"] = "Not downloaded — open it yourself only if you trust the site."
        page = ctx.out.entity(etype, canonical, label=title or url_host(url), source=src, props=props,
                              evidence=f"Search result #{r['rank']} for {r['query']}", confidence=0.9)
        names = person_names(entity, ctx.info) if entity.type == "person" else []
        if entity.type == "person":
            if names:
                nk = ctx.out.entity("name", names[0], source=src)
                ctx.out.link(nk, page, "MENTIONED_IN", source=src, confidence=0.6 if "name" in matched else 0.3,
                             evidence="Result of a search for the name" + ("" if "name" in matched else
                                                                            " (name not visible in the snippet)"))
        else:
            ctx.out.link(entity.key, page, "MENTIONED_IN", source=src,
                         confidence=0.9 if matched else 0.5, evidence="Result of an exact search for this identifier")
        hit = parse_profile(url)
        if hit:
            self._emit_profile(entity, ctx, hit, r, src, page, names or list(ctx.info.target_names[:3]))

    def _emit_profile(self, entity, ctx, hit, r: dict, src: str, page: str | None, names: list[str]) -> None:
        """A result that is a profile becomes an account and a *discovered* handle, with the rank, query and provider
        that surfaced it. The handle comes from the URL, so it does not need to be predictable from the name."""
        platform, uname = hit.platform, hit.username
        title = str(r.get("title") or "")
        ti = title_identity(title, uname)
        overlap = handle_name_overlap(uname, names)
        shown = set(name_tokens(ti.display_name or "")) & {t for n in names for t in name_tokens(n)}
        why = (f"Search result #{r['rank']} from {self.manifest.title} for {r['query']}: {hit.evidence.lower()}"
               + (f"; the title shows “{ti.display_name}”" if ti.display_name else ""))
        acc = ctx.out.entity("account", account_key(platform.id, uname), label=f"{platform.name}: {uname}", source=src,
                             props={"platform": platform.id, "platform_title": platform.name, "username": uname,
                                    "profile_url": hit.profile_url, "found_by": "search_result",
                                    "display_name": ti.display_name, "search_rank": r["rank"],
                                    "search_query": r["query"], "search_provider": self.manifest.name,
                                    "search_result_url": canonical_url(hit.found_url) or hit.found_url,
                                    "discovery_how": hit.how, "title_handle_agrees": ti.handle_agrees,
                                    "handle_name_tokens": overlap or None},
                             evidence=why, confidence=0.9)
        if acc is None:
            return
        ctx.out.link(page, acc, "LINKED_TO", source=src, evidence="The search result is (a page of) this profile")
        # Stage 7–8: the handle is a new identifier. It is pivoted on automatically only when the result came from a
        # search for the target's name and the result itself echoes that name (title or handle) — otherwise it stays a
        # candidate the investigator or the assistant can choose to pivot on.
        name_query = r.get("query_kind") in ("name", "name_platform") or entity.type == "person"
        pivot = bool(name_query and (len(shown) >= 2 or len(overlap) >= 2 or (shown and overlap)))
        uk = ctx.out.entity("username", uname, source=src, confidence=0.85,
                            props={"username": uname, "discovered": True, "discovery": "search_result",
                                   "discovered_on": platform.name, "source_url": hit.profile_url,
                                   "search_rank": r["rank"], "search_query": r["query"],
                                   "search_provider": self.manifest.name, "pivot": pivot or None},
                            evidence=f"Handle in the profile URL of {why[0].lower() + why[1:]}")
        ctx.out.link(acc, uk, "USES", source=src, confidence=0.95,
                     evidence=f"The profile URL {hit.profile_url} carries this handle")
        if ti.display_name and len(name_tokens(ti.display_name)) >= 2:
            nk = ctx.out.entity("name", ti.display_name, source=src)
            ctx.out.link(acc, nk, "HAS_NAME", source=src, confidence=0.8, evidence="Display name in the search-result title")

    # ------------------------------------------------------------------ transforms (inherited by every provider)
    @transform(inputs=("person",), outputs=("website", "document", "account", "name", "username"),
               title="Web search: person", priority=0.6, estimated_requests=6, accepts=accepts_named_person,
               sends="search queries built from the name (+ city/username/email domain of the target)")
    async def person_web(self, entity, ctx):
        await self._run(entity, ctx, person_queries(entity, ctx.info))

    @transform(inputs=("email", "phone", "username", "domain"), outputs=("website", "document", "account", "username"),
               title="Web search: exact identifier", priority=0.6, estimated_requests=2,
               sends="the identifier as an exact-match search query")
    async def identifier_web(self, entity, ctx):
        await self._run(entity, ctx, identifier_queries(entity))

    @transform(inputs=("person", "email", "phone", "domain"), outputs=("document",), title="Document search (filetype)",
               priority=0.4, estimated_requests=1,
               accepts=lambda self, e, info: e.type != "person" or bool(person_names(e, info)),
               sends="filetype-restricted search queries with the identifier")
    async def documents(self, entity, ctx):
        await self._run(entity, ctx, document_queries(entity, ctx.info))

    @transform(inputs=("person", "username"), outputs=("account", "username", "website", "name"),
               title="Profile discovery: name / handle on each platform (web search)", priority=0.85,
               estimated_requests=12, accepts=_accepts_discovery,
               sends="platform-restricted search queries (\"name\" site:platform, \"handle\" site:platform)")
    async def profile_discovery(self, entity, ctx):
        """Stages 4–7 of the identity pipeline: ask the search engine for the target's name (or a discovered handle)
        on each platform, read the profile URLs in the results, and emit the handles they carry."""
        depth = str(ctx.info.option("depth_preset") or "2")
        found = frozenset(str(p) for p in ctx.info.option("platforms_found", ()) or ())
        countries = tuple(ctx.info.countries)
        extra = tuple(user_platforms())
        # a focused request ("check Pinterest and TikTok") from the investigator or the assistant, for this entity only
        only = tuple((ctx.info.options.get("discovery_focus") or {}).pop(entity.key, ()) or ())
        if entity.type == "person":
            cap = int(ctx.info.option("discovery_query_cap") or PERSON_CAP.get(depth, 12))
            plan = plan_person(person_names(entity, ctx.info), countries=countries, found_platforms=found,
                               cap=max(cap, 2 * len(only)), extra=extra, only=only)
        else:
            cap = int(ctx.info.option("discovery_handle_cap") or HANDLE_CAP.get(depth, 4))
            plan = plan_handle(entity.display, countries=countries, found_platforms=found, cap=max(cap, len(only)),
                               extra=extra, only=only)
        if not plan:
            ctx.out.set_status("skipped", "Nothing to search for")
            return
        await self._run(entity, ctx, plan)


def _title_name(title: str) -> str | None:
    for sep in (" (", " - ", " | ", " – ", " on ", " / ", " • "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if 2 <= len(head) <= 60 and len(head.split()) >= 2:
                return head
    return None


def _strip(s) -> str:
    return re.sub(r"<[^>]+>", "", str(s or ""))


@register
class BraveSearch(SearchConnector):
    manifest = Manifest(
        name="brave_search", title="Brave Search API", category="search",
        description="Independent web index via the official Brave Search API.", access="metered",
        credentials=(Credential("BRAVE_API_KEY", "Brave Search API key", "Since Feb 2026: $5 monthly credit (≈1,000 queries), "
                                "then $5 per 1,000.", obtain_url="https://api-dashboard.search.brave.com/"),),
        cost="$5 per 1,000 queries after a $5 monthly credit", cost_per_call_usd=0.005,
        rate_limit=RateLimit(0.9, 1, "1 query/second on the base plan"), website="https://brave.com/search/api/",
        documentation="https://api-dashboard.search.brave.com/app/documentation/web-search/get-started", reliability="low",
        capabilities=("web pages mentioning identifiers", "documents by file type", "profiles in results"),
        disclosure=Disclosure(("api.search.brave.com",), "search queries containing the identifiers"), auto_run=True,
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        (key,) = ctx.require("BRAVE_API_KEY")
        resp = await ctx.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": query, "count": RESULTS_PER_QUERY, "safesearch": "off"},
                             headers={"Accept": "application/json", "X-Subscription-Token": key})
        if resp.status in (401, 403, 422):
            raise NeedsCredentials(f"Brave Search rejected the key (HTTP {resp.status})")
        results = ((ctx.json(ctx.expect(resp)) or {}).get("web") or {}).get("results") or []
        return [{"url": r.get("url"), "title": r.get("title"), "snippet": _strip(r.get("description")),
                 "date": r.get("page_age") or r.get("age")} for r in results if isinstance(r, dict)]


@register
class Searxng(SearchConnector):
    manifest = Manifest(
        name="searxng", title="SearXNG (self-hosted)", category="search",
        description="Meta-search through your own SearXNG instance — the most private option.", access="free_key",
        credentials=(Credential("SEARXNG_URL", "SearXNG URL", "e.g. http://127.0.0.1:8888 — JSON output must be enabled "
                                "(search.formats: [html, json]). See deploy/searxng.", secret=False,
                                obtain_url="https://docs.searxng.org/admin/installation.html"),),
        cost="Free (your own instance)", rate_limit=RateLimit(0.5, 1), website="https://docs.searxng.org",
        documentation="https://docs.searxng.org/dev/search_api.html", reliability="low",
        legal="Your instance queries upstream engines on your behalf; their terms apply to your instance.",
        capabilities=("web pages mentioning identifiers", "documents by file type"),
        disclosure=Disclosure(("your SearXNG instance", "the engines it is configured to use"),
                              "search queries containing the identifiers"),
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        (base,) = ctx.require("SEARXNG_URL")
        resp = await ctx.get(f"{base.rstrip('/')}/search", params={"q": query, "format": "json", "safesearch": "0"},
                             headers={"Accept": "application/json"})
        if resp.status == 403:
            raise SourceUnavailable("SearXNG refused JSON output — enable 'json' in search.formats")
        data = ctx.json(ctx.expect(resp)) or {}
        results = data.get("results") or []
        failed = data.get("unresponsive_engines") or []
        if not results and failed:
            reasons = ", ".join(f"{e[0]} ({e[1]})" for e in failed if isinstance(e, list) and len(e) == 2)
            raise SourceUnavailable(f"SearXNG's engines did not answer: {reasons}")
        return [{"url": r.get("url"), "title": r.get("title"), "snippet": r.get("content"), "date": r.get("publishedDate")}
                for r in results if isinstance(r, dict)]


@register
class GoogleCse(SearchConnector):
    manifest = Manifest(
        name="google_cse", title="Google Programmable Search (JSON API)", category="search",
        description="Google Custom Search JSON API — closed to new customers; Google shuts it down on 2027-01-01.",
        access="metered",
        credentials=(Credential("GOOGLE_CSE_KEY", "Google API key"),
                     Credential("GOOGLE_CSE_CX", "Search engine id (cx)", "Configured to search the entire web.", secret=False)),
        cost="100 queries/day free, then $5 per 1,000 (existing customers)", cost_per_call_usd=0.005,
        rate_limit=RateLimit(1.0, 1), website="https://developers.google.com/custom-search/v1/overview",
        documentation="https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list", reliability="low",
        status="sunsetting", status_note="Discontinued by Google on 2027-01-01; closed to new customers since 2025.",
        capabilities=("web pages mentioning identifiers", "documents by file type"),
        disclosure=Disclosure(("www.googleapis.com",), "search queries containing the identifiers"), auto_run=True,
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        key, cx = ctx.require("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX")
        resp = await ctx.get("https://www.googleapis.com/customsearch/v1", params={"key": key, "cx": cx, "q": query,
                                                                                    "num": RESULTS_PER_QUERY})
        if resp.status in (400, 403):
            raise NeedsCredentials(f"Google CSE rejected the request (HTTP {resp.status}) — key/cx/quota")
        out = []
        for it in (ctx.json(ctx.expect(resp)) or {}).get("items") or []:
            meta = ((it.get("pagemap") or {}).get("metatags") or [{}])[0]
            out.append({"url": it.get("link"), "title": it.get("title"), "snippet": it.get("snippet"),
                        "date": meta.get("article:published_time")})
        return out


@register
class SerpApi(SearchConnector):
    manifest = Manifest(
        name="serpapi", title="SerpApi", category="search",
        description="Commercial search-results API (Google, Bing and others) with your own subscription.", access="paid",
        credentials=(Credential("SERPAPI_KEY", "SerpApi key", obtain_url="https://serpapi.com/manage-api-key"),),
        cost="Paid plans (free trial: 100 searches/month)", cost_per_call_usd=0.01,
        rate_limit=RateLimit(1.0, 1), website="https://serpapi.com", documentation="https://serpapi.com/search-api",
        reliability="low", capabilities=("web pages mentioning identifiers", "documents by file type"),
        disclosure=Disclosure(("serpapi.com",), "search queries containing the identifiers"),
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        (key,) = ctx.require("SERPAPI_KEY")
        resp = await ctx.get("https://serpapi.com/search.json", params={"q": query, "api_key": key, "engine": "google",
                                                                        "num": RESULTS_PER_QUERY})
        if resp.status == 401:
            raise NeedsCredentials("SerpApi rejected the key")
        return [{"url": r.get("link"), "title": r.get("title"), "snippet": r.get("snippet"), "date": r.get("date")}
                for r in (ctx.json(ctx.expect(resp)) or {}).get("organic_results") or []]


@register
class Serper(SearchConnector):
    manifest = Manifest(
        name="serper", title="Serper (Google results API)", category="search",
        description="Google web results through the Serper.dev API, with your own key.", access="metered",
        credentials=(Credential("SERPER_API_KEY", "Serper API key", "Prepaid query credits; a free trial allowance exists.",
                                obtain_url="https://serper.dev/api-key"),),
        cost="Prepaid credits per query (set the price you pay in Settings → Pricing)",
        rate_limit=RateLimit(2.0, 2), website="https://serper.dev", documentation="https://serper.dev/playground",
        reliability="low", capabilities=("web pages mentioning identifiers", "profiles in results", "documents by file type"),
        disclosure=Disclosure(("google.serper.dev",), "search queries containing the identifiers"), auto_run=True,
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        import json as _json
        (key,) = ctx.require("SERPER_API_KEY")
        resp = await ctx.post("https://google.serper.dev/search", headers={"X-API-KEY": key, "Content-Type": "application/json"},
                              content=_json.dumps({"q": query, "num": RESULTS_PER_QUERY}).encode())
        if resp.status in (401, 403):
            raise NeedsCredentials(f"Serper rejected the key (HTTP {resp.status})")
        rows = (ctx.json(ctx.expect(resp)) or {}).get("organic") or []
        rows = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: r.get("position") or 99)
        return [{"url": r.get("link"), "title": r.get("title"), "snippet": r.get("snippet"), "date": r.get("date")}
                for r in rows]


@register
class Tavily(SearchConnector):
    manifest = Manifest(
        name="tavily", title="Tavily Search API", category="search",
        description="Web search API built for AI agents, with your own key.", access="metered",
        credentials=(Credential("TAVILY_API_KEY", "Tavily API key", "Monthly free credits, then pay-as-you-go.",
                                obtain_url="https://app.tavily.com/"),),
        cost="Credits per search (set the price you pay in Settings → Pricing)",
        rate_limit=RateLimit(2.0, 2), website="https://tavily.com", documentation="https://docs.tavily.com",
        reliability="low", capabilities=("web pages mentioning identifiers", "profiles in results"),
        disclosure=Disclosure(("api.tavily.com",), "search queries containing the identifiers"), auto_run=True,
    )

    async def fetch(self, query: str, ctx) -> list[dict]:
        import json as _json
        (key,) = ctx.require("TAVILY_API_KEY")
        resp = await ctx.post("https://api.tavily.com/search",
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              content=_json.dumps({"query": query, "max_results": RESULTS_PER_QUERY,
                                                   "search_depth": "basic"}).encode())
        if resp.status in (401, 403):
            raise NeedsCredentials(f"Tavily rejected the key (HTTP {resp.status})")
        rows = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
        return [{"url": r.get("url"), "title": r.get("title"), "snippet": r.get("content"), "date": r.get("published_date")}
                for r in rows if isinstance(r, dict)]
