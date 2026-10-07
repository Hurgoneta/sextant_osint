"""Wikidata — notable people and organizations: identifiers, dates, positions, employers, official websites,
social-media handles (all community-curated, with references)."""

from __future__ import annotations

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import compare_names
from .._common import add_link_target, person_names
from ..companies._registry import add_org, org_name

API = "https://www.wikidata.org/w/api.php"
HANDLES = {"P2002": "https://x.com/{}", "P2003": "https://www.instagram.com/{}/", "P2037": "https://github.com/{}",
           "P4033": "https://{}", "P2013": "https://www.facebook.com/{}", "P6634": "https://www.linkedin.com/in/{}/",
           "P2397": "https://www.youtube.com/channel/{}", "P7085": "https://www.tiktok.com/@{}", "P12361": "https://bsky.app/profile/{}"}


def claim_values(claims: dict, prop: str) -> list:
    out = []
    for c in claims.get(prop) or []:
        dv = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if dv is None:
            continue
        if isinstance(dv, dict) and "time" in dv:
            out.append(dv["time"].lstrip("+")[:10])
        elif isinstance(dv, dict) and "id" in dv:
            out.append(dv["id"])
        else:
            out.append(dv)
    return out


@register
class Wikidata(Connector):
    manifest = Manifest(
        name="wikidata", title="Wikidata", category="organizations",
        description="Notable people and organizations: birth/inception dates, occupations, employers, official websites and "
                    "social-media handles recorded with references.", access="free", rate_limit=RateLimit(1.0, 3),
        website="https://www.wikidata.org", documentation="https://www.wikidata.org/wiki/Wikidata:Data_access",
        reliability="medium", license="CC0", capabilities=("notable persons/organizations by name", "dates of birth",
                                                          "occupations, employers", "official website", "social handles"),
        disclosure=Disclosure(("www.wikidata.org",), "the person or organization name"),
    )

    async def _search(self, ctx, name: str) -> list[dict]:
        resp = await ctx.get(API, params={"action": "wbsearchentities", "search": name, "language": "en", "format": "json",
                                          "limit": 5, "type": "item"})
        return (ctx.json(ctx.expect(resp)) or {}).get("search") or []

    async def _entities(self, ctx, ids: list[str]) -> dict:
        resp = await ctx.get(API, params={"action": "wbgetentities", "ids": "|".join(ids), "format": "json",
                                          "props": "labels|descriptions|claims", "languages": "en"})
        return (ctx.json(ctx.expect(resp)) or {}).get("entities") or {}

    async def _labels(self, ctx, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        resp = await ctx.get(API, params={"action": "wbgetentities", "ids": "|".join(ids[:40]), "format": "json",
                                          "props": "labels", "languages": "en"})
        ents = (ctx.json(resp) or {}).get("entities") or {} if resp.status == 200 else {}
        return {k: ((v.get("labels") or {}).get("en") or {}).get("value", k) for k, v in ents.items()}

    @transform(inputs=("person",), outputs=("person", "organization", "account", "domain"),
               title="Wikidata: notable people with this name", priority=0.4,
               accepts=lambda self, e, info: e.is_seed and bool(person_names(e, info)))
    async def person(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        hits = [h for h in await self._search(ctx, name) if compare_names(name, h.get("label", ""))]
        if not hits:
            raise NotFound("No Wikidata item with this name")
        ents = await self._entities(ctx, [h["id"] for h in hits[:3]])
        for qid, e in ents.items():
            claims = e.get("claims") or {}
            if "Q5" not in claim_values(claims, "P31"):
                continue                                     # not a human
            labels = await self._labels(ctx, claim_values(claims, "P106")[:3] + claim_values(claims, "P108")[:3]
                                        + claim_values(claims, "P27")[:2])
            label = ((e.get("labels") or {}).get("en") or {}).get("value", name)
            url = f"https://www.wikidata.org/wiki/{qid}"
            src = ctx.out.source(url, title=f"Wikidata {qid}: {label}", request=f"name “{name}”")
            births = claim_values(claims, "P569")
            pk = ctx.out.entity("person", f"wikidata:{qid}", label=label, source=src,
                                props={"full_name": label, "birth_date": births[0] if births else None,
                                       "occupation": [labels.get(x, x) for x in claim_values(claims, "P106")[:3]],
                                       "nationality": [labels.get(x, x) for x in claim_values(claims, "P27")[:2]],
                                       "description": ((e.get("descriptions") or {}).get("en") or {}).get("value")})
            nk = ctx.out.entity("name", label, source=src)
            ctx.out.link(pk, nk, "HAS_NAME", source=src, evidence="Wikidata label")
            for emp in claim_values(claims, "P108")[:3]:
                org = add_org(ctx.out, src, scheme="wikidata", number=emp, name=labels.get(emp, emp), jurisdiction="")
                ctx.out.link(pk, org, "WORKS_FOR", source=src, evidence="Employer (P108) on Wikidata")
            for site in claim_values(claims, "P856")[:2]:
                add_link_target(ctx.out, pk, str(site), src)
            for prop, tpl in HANDLES.items():
                for v in claim_values(claims, prop)[:1]:
                    add_link_target(ctx.out, pk, tpl.format(v), src)

    @transform(inputs=("organization",), outputs=("organization", "domain", "account"),
               title="Wikidata: organization details", priority=0.4,
               accepts=lambda self, e, info: bool(e.get("name") or e.label) and not str(e.value).startswith("wikidata:"))
    async def organization(self, entity, ctx):
        name = org_name(entity)
        hits = await self._search(ctx, name)
        if not hits:
            raise NotFound("No Wikidata item with this name")
        ents = await self._entities(ctx, [h["id"] for h in hits[:2]])
        for qid, e in ents.items():
            claims = e.get("claims") or {}
            label = ((e.get("labels") or {}).get("en") or {}).get("value", name)
            if label.casefold() != name.casefold():
                continue
            url = f"https://www.wikidata.org/wiki/{qid}"
            src = ctx.out.source(url, title=f"Wikidata {qid}: {label}", request=f"name “{name}”")
            lei = claim_values(claims, "P1278")
            org = add_org(ctx.out, src, scheme="wikidata", number=qid, name=label, jurisdiction="",
                          props={"lei": lei[0] if lei else None, "incorporated": (claim_values(claims, "P571") or [None])[0]},
                          link_from=entity.key, query_name=name)
            for site in claim_values(claims, "P856")[:2]:
                add_link_target(ctx.out, org, str(site), src)
            for prop, tpl in HANDLES.items():
                for v in claim_values(claims, prop)[:1]:
                    add_link_target(ctx.out, org, tpl.format(v), src)
