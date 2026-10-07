"""GDELT DOC 2.0 — global news articles (last ~3 months, 65 languages) mentioning a name."""

from __future__ import annotations

from ...registry.countries import get as country_get
from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, RateLimited
from .._common import person_names
from ..companies._registry import org_name


@register
class Gdelt(Connector):
    manifest = Manifest(
        name="gdelt", title="GDELT news search", category="news",
        description="News articles worldwide (rolling ~3-month window, 65 languages) mentioning a person or organization; "
                    "also restricted to outlets based in the investigation's countries (any of the 195).",
        access="free", rate_limit=RateLimit(0.15, 1, "GDELT asks for at most one request every 5 seconds"),
        website="https://www.gdeltproject.org", documentation="https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/",
        reliability="medium", capabilities=("recent news articles mentioning the name", "news from outlets in a given country",
                      "source country and language"),
        disclosure=Disclosure(("api.gdeltproject.org",), "the name as an exact phrase"),
    )

    @transform(inputs=("person", "organization"), outputs=("website", "name"),
               title="GDELT: news from media in the investigation's countries", priority=0.45, estimated_requests=3,
               accepts=lambda self, e, info: bool(info.countries) and (
                   (e.type == "person" and e.is_seed and bool(person_names(e, info)))
                   or (e.type == "organization" and e.is_seed and bool(e.get("name") or e.label))))
    async def country_articles(self, entity, ctx):
        """The same exact-name search, restricted to outlets based in each country the investigation points to
        (GDELT's sourcecountry filter takes FIPS 10-4 codes, imported from Wikidata into the country registry)."""
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else org_name(entity)
        found = 0
        for code in list(ctx.info.countries)[:3]:
            c = country_get(code)
            if c is None or not c.fips:
                continue
            resp = await ctx.get("https://api.gdeltproject.org/api/v2/doc/doc",
                                 params={"query": f'"{name}" sourcecountry:{c.fips}', "mode": "artlist", "maxrecords": 20,
                                         "format": "json", "sort": "datedesc"})
            if resp.status == 429 or resp.text.lstrip().startswith("Please limit"):
                raise RateLimited("GDELT throttles to one request every 5 seconds")
            arts = (ctx.json(ctx.expect(resp)) or {}).get("articles") or []
            found += len(arts)
            self._emit(ctx, entity, name, arts, f"“{name}” in media from {c.name}")
        if not found:
            raise NotFound("No recent articles from media in these countries")

    def _emit(self, ctx, entity, name: str, arts: list[dict], request: str) -> None:
        hub = ctx.out.entity("name", name, source=ctx.out.source("https://www.gdeltproject.org", title="GDELT DOC API",
                                                                request=request)) if entity.type == "person" else entity.key
        for a in arts:
            src = ctx.out.source(a.get("url"), title=a.get("title") or "News article", kind="web_page", request=request)
            seen = str(a.get("seendate") or "")
            w = ctx.out.entity("website", a.get("url"), label=a.get("title") or a.get("domain"), source=src,
                               props={"url": a.get("url"), "title": a.get("title"), "host": a.get("domain"), "category": "news",
                                      "published": f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if len(seen) >= 8 else None,
                                      "language": a.get("language"), "source_country": a.get("sourcecountry")})
            ctx.out.link(hub, w, "MENTIONED_IN", source=src, confidence=0.6, evidence="News article matching the exact name")

    @transform(inputs=("person", "organization"), outputs=("website", "name"), title="GDELT: news mentioning this name",
               priority=0.4, accepts=lambda self, e, info: (e.type == "person" and e.is_seed and bool(person_names(e, info)))
               or (e.type == "organization" and bool(e.get("name") or e.label)))
    async def articles(self, entity, ctx):
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else org_name(entity)
        resp = await ctx.get("https://api.gdeltproject.org/api/v2/doc/doc",
                             params={"query": f'"{name}"', "mode": "artlist", "maxrecords": 25, "format": "json",
                                     "sort": "datedesc"})
        if resp.status == 429 or resp.text.lstrip().startswith("Please limit"):
            raise RateLimited("GDELT throttles to one request every 5 seconds")
        arts = (ctx.json(ctx.expect(resp)) or {}).get("articles") or []
        if not arts:
            raise NotFound("No recent news articles")
        hub = ctx.out.entity("name", name, source=ctx.out.source("https://www.gdeltproject.org", title="GDELT DOC API",
                                                                request=f"“{name}”")) if entity.type == "person" else entity.key
        for a in arts:
            src = ctx.out.source(a.get("url"), title=a.get("title") or "News article", kind="web_page", request=f"“{name}”")
            seen = str(a.get("seendate") or "")
            w = ctx.out.entity("website", a.get("url"), label=a.get("title") or a.get("domain"), source=src,
                               props={"url": a.get("url"), "title": a.get("title"), "host": a.get("domain"), "category": "news",
                                      "published": f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if len(seen) >= 8 else None,
                                      "language": a.get("language"), "source_country": a.get("sourcecountry")})
            ctx.out.link(hub, w, "MENTIONED_IN", source=src, confidence=0.6, evidence="News article matching the exact name")
