"""Global company/identifier sources: GLEIF (LEI), OpenCorporates (key), SEC EDGAR (US filings)."""

from __future__ import annotations

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from .._common import person_names
from ._registry import add_officer, add_org, org_accepts, org_name, seed_person


@register
class Gleif(Connector):
    manifest = Manifest(
        name="gleif", title="GLEIF (Legal Entity Identifiers)", category="organizations",
        description="Global LEI index: legal name, legal form, registered address, status and registration authority of "
                    "legal entities (no natural persons).",
        access="free", rate_limit=RateLimit(1.0, 3, "60 requests/minute"), website="https://www.gleif.org",
        documentation="https://www.gleif.org/en/lei-data/gleif-api", reliability="official", public_record=True,
        license="CC0", capabilities=("LEI", "legal name and form", "registered address", "entity status", "registration authority id"),
        disclosure=Disclosure(("api.gleif.org",), "the organization name", "public"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="GLEIF: LEI records for this name",
               priority=0.5, accepts=org_accepts(None))
    async def search(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://api.gleif.org/api/v1/lei-records", params={"filter[fulltext]": name, "page[size]": 5})
        data = (ctx.json(ctx.expect(resp)) or {}).get("data") or []
        if not data:
            raise NotFound("No LEI records")
        for r in data:
            a = r.get("attributes") or {}
            ent = a.get("entity") or {}
            addr = ent.get("legalAddress") or {}
            src = ctx.out.source(f"https://search.gleif.org/#/record/{r.get('id')}", title="GLEIF LEI record",
                                 request=f"name “{name}”")
            add_org(ctx.out, src, scheme="lei", number=r.get("id"), name=(ent.get("legalName") or {}).get("name") or name,
                    jurisdiction=ent.get("jurisdiction") or addr.get("country") or "",
                    props={"lei": r.get("id"), "status": ent.get("status"), "legal_form": (ent.get("legalForm") or {}).get("id"),
                           "address": ", ".join(x for x in [*(addr.get("addressLines") or []), addr.get("city"),
                                                            addr.get("country")] if x),
                           "incorporated": (a.get("registration") or {}).get("initialRegistrationDate")},
                    link_from=entity.key, query_name=name)


@register
class OpenCorporates(Connector):
    manifest = Manifest(
        name="opencorporates", title="OpenCorporates", category="companies",
        description="200M+ companies from 140+ official registers, and officers by name where registers publish them.",
        access="free_key",
        credentials=(Credential("OPENCORPORATES_API_TOKEN", "OpenCorporates API token", "Free for public-benefit/open-data "
                                "projects on application; commercial plans otherwise.", obtain_url="https://opencorporates.com/api_accounts/new"),),
        cost="Free (approved public-benefit use) / paid", rate_limit=RateLimit(0.5, 2), website="https://opencorporates.com",
        documentation="https://api.opencorporates.com/documentation/API-Reference", reliability="high", public_record=True,
        license="ODbL (with attribution)", capabilities=("companies by name in 140+ jurisdictions", "officers by name",
                                                        "company status, type, address, incorporation date"),
        disclosure=Disclosure(("api.opencorporates.com",), "organization and person names"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="OpenCorporates: companies with this name",
               priority=0.5, accepts=org_accepts(None))
    async def companies(self, entity, ctx):
        (token,) = ctx.require("OPENCORPORATES_API_TOKEN")
        name = org_name(entity)
        resp = await ctx.get("https://api.opencorporates.com/v0.4/companies/search",
                             params={"q": name, "api_token": token, "per_page": 10})
        comps = ((ctx.json(ctx.expect(resp)) or {}).get("results") or {}).get("companies") or []
        for c in comps:
            c = c.get("company") or {}
            src = ctx.out.source(c.get("opencorporates_url"), title="OpenCorporates company", request=f"name “{name}”")
            add_org(ctx.out, src, scheme=f"oc-{c.get('jurisdiction_code')}", number=c.get("company_number"), name=c.get("name"),
                    jurisdiction=str(c.get("jurisdiction_code") or "")[:2].upper(),
                    props={"status": c.get("current_status"), "legal_form": c.get("company_type"),
                           "incorporated": c.get("incorporation_date"), "dissolved": c.get("dissolution_date"),
                           "address": c.get("registered_address_in_full")}, link_from=entity.key, query_name=name)

    @transform(inputs=("person",), outputs=("person", "organization"), title="OpenCorporates: officers with this name",
               priority=0.5, accepts=seed_person(), sends="full name “{value}”")
    async def officers(self, entity, ctx):
        (token,) = ctx.require("OPENCORPORATES_API_TOKEN")
        name = person_names(entity, ctx.info)[0]
        resp = await ctx.get("https://api.opencorporates.com/v0.4/officers/search",
                             params={"q": name, "api_token": token, "per_page": 15})
        offs = ((ctx.json(ctx.expect(resp)) or {}).get("results") or {}).get("officers") or []
        for o in offs:
            o = o.get("officer") or {}
            c = o.get("company") or {}
            src = ctx.out.source(o.get("opencorporates_url"), title="OpenCorporates officer", request=f"name “{name}”")
            org = add_org(ctx.out, src, scheme=f"oc-{c.get('jurisdiction_code')}", number=c.get("company_number"),
                          name=c.get("name"), jurisdiction=str(c.get("jurisdiction_code") or "")[:2].upper())
            add_officer(ctx.out, src, org, registry="opencorporates", record_id=str(o.get("id")), name=o.get("name") or "",
                        role=o.get("position") or "", birth_date=o.get("date_of_birth"), nationality=o.get("nationality"),
                        occupation=o.get("occupation"), start=o.get("start_date"), end=o.get("end_date"), query_name=name)


@register
class SecEdgar(Connector):
    manifest = Manifest(
        name="sec_edgar", title="SEC EDGAR full-text search", category="companies",
        description="US securities filings mentioning a person or company (officers, directors, 10% owners, signatories).",
        access="free", rate_limit=RateLimit(2.0, 4, "SEC fair access: ≤10 requests/second with a contact User-Agent"),
        website="https://www.sec.gov/edgar/search/", documentation="https://efts.sec.gov/LATEST/search-index",
        reliability="official", public_record=True, jurisdictions=("US",), supported_countries=("US",),
        legal="SEC fair-access policy: identify yourself in the User-Agent (set a contact e-mail in Settings).",
        capabilities=("filings naming the person", "insider forms 3/4/5", "filer companies and CIKs"),
        disclosure=Disclosure(("efts.sec.gov",), "the name as a full-text query"),
    )

    @transform(inputs=("person", "organization"), outputs=("document", "organization"),
               title="SEC EDGAR: filings mentioning this name", priority=0.4,
               accepts=lambda self, e, info: (e.type == "person" and e.is_seed and bool(person_names(e, info)))
               or (e.type == "organization" and org_accepts("US")(self, e, info)),
               sends="the name as an exact full-text query")
    async def full_text(self, entity, ctx):
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else org_name(entity)
        contact = ctx.info.option("contact_email") or "contact@example.invalid"
        resp = await ctx.get("https://efts.sec.gov/LATEST/search-index", params={"q": f'"{name}"'},
                             headers={"User-Agent": f"Sextant OSINT research {contact}"})
        hits = ((ctx.json(ctx.expect(resp)) or {}).get("hits") or {}).get("hits") or []
        if not hits:
            raise NotFound("No EDGAR filings mention this name")
        nk = ctx.out.entity("name", name, source=ctx.out.source("https://www.sec.gov/edgar/search/", title="EDGAR full-text search",
                                                              request=f"“{name}”")) if entity.type == "person" else entity.key
        for h in hits[:15]:
            s = h.get("_source") or {}
            adsh = str(s.get("adsh") or "")
            fname = str(h.get("_id") or "").split(":", 1)[-1]
            cik = str((s.get("ciks") or [""])[-1]).lstrip("0")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
            src = ctx.out.source(url, title=f"SEC filing {s.get('form')} ({s.get('file_date')})", kind="api",
                                 request=f"full-text “{name}”")
            doc = ctx.out.entity("document", url, label=f"SEC Form {s.get('form')} — {', '.join(s.get('display_names') or [])[:120]}",
                                 source=src, props={"url": url, "title": f"Form {s.get('form')}", "published": s.get("file_date"),
                                                    "file_type": "sec", "host": "sec.gov",
                                                    "snippet": "; ".join(s.get("display_names") or [])})
            ctx.out.link(nk, doc, "MENTIONED_IN", source=src, confidence=0.7, evidence="Name appears in the filing text")
            for dn, ck in zip(s.get("display_names") or [], s.get("ciks") or []):
                if "(" in dn and ck:
                    filer = dn.split("(")[0].strip()
                    org = add_org(ctx.out, src, scheme="us-cik", number=str(ck).lstrip("0"), name=filer, jurisdiction="US")
                    ctx.out.link(org, doc, "AUTHORED", source=src, confidence=0.8, evidence="Filer of the document")
