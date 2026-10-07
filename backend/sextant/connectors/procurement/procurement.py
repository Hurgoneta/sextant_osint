"""Public procurement: EU TED, US USAspending, UK Contracts Finder — contracts won or awarded by organizations."""

from __future__ import annotations

import json

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ..companies._registry import org_accepts, org_name
from .._common import add_record


@register
class Ted(Connector):
    manifest = Manifest(
        name="ted_procurement", title="EU TED (Tenders Electronic Daily)", category="procurement",
        description="EU public-procurement notices naming the organization as buyer or winner.", access="free",
        rate_limit=RateLimit(1.0, 2), website="https://ted.europa.eu/", documentation="https://docs.ted.europa.eu/api/latest/index.html",
        reliability="official", public_record=True, jurisdictions=("EU",), license="CC BY 4.0 (EU open data)",
        capabilities=("contract notices and awards", "buyer and winner names", "publication date, place of performance"),
        disclosure=Disclosure(("api.ted.europa.eu",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("public_record",), title="TED: EU procurement notices", priority=0.4,
               accepts=org_accepts(None))
    async def notices(self, entity, ctx):
        name = org_name(entity).replace('"', "")
        body = {"query": f'winner-name ~ "{name}" OR buyer-name ~ "{name}"', "limit": 15,
                "fields": ["publication-number", "notice-title", "publication-date", "buyer-name", "winner-name",
                           "notice-type", "place-of-performance"]}
        resp = await ctx.post("https://api.ted.europa.eu/v3/notices/search", content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        notices = (ctx.json(ctx.expect(resp)) or {}).get("notices") or []
        if not notices:
            raise NotFound("No TED notices name this organization")
        for n in notices:
            pub = n.get("publication-number")
            url = f"https://ted.europa.eu/en/notice/-/detail/{pub}"
            title = n.get("notice-title")
            if isinstance(title, dict):
                title = title.get("eng") or next(iter(title.values()), [""])
            if isinstance(title, list):
                title = title[0] if title else ""
            src = ctx.out.source(url, title=f"TED notice {pub}", request=f"name “{name}”")
            add_record(ctx.out, src, key=f"ted:{pub}", title=f"TED {pub}: {str(title)[:150]}", record_type="procurement_notice",
                       jurisdiction="EU", authority="Publications Office of the EU", official_url=url, link_from=entity.key,
                       link_type="ANNOUNCED_IN",
                       props={"reference": pub, "date": str(n.get("publication-date") or "")[:10],
                              "category": n.get("notice-type"), "summary": json.dumps(
                                  {"buyer": n.get("buyer-name"), "winner": n.get("winner-name")}, ensure_ascii=False)[:500]})


@register
class UsaSpending(Connector):
    manifest = Manifest(
        name="usaspending", title="USAspending.gov", category="procurement",
        description="US federal award recipients matching the organization name.", access="free",
        rate_limit=RateLimit(1.0, 2), website="https://www.usaspending.gov/", documentation="https://api.usaspending.gov/",
        reliability="official", public_record=True, jurisdictions=("US",), supported_countries=("US",),
        capabilities=("federal award recipients by name", "UEI / DUNS identifiers"),
        disclosure=Disclosure(("api.usaspending.gov",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("public_record",), title="USAspending: federal award recipients",
               priority=0.3, accepts=org_accepts("US"))
    async def recipients(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.post("https://api.usaspending.gov/api/v2/autocomplete/recipient/",
                              content=json.dumps({"search_text": name, "limit": 10}).encode(),
                              headers={"Content-Type": "application/json"})
        rows = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
        if not rows:
            raise NotFound("No federal award recipient with this name")
        for r in rows:
            rid = r.get("uei") or r.get("duns") or r.get("recipient_name")
            url = f"https://www.usaspending.gov/search/?keyword={str(r.get('recipient_name')).replace(' ', '%20')}"
            src = ctx.out.source(url, title="USAspending recipient", request=f"name “{name}”")
            add_record(ctx.out, src, key=f"usaspending:{rid}", title=f"Federal award recipient: {r.get('recipient_name')}",
                       record_type="procurement_recipient", jurisdiction="US", authority="US Treasury (USAspending)",
                       official_url=url, link_from=entity.key, link_type="ANNOUNCED_IN",
                       props={"reference": rid, "summary": f"UEI {r.get('uei') or '—'}, level {r.get('recipient_level') or '—'}"})


@register
class UkContractsFinder(Connector):
    manifest = Manifest(
        name="uk_contracts_finder", title="UK Contracts Finder", category="procurement",
        description="UK public-sector contract notices and awards mentioning the organization.", access="free",
        rate_limit=RateLimit(0.5, 1), website="https://www.contractsfinder.service.gov.uk/",
        documentation="https://www.contractsfinder.service.gov.uk/apidocumentation", reliability="official",
        public_record=True, jurisdictions=("GB",), supported_countries=("GB",), license="Open Government Licence",
        capabilities=("contract notices and awards (OCDS)", "buyer and supplier names", "values and dates"),
        disclosure=Disclosure(("www.contractsfinder.service.gov.uk",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("public_record",), title="Contracts Finder: UK contract notices",
               priority=0.3, accepts=org_accepts("GB"))
    async def notices(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
                             params={"keyword": name, "limit": 15})
        releases = (ctx.json(ctx.expect(resp)) or {}).get("releases") or []
        hits = 0
        for rel in releases:
            parties = [p.get("name", "") for p in rel.get("parties") or []]
            if not any(name.casefold() in p.casefold() for p in parties):
                continue
            hits += 1
            ocid = rel.get("ocid")
            url = f"https://www.contractsfinder.service.gov.uk/Notice/{str(rel.get('id', '')).split('-')[0]}" if rel.get("id") else \
                "https://www.contractsfinder.service.gov.uk/"
            src = ctx.out.source(url, title=f"Contracts Finder {ocid}", request=f"keyword “{name}”")
            add_record(ctx.out, src, key=f"ukcf:{ocid}", title=f"UK contract: {(rel.get('tender') or {}).get('title', ocid)}",
                       record_type="procurement_notice", jurisdiction="GB", authority="Crown Commercial Service",
                       official_url=url, link_from=entity.key, link_type="ANNOUNCED_IN",
                       props={"reference": ocid, "date": str(rel.get("date") or "")[:10], "summary": "; ".join(parties)[:400]})
        if not hits:
            raise NotFound("No UK contract notices name this organization")
