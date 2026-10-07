"""Publications & research: ORCID (public API), OpenAlex, Crossref."""

from __future__ import annotations

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import compare_names
from .._common import add_link_target, person_names


def _named(_self, e, info) -> bool:
    return e.is_seed and bool(person_names(e, info))


@register
class Orcid(Connector):
    manifest = Manifest(
        name="orcid", title="ORCID (public API)", category="publications",
        description="Researcher identifiers by name or public email: affiliations, works and linked websites.",
        access="free", rate_limit=RateLimit(2.0, 4, "24 requests/second burst, public API"), website="https://orcid.org",
        documentation="https://info.orcid.org/documentation/api-tutorials/", reliability="high", license="CC0 (public data)",
        capabilities=("ORCID iDs by name or public email", "employments/affiliations", "researcher URLs and keywords"),
        disclosure=Disclosure(("pub.orcid.org",), "a name or an email address"),
    )

    async def _search(self, ctx, q: str) -> list[dict]:
        resp = await ctx.get("https://pub.orcid.org/v3.0/expanded-search/", params={"q": q, "rows": 8},
                             headers={"Accept": "application/json"})
        return (ctx.json(ctx.expect(resp)) or {}).get("expanded-result") or []

    def _emit(self, ctx, r: dict, src, link_from=None) -> None:
        oid = r.get("orcid-id")
        full = " ".join(x for x in (r.get("given-names"), r.get("family-names")) if x)
        url = f"https://orcid.org/{oid}"
        acc = ctx.out.entity("account", f"orcid:{oid}", label=f"ORCID: {full or oid}", source=src,
                             props={"platform": "orcid", "platform_title": "ORCID", "username": oid, "display_name": full,
                                    "profile_url": url, "found_by": "name search" if not link_from else "email search",
                                    "emails": r.get("email") or [], "company": (r.get("institution-name") or [None])[0]})
        if link_from:
            ctx.out.link(acc, link_from, "USES", source=src, evidence="Public email on the ORCID record")
        for inst in (r.get("institution-name") or [])[:3]:
            org = ctx.out.entity("organization", f"name:{inst.casefold()}", label=inst, source=src, props={"name": inst})
            ctx.out.link(acc, org, "WORKS_FOR", source=src, confidence=0.8, evidence="Affiliation on the ORCID record")
        if full and len(full.split()) >= 2:
            nk = ctx.out.entity("name", full, source=src)
            ctx.out.link(acc, nk, "HAS_NAME", source=src, evidence="Name on the ORCID record")

    @transform(inputs=("person",), outputs=("account", "organization"), title="ORCID: researchers with this name",
               priority=0.4, accepts=_named)
    async def by_name(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        toks = name.split()
        rows = await self._search(ctx, f'given-names:"{" ".join(toks[:-1])}" AND family-name:"{toks[-1]}"')
        rows = [r for r in rows if compare_names(name, f"{r.get('given-names', '')} {r.get('family-names', '')}")]
        if not rows:
            raise NotFound("No ORCID record with this name")
        src = ctx.out.source(f"https://orcid.org/orcid-search/search?searchQuery={name}", title="ORCID search",
                             request=f"name “{name}”")
        for r in rows[:6]:
            self._emit(ctx, r, src)

    @transform(inputs=("email",), outputs=("account",), title="ORCID: record with this public email", priority=0.5)
    async def by_email(self, entity, ctx):
        rows = await self._search(ctx, f'email:"{entity.value}"')
        if not rows:
            raise NotFound("No ORCID record lists this email publicly")
        src = ctx.out.source("https://orcid.org/orcid-search/search", title="ORCID search", request=f"email “{entity.value}”")
        for r in rows[:3]:
            self._emit(ctx, r, src, link_from=entity.key)


@register
class OpenAlex(Connector):
    manifest = Manifest(
        name="openalex", title="OpenAlex", category="publications",
        description="Scholarly authors by name with institutions, ORCID, works count and recent works.", access="free",
        credentials=(Credential("OPENALEX_API_KEY", "OpenAlex API key", "Optional; higher limits.", required=False,
                                obtain_url="https://openalex.org/settings/api"),),
        rate_limit=RateLimit(5.0, 10, "100,000 requests/day"), website="https://openalex.org",
        documentation="https://docs.openalex.org/", reliability="high", license="CC0",
        capabilities=("authors by name", "last known institution", "ORCID", "works and citation counts"),
        disclosure=Disclosure(("api.openalex.org",), "the full name"),
    )

    @transform(inputs=("person",), outputs=("account", "organization", "publication"), title="OpenAlex: authors with this name",
               priority=0.4, accepts=_named, estimated_requests=2)
    async def authors(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        params = {"search": name, "per-page": 5}
        key = ctx.credential("OPENALEX_API_KEY")
        if key:
            params["api_key"] = key
        elif ctx.info.option("contact_email"):
            params["mailto"] = ctx.info.option("contact_email")
        resp = await ctx.get("https://api.openalex.org/authors", params=params)
        rows = [a for a in (ctx.json(ctx.expect(resp)) or {}).get("results") or [] if compare_names(name, a.get("display_name", ""))]
        if not rows:
            raise NotFound("No OpenAlex author with this name")
        for a in rows[:4]:
            aid = str(a.get("id", "")).rsplit("/", 1)[-1]
            src = ctx.out.source(a.get("id"), title=f"OpenAlex author {aid}", request=f"name “{name}”")
            insts = [i.get("display_name") for i in a.get("last_known_institutions") or [] if i.get("display_name")]
            acc = ctx.out.entity("account", f"openalex:{aid}", label=f"OpenAlex: {a.get('display_name')}", source=src,
                                 props={"platform": "openalex", "platform_title": "OpenAlex", "username": aid,
                                        "display_name": a.get("display_name"), "profile_url": a.get("id"),
                                        "company": insts[0] if insts else None, "found_by": "name search",
                                        "extra": {"works": a.get("works_count"), "citations": a.get("cited_by_count")}})
            nk = ctx.out.entity("name", a.get("display_name"), source=src)
            ctx.out.link(acc, nk, "HAS_NAME", source=src, evidence="Author name")
            for inst in insts[:2]:
                org = ctx.out.entity("organization", f"name:{inst.casefold()}", label=inst, source=src, props={"name": inst})
                ctx.out.link(acc, org, "WORKS_FOR", source=src, confidence=0.8, evidence="Last known institution (OpenAlex)")
            if a.get("orcid"):
                add_link_target(ctx.out, acc, a["orcid"], src, verified=True)


@register
class Crossref(Connector):
    manifest = Manifest(
        name="crossref", title="Crossref", category="publications",
        description="Scholarly works (DOIs) with an author of this name, with affiliations where deposited.", access="free",
        rate_limit=RateLimit(2.0, 4, "polite pool with a contact e-mail"), website="https://www.crossref.org",
        documentation="https://api.crossref.org/swagger-ui/index.html", reliability="high", license="CC0 (metadata)",
        capabilities=("works by author name", "co-authors", "affiliations", "venues and years"),
        disclosure=Disclosure(("api.crossref.org",), "the full name"),
    )

    @transform(inputs=("person",), outputs=("publication",), title="Crossref: works with this author", priority=0.3,
               accepts=_named)
    async def works(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        params = {"query.author": name, "rows": 10, "select": "DOI,title,author,issued,container-title,type,URL"}
        if ctx.info.option("contact_email"):
            params["mailto"] = ctx.info.option("contact_email")
        resp = await ctx.get("https://api.crossref.org/works", params=params)
        items = ((ctx.json(ctx.expect(resp)) or {}).get("message") or {}).get("items") or []
        nk = None
        for w in items:
            authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in w.get("author") or []]
            match = next((a for a in authors if compare_names(name, a)), None)
            if not match:
                continue
            doi = w.get("DOI")
            src = ctx.out.source(f"https://doi.org/{doi}", title="Crossref work", request=f"author “{name}”")
            year = ((w.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            pub = ctx.out.entity("publication", f"doi:{doi}", label=(w.get("title") or [doi])[0][:200], source=src,
                                 props={"title": (w.get("title") or [""])[0], "doi": doi, "year": year,
                                        "venue": (w.get("container-title") or [""])[0], "authors": authors[:20],
                                        "url": w.get("URL"), "kind": w.get("type")})
            nk = ctx.out.entity("name", match, source=src)
            ctx.out.link(nk, pub, "MENTIONED_IN", source=src, confidence=0.6, evidence="Listed as an author")
        if nk is None:
            raise NotFound("No Crossref work with an author of this name")

