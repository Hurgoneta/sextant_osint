"""Finding and describing new catalog sources: inspect one address, or discover candidates through web search.

Both produce *proposals* for the canonical catalog (``registry/catalog.py``), never records: the investigator (or the
assistant, with the investigator's confirmation) saves them through the same validation as the Add-source form. Every
classification says which signal it came from ("title contains 'domstol'"), every candidate carries the query, rank
and provider that surfaced it, and everything is "Not verified" until someone checks it. Nothing is invented: a
candidate is a search result, and a field the page does not support is left empty.

* ``inspect_url``: one GET of the address through the guarded client (no cookies, size cap, no private addresses),
  then title, description, site name, language, ccTLD → jurisdiction, category, official/commercial/API hints, and the
  duplicate check against connectors, official sources, platforms and your own sources.
* ``discover``: coverage gaps of a jurisdiction (the audit) + the request → a few queries (never dozens) through the
  primary web-search provider → candidates deduplicated by site, classified, ranked, minus everything already in the
  catalog.
"""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlsplit, urlunsplit

from . import catalog as CAT
from . import connectors as REG
from . import countries as C
from . import jurisdictions as J
from . import platforms as P
from ..model import utcnow

MAX_QUERIES = 4
LAST_KEY = "catalog_discovery:last"

# (category, official kind, keywords) — matched on folded title/description/URL text; first match wins per category
HINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("legal_records", "judgments", ("case law", "judgments", "judgements", "rattsfall", "rättsfall", "lagrummet",
                                    "jurisprudence", "jurisprudencia", "rechtsprechung", "precedent")),
    ("legal_records", "court", ("court", "courts", "tribunal", "domstol", "domstolar", "gericht", "juzgado", "tribunale",
                                "sad ", "instanta", "rechtbank", "oikeus", "hovratt", "hovrätt", "tingsratt", "tingsrätt",
                                "supreme court", "hogsta domstolen", "högsta domstolen")),
    ("legal_records", "bar_association", ("bar association", "advokatsamfundet", "law society", "lawyer register")),
    ("companies", "company_registry", ("company register", "companies register", "business register", "companies house",
                                       "handelsregister", "bolagsverket", "registro mercantil", "registre du commerce",
                                       "trade register", "company search", "foretagsregister", "företagsregister", "onrc",
                                       "brreg", "kvk")),
    ("public_records", "gazette", ("gazette", "official journal", "kungorelse", "kungörelse", "monitorul oficial",
                                   "bundesanzeiger", "post- och inrikes tidningar")),
    ("public_records", "insolvency", ("insolvency", "bankruptcy", "konkurs")),
    ("public_records", "property", ("land registry", "property register", "lantmateriet", "lantmäteriet", "cadastre")),
    ("sanctions", "sanctions", ("sanctions list", "sanctions")),
    ("procurement", "procurement", ("procurement", "tenders", "upphandling")),
    ("documents", "legislation", ("legislation", "statutes", "lagstiftning", "svensk forfattningssamling",
                                  "svensk författningssamling", "official laws")),
    ("b2b_sales_intelligence", "commercial_provider", ("b2b", "sales intelligence", "lead generation", "prospecting",
                                                       "go-to-market", "sales leads", "buyer intent")),
    ("contact_enrichment", "commercial_provider", ("data enrichment", "contact enrichment", "email finder",
                                                   "phone finder", "enrich contacts")),
    ("business_contact_data", "commercial_provider", ("business contact", "contact data", "contact database",
                                                      "decision makers", "b2b contacts")),
    ("people_search", "people_directory", ("people search", "find people", "background check", "reverse phone",
                                           "person search", "people finder", "public records search")),
    ("identity_resolution", "commercial_provider", ("identity resolution", "identity verification", "kyc",
                                                    "identity graph")),
    ("company_intelligence", "commercial_provider", ("firmographics", "company data", "company information",
                                                     "credit report", "business information", "company intelligence")),
    ("consumer_data", "commercial_provider", ("consumer data", "audience data", "data broker", "consumer insights")),
    ("marketing_data", "commercial_provider", ("marketing data", "audience segments", "data marketplace")),
    ("historical_data", "commercial_provider", ("historical records", "archive data", "historical data")),
    ("news", "news_media", ("newspaper", "news agency", "nyheter")),
)
COMMERCIAL_WORDS = ("pricing", "plans", "subscription", "free trial", "book a demo", "request a demo", "get a demo",
                    "per month", "credits", "enterprise", "start for free", "sign up free")
API_WORDS = ("api", "developer", "developers", "rest endpoint", "sdk", "openapi", "swagger")
GOV_WORDS = ("government", "ministry", "agency", "authority", "myndighet", "regeringen", "riksdag", "official website",
             "officiell", "statlig", "public authority")
# results on these hosts talk *about* sources; they are not the source
SKIP_HOSTS = ("wikipedia.org", "linkedin.com", "facebook.com", "youtube.com", "reddit.com", "quora.com", "medium.com",
              "x.com", "twitter.com", "instagram.com", "g2.com", "capterra.com", "trustpilot.com", "crunchbase.com",
              "github.com", "tiktok.com", "pinterest.com", "glassdoor.com", "indeed.com", "amazon.com", "google.com",
              "bing.com", "duckduckgo.com", "yelp.com")
GENERIC_TLDS = {".com", ".org", ".net", ".io", ".ai", ".co", ".info", ".biz", ".app", ".dev", ".eu"}
CATEGORY_QUERIES = {
    "legal_records": "court decisions case law database", "companies": "official company register search",
    "public_records": "official public records register", "sanctions": "national sanctions list",
    "procurement": "public procurement tenders portal", "documents": "official legislation database",
    "people_search": "people search directory", "b2b_sales_intelligence": "B2B contact data provider",
    "business_contact_data": "business contact database provider", "contact_enrichment": "contact data enrichment API",
    "company_intelligence": "company information credit data provider", "identity_resolution": "identity verification data provider",
    "consumer_data": "consumer data provider", "news": "news archive",
}
GROUP_QUERIES = {"judicial": "court decisions database", "corporate": "company register", "public_records": "public records",
                 "regulatory": "regulator register", "procurement": "public procurement portal",
                 "commercial": "company and contact data provider", "professional": "professional register"}


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).casefold()


def classify(text: str, url: str, jurisdiction: str = "", category_hint: str = "") -> dict:
    """Category, kind, nature (official / commercial / public), API presence and jurisdiction from a page's words and
    address, each with the signal that produced it."""
    t = _fold(f" {text} {url} ")
    raw = (text or "").casefold()
    signals: list[str] = []
    host = (urlsplit(url).hostname or "").lower()
    category, kind = "", ""
    for cat, k, words in HINTS:
        w = next((w for w in words if _fold(w) in t), None)
        if w:
            category, kind = cat, k
            signals.append(f"“{w}” in the title, description or address → {cat.replace('_', ' ')}")
            break
    if category_hint and not category:
        category = category_hint
        signals.append(f"category taken from your request ({category_hint})")
    tld = "." + host.rsplit(".", 1)[-1] if "." in host else ""
    country = None
    if jurisdiction:
        country = jurisdiction.upper()
        signals.append(f"jurisdiction from your request ({country})")
    elif tld and tld not in GENERIC_TLDS:
        c = C.by_tld(tld)
        if c is not None:
            country = c.code
            signals.append(f"country-code domain {tld} → {c.name}")
    gov_host = bool(re.search(r"(^|\.)(gov|gouv|gob|govt|go|gv|admin|bund|overheid|government)\.", host + ".")) or \
        host.endswith((".gov", ".mil", ".europa.eu", ".int"))
    gov_word = next((w for w in GOV_WORDS if w in raw), None)
    official = gov_host or (bool(gov_word) and kind not in ("commercial_provider", "people_directory"))
    if gov_host:
        signals.append(f"government domain ({host})")
    elif official:
        signals.append(f"“{gov_word}” on the page (not a government domain: check it)")
    com_word = next((w for w in COMMERCIAL_WORDS if w in raw), None)
    commercial = bool(com_word) or kind == "commercial_provider" or category in COMMERCIAL_CATEGORIES
    if com_word:
        signals.append(f"“{com_word}” on the page → commercial offering")
    api_word = next((w for w in API_WORDS if re.search(rf"\b{re.escape(w)}\b", raw)), None)
    if api_word:
        signals.append(f"“{api_word}” on the page → an API may exist (not verified)")
    if official and not commercial:
        nature = "official"
    elif commercial:
        nature = "commercial"
    else:
        nature = "public"
    source_type = "official_api" if api_word and (official or commercial) else "manual" if (official or commercial) \
        else "reference"
    access = "paid" if commercial else ("free_api" if api_word and official else "free_web" if official else "unknown")
    return {"category": category or "public_records", "category_guessed": not category, "kind": kind if official or
            kind in ("commercial_provider", "people_directory") else "", "jurisdiction": country, "official": official,
            "commercial": commercial, "api": bool(api_word), "nature": nature, "source_type": source_type,
            "access": access, "provider_class": "commercial" if commercial else "public", "signals": signals}


COMMERCIAL_CATEGORIES = frozenset({"people_search", "b2b_sales_intelligence", "business_contact_data", "consumer_data",
                                   "identity_resolution", "professional_intelligence", "company_intelligence",
                                   "public_records_aggregation", "contact_enrichment", "marketing_data", "historical_data"})


def _title(raw: str, host: str) -> str:
    t = html.unescape(re.sub(r"\s+", " ", raw or "")).strip()
    for sep in (" | ", " – ", " — ", " - ", " · ", " :: "):
        parts = [p.strip() for p in t.split(sep) if p.strip()]
        if len(parts) > 1:
            # the site name is usually the shorter end of "Page – Site"
            t = min((parts[0], parts[-1]), key=len) if len(parts[-1]) >= 3 else parts[0]
            break
    return (t or host)[:200]


def draft(url: str, title: str, description: str, cls: dict, *, evidence: str, extra_notes: str = "") -> dict:
    """A source definition in the shape of the Add-source form (CatalogSourceInput)."""
    juris = [cls["jurisdiction"]] if cls.get("jurisdiction") and cls["jurisdiction"] in CAT.jurisdiction_codes() else []
    return {"title": title, "url": url, "category": cls["category"], "source_type": cls["source_type"],
            "access": cls["access"], "kind": cls["kind"] if cls["kind"] in J.SOURCE_KINDS else "",
            "jurisdictions": juris, "official": bool(cls["official"]), "description": (description or "")[:600],
            "provider_class": cls["provider_class"], "review": "not_verified",
            "notes": (f"Proposed from {evidence} on {utcnow()[:10]}. Classification signals: "
                      + "; ".join(cls["signals"][:6]) + ". Not verified." + (f" {extra_notes}" if extra_notes else ""))[:2000]}


def _meta(doc: str, name: str) -> str:
    m = re.search(rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)', doc, re.I) or \
        re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']{re.escape(name)}["\']', doc, re.I)
    return html.unescape(m.group(1)).strip()[:600] if m else ""


async def inspect_url(url: str, http, snap: CAT.Snapshot, *, jurisdiction: str = "", category: str = "") -> dict:
    url = CAT.clean_url(url)
    status, error, final, page = None, None, url, ""
    try:
        resp = await http.request("GET", url, connector="catalog_inspect", cache_ttl=None, retries=0, max_bytes=600_000)
        status, final = resp.status, resp.url or url
        if "html" in (resp.headers.get("content-type") or "") or resp.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            page = resp.text[:600_000]
    except Exception as exc:  # unreachable addresses are reported, and the URL alone still gives a draft
        error = f"{exc.__class__.__name__}: {str(exc)[:160]}"
    host = (urlsplit(final).hostname or "").lower()
    title_raw = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if m:
        title_raw = m.group(1)
    site = _meta(page, "og:site_name")
    desc = _meta(page, "description") or _meta(page, "og:description")
    lang = (re.search(r"<html[^>]*\blang=[\"']?([A-Za-z-]{2,10})", page, re.I) or [None, ""])[1]
    api_link = bool(re.search(r'href=["\'][^"\']*(/api\b|developer|/docs/api|swagger|openapi)', page, re.I))
    title = site or _title(title_raw, host)
    cls = classify(f"{title_raw} {site} {desc}" + (" api" if api_link else ""), final, jurisdiction, category)
    d = draft(final if status and status < 400 else url, title, desc, cls, evidence=f"the page at {host}")
    if not cls["jurisdiction"] and lang:
        d["notes"] += f" Page language: {lang}."
    dups = CAT.find_duplicates(d, snap)
    return {"url": url, "final_url": final, "reachable": bool(status and status < 400), "http_status": status,
            "error": error, "title": title, "description": desc, "language": lang, "classification": cls,
            "draft": d, "duplicates": dups, "already_exists": any(x["strength"] == "likely" for x in dups),
            "checked_at": utcnow()}


# --------------------------------------------------------------------------- discovery through web search

async def search_once(provider: str, query: str, cfg, http) -> list[dict]:
    """One query through a configured search provider outside an investigation (same code path as the connectors)."""
    from ..sdk.context import TransformContext
    from ..sdk.http import limiter_for
    from ..sdk.result import TransformResult
    from ..sdk.transform import EntityView, InvestigationInfo
    conn = REG.REGISTRY[provider]
    m = conn.manifest
    ent = EntityView(key="website:catalog-discovery", type="website", value="catalog-discovery", label="catalog discovery",
                     status="seed", props={})
    ctx = TransformContext(transform=f"{provider}.catalog_discovery", manifest=m, entity=ent, info=InvestigationInfo(),
                           http=http, limiter=limiter_for(m.name, m.rate_limit.per_second, m.rate_limit.burst),
                           credentials=cfg.credential, settings=cfg.get,
                           out=TransformResult(f"{provider}.catalog_discovery", provider, ent.key, ent.type, ent.label),
                           run_id="catalog")
    return await conn.fetch(query, ctx)


def plan_queries(request: str, jurisdiction: str, category: str, gaps: list[str]) -> list[str]:
    cname = C.name(jurisdiction) if jurisdiction and C.get(jurisdiction) else ""
    out: list[str] = []
    req = " ".join((request or "").split())[:200]
    if req:
        out.append(req if not cname or cname.casefold() in req.casefold() else f"{req} {cname}")
    if category and category in CATEGORY_QUERIES:
        out.append(f"{cname} {CATEGORY_QUERIES[category]}".strip())
    for g in gaps:
        if g in GROUP_QUERIES:
            out.append(f"{cname} {GROUP_QUERIES[g]} official".strip())
    seen, uniq = set(), []
    for q in out:
        k = q.casefold()
        if q and k not in seen:
            seen.add(k)
            uniq.append(q)
    return uniq[:MAX_QUERIES]


async def discover(*, request: str = "", jurisdiction: str = "", category: str = "", cfg, db, http, snap: CAT.Snapshot,
                   inv_id: str | None = None) -> dict:
    """Candidate sources for a request ("commercial people/contact data providers covering Sweden"), a jurisdiction's
    gaps and/or a category, from the configured web-search provider. Nothing is added."""
    from ..costs import ledger as LEDGER
    from ..costs import pricing as PR
    from ..discovery import router as ROUTER
    code = (jurisdiction or "").upper()
    gaps: list[str] = []
    audit_summary = None
    if code and code in J.jurisdictions():
        from . import catalog_audit as AUDIT
        from . import coverage_model as CM
        a = AUDIT.audit(code, CM.CoverageContext(cfg, db.health_records(), snap), snap, db.get_setting(f"discovery:{code}"))
        weak = {"gap": 0, "potential": 1, "global": 2, "links": 3}
        gaps = [g["group"] for g in sorted((g for g in a.get("coverage", []) if g.get("rating") in weak),
                                           key=lambda g: (not g.get("core"), weak[g["rating"]]))][:3]
        audit_summary = {"coverage": [{"group": g.get("group"), "rating": g.get("rating")} for g in a.get("coverage", [])],
                         "gaps": a.get("gaps")}
    queries = plan_queries(request, code, category, gaps)
    providers = ROUTER.available(cfg)
    if not queries:
        return {"ok": False, "message": "Say what to look for, or pick a jurisdiction or category.", "candidates": []}
    if not providers:
        return {"ok": False, "message": ROUTER.unavailable_message(cfg), "queries": queries, "candidates": [],
                "audit": audit_summary}
    provider = providers[0]
    rows: dict[str, dict] = {}
    errors, ran = [], 0
    for q in queries:
        try:
            results = await search_once(provider, q, cfg, http)
            ran += 1
        except Exception as exc:
            errors.append(f"{q}: {str(exc)[:160]}")
            continue
        for rank, r in enumerate(results[:10], start=1):
            url = str(r.get("url") or "")
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS) or P.platform_for_host(host):
                continue
            cur = rows.get(host)
            if cur is None or rank < cur["rank"]:
                rows[host] = {"host": host, "url": url, "title": str(r.get("title") or ""), "snippet": str(r.get("snippet") or ""),
                              "rank": rank, "query": q, "queries": sorted({q, *(cur["queries"] if cur else [])})}
            else:
                cur["queries"] = sorted({q, *cur["queries"]})
    unit, price = LEDGER.connector_unit_cost(provider, cfg)
    if ran:
        db.usage_add({"investigation_id": inv_id, "provider": PR.provider_for_connector(provider), "service": "web_search",
                      "model": "", "requests": ran, "estimated_cost": round((unit or 0) * ran, 6) if unit is not None else None,
                      "cost_status": "estimated" if unit is not None else "unknown",
                      "data": {"purpose": "catalog source discovery", "queries": queries}})
    candidates, known = [], []
    for host, r in rows.items():
        parts = urlsplit(r["url"])
        root = urlunsplit((parts.scheme or "https", parts.netloc, "/", "", ""))
        cls = classify(f"{r['title']} {r['snippet']}", r["url"], code, category)
        d = draft(root if cls["nature"] == "commercial" else r["url"], _title(r["title"], host), r["snippet"], cls,
                  evidence=f"a {REG.REGISTRY[provider].manifest.title} result (rank {r['rank']} for “{r['query']}”)")
        dups = [x for x in CAT.find_duplicates(d, snap) if x["strength"] == "likely"]
        score = (1.0 / r["rank"]) + (0.6 if cls["official"] else 0) + (0.4 if not cls["category_guessed"] else 0) + \
            (0.3 if category and cls["category"] == category else 0) + (0.2 if code and cls["jurisdiction"] == code else 0) + \
            0.1 * (len(r["queries"]) - 1)
        item = {"id": hashlib.sha1(host.encode()).hexdigest()[:10], "host": host, "url": r["url"], "title": d["title"],
                "snippet": r["snippet"][:300], "rank": r["rank"], "query": r["query"], "provider": provider,
                "classification": cls, "draft": d, "score": round(score, 3), "duplicates": dups[:3],
                "status": "Not verified"}
        (known if dups else candidates).append(item)
    candidates.sort(key=lambda c: -c["score"])
    out = {"ok": True, "request": request, "jurisdiction": code, "category": category, "queries": queries,
           "provider": provider, "provider_title": REG.REGISTRY[provider].manifest.title, "errors": errors,
           "candidates": candidates[:25], "already_in_catalog": known[:25], "audit": audit_summary, "at": utcnow(),
           "cost": {"requests": ran, "estimated_usd": round((unit or 0) * ran, 6) if unit is not None else None,
                    "price_verified": bool(price and price.get("verified"))},
           "note": "Search results, classified by keywords: check each one before adding. Nothing was added."}
    db.set_setting(LAST_KEY, out)
    return out


__all__ = ["inspect_url", "discover", "classify", "draft", "search_once", "plan_queries", "LAST_KEY"]
