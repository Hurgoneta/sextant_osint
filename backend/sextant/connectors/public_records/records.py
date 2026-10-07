"""Other official / public-interest records: ICIJ Offshore Leaks, US NPI registry (healthcare providers),
US FEC campaign contributions."""

from __future__ import annotations

import json

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import compare_names
from .._common import add_record, person_names
from ..companies._registry import add_officer


@register
class IcijOffshoreLeaks(Connector):
    manifest = Manifest(
        name="icij_offshore", title="ICIJ Offshore Leaks Database", category="public_records",
        description="Officers, entities and intermediaries in the Panama/Paradise/Pandora Papers and other ICIJ leak "
                    "investigations (published by ICIJ).", access="free", rate_limit=RateLimit(0.5, 1),
        website="https://offshoreleaks.icij.org/", documentation="https://offshoreleaks.icij.org/docs/reconciliation",
        reliability="high", license="ODbL / CC BY-SA (ICIJ)",
        legal="ICIJ: 'There are legitimate uses for offshore companies and trusts. We do not intend to suggest or imply that "
              "any persons, companies or other entities included in the database have broken the law or otherwise acted "
              "improperly.'",
        capabilities=("officers and entities with this name", "source leak (e.g. Panama Papers)", "node type"),
        disclosure=Disclosure(("offshoreleaks.icij.org",), "the person or organization name"),
    )

    @transform(inputs=("person", "organization"), outputs=("public_record", "name"),
               title="ICIJ Offshore Leaks: nodes with this name", priority=0.4,
               accepts=lambda self, e, info: (e.type == "person" and e.is_seed and bool(person_names(e, info)))
               or (e.type == "organization" and bool(e.get("name") or e.label)))
    async def reconcile(self, entity, ctx):
        name = person_names(entity, ctx.info)[0] if entity.type == "person" else str(entity.get("name") or entity.label)
        body = {"queries": {"q0": {"query": name, "limit": 10}}}
        resp = await ctx.post("https://offshoreleaks.icij.org/api/v1/reconcile", content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", "Accept": "application/json"})
        # the reconciliation endpoint answers 201 Created since 2026 (it used to answer 200)
        results = ((ctx.json(ctx.expect(resp, ok=(200, 201))) or {}).get("q0") or {}).get("result") or []
        hits = 0
        for r in results:
            listed = str(r.get("name") or "")
            how = compare_names(name, listed) if entity.type == "person" else ("exact" if listed.casefold() == name.casefold() else None)
            if not how and float(r.get("score") or 0) < 90:
                continue
            hits += 1
            url = f"https://offshoreleaks.icij.org/nodes/{r.get('id')}"
            kind = ((r.get("types") or [{}])[0]).get("name") or "Node"
            src = ctx.out.source(url, title=f"ICIJ Offshore Leaks — {listed}", request=f"name “{name}”")
            add_record(ctx.out, src, key=f"icij:{r.get('id')}", title=f"ICIJ {kind}: {listed}", record_type="leak_database_node",
                       jurisdiction="INT", authority="ICIJ", official_url=url,
                       matched_name=listed if entity.type == "person" else None, name_match=how or "similar", query_name=name,
                       link_from=entity.key if entity.type == "organization" else None,
                       props={"category": kind, "summary": r.get("description"),
                              "note": "Inclusion does not imply wrongdoing (ICIJ)."})
        if not hits:
            raise NotFound("No close match in the Offshore Leaks database")


@register
class UsNpi(Connector):
    manifest = Manifest(
        name="us_npi", title="US NPI Registry (healthcare providers)", category="public_records",
        description="Healthcare providers registered with a National Provider Identifier: name, credential, specialty, "
                    "practice addresses.", access="free", rate_limit=RateLimit(1.0, 2),
        website="https://npiregistry.cms.hhs.gov/", documentation="https://npiregistry.cms.hhs.gov/api-page",
        reliability="official", public_record=True, jurisdictions=("US",), supported_countries=("US",),
        capabilities=("provider by name", "credential and specialty", "practice location", "NPI number"),
        disclosure=Disclosure(("npiregistry.cms.hhs.gov",), "first and last name"),
    )

    @transform(inputs=("person",), outputs=("person", "public_record"), title="NPI: healthcare providers with this name",
               priority=0.3, accepts=lambda self, e, info: e.is_seed and bool(person_names(e, info))
               and (not info.countries or "US" in info.countries))
    async def providers(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        toks = name.split()
        resp = await ctx.get("https://npiregistry.cms.hhs.gov/api/", params={"version": "2.1", "first_name": toks[0],
                                                                            "last_name": toks[-1], "limit": 20})
        hits = 0
        for r in (ctx.json(ctx.expect(resp)) or {}).get("results") or []:
            b = r.get("basic") or {}
            full = " ".join(x for x in (b.get("first_name"), b.get("middle_name"), b.get("last_name")) if x)
            if not compare_names(name, full):
                continue                                  # the API also matches other-name fields loosely
            hits += 1
            addr = next((a for a in r.get("addresses") or [] if a.get("address_purpose") == "LOCATION"), {})
            url = f"https://npiregistry.cms.hhs.gov/provider-view/{r.get('number')}"
            src = ctx.out.source(url, title=f"NPI {r.get('number')}", request=f"name “{name}”")
            rec = add_record(ctx.out, src, key=f"npi:{r.get('number')}", title=f"NPI {r.get('number')}: {full}",
                             record_type="professional_registration", jurisdiction="US", authority="CMS NPPES",
                             official_url=url, matched_name=full, name_match=compare_names(name, full), query_name=name,
                             props={"reference": r.get("number"), "category": ", ".join(t.get("desc", "") for t in (r.get("taxonomies") or [])[:2]),
                                    "date": b.get("enumeration_date"), "status": b.get("status")})
            loc = ", ".join(x for x in (addr.get("city"), addr.get("state")) if x)
            pk = add_officer(ctx.out, src, None, registry="npi", record_id=str(r.get("number")), name=full,
                             role="Healthcare provider", occupation=b.get("credential"), address=loc or None)
            if pk and rec:
                ctx.out.link(pk, rec, "APPEARS_ON", source=src, evidence="The registration describes this provider")
        if not hits:
            raise NotFound("No provider with this name")


@register
class Fec(Connector):
    manifest = Manifest(
        name="fec", title="US FEC — individual contributions", category="public_records",
        description="Federal campaign contributions above $200 by contributor name (city, state, employer, occupation).",
        access="free",
        credentials=(Credential("FEC_API_KEY", "api.data.gov key", "Optional; without it the shared DEMO_KEY (very low "
                                "limits) is used.", required=False, obtain_url="https://api.data.gov/signup/"),),
        rate_limit=RateLimit(0.5, 1, "1,000 requests/hour with a key"), website="https://www.fec.gov/data/",
        documentation="https://api.open.fec.gov/developers/", reliability="official", public_record=True,
        jurisdictions=("US",), supported_countries=("US",),
        legal="52 U.S.C. 30111(a)(4): contributor information may not be used for soliciting contributions or for "
              "commercial purposes.",
        capabilities=("contributions by name", "city/state", "employer and occupation as reported", "recipient committee"),
        disclosure=Disclosure(("api.open.fec.gov",), "the full name"),
    )

    @transform(inputs=("person",), outputs=("public_record", "organization", "location"),
               title="FEC: campaign contributions by this name", priority=0.3,
               accepts=lambda self, e, info: e.is_seed and bool(person_names(e, info))
               and (not info.countries or "US" in info.countries))
    async def contributions(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        key = ctx.credential("FEC_API_KEY") or "DEMO_KEY"
        resp = await ctx.get("https://api.open.fec.gov/v1/schedules/schedule_a/",
                             params={"contributor_name": name, "per_page": 20, "sort": "-contribution_receipt_date", "api_key": key})
        rows = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
        hits = 0
        for r in rows:
            listed = str(r.get("contributor_name") or "")
            how = compare_names(name, listed.replace(",", " "))
            if not how:
                continue
            hits += 1
            url = f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name.replace(' ', '+')}"
            src = ctx.out.source(url, title="FEC individual contributions", request=f"name “{name}”")
            rec = add_record(ctx.out, src, key=f"fec:{r.get('sub_id') or r.get('transaction_id')}",
                             title=f"Contribution to {r.get('committee', {}).get('name') or r.get('committee_id')}",
                             record_type="campaign_contribution", jurisdiction="US", authority="Federal Election Commission",
                             official_url=url, matched_name=listed, name_match=how, query_name=name,
                             props={"date": r.get("contribution_receipt_date"), "category": r.get("receipt_type_full"),
                                    "summary": f"${r.get('contribution_receipt_amount')} — {r.get('contributor_city')}, "
                                               f"{r.get('contributor_state')}; employer: {r.get('contributor_employer')}; "
                                               f"occupation: {r.get('contributor_occupation')}"})
            pk = add_officer(ctx.out, src, None, registry="fec", record_id=str(r.get("sub_id")), name=listed.replace(",", " "),
                             role="Contributor", occupation=r.get("contributor_occupation"),
                             address=", ".join(x for x in (r.get("contributor_city"), r.get("contributor_state")) if x) or None)
            if pk and rec:
                ctx.out.link(pk, rec, "APPEARS_ON", source=src, evidence="Contributor named on the filing")
                emp = r.get("contributor_employer")
                if emp and emp.upper() not in ("NONE", "N/A", "SELF-EMPLOYED", "RETIRED", "NOT EMPLOYED", "SELF"):
                    org = ctx.out.entity("organization", f"name:{emp.casefold()}", label=emp.title(), source=src,
                                         props={"name": emp.title()})
                    ctx.out.link(pk, org, "WORKS_FOR", source=src, confidence=0.8, evidence="Employer as reported to the FEC")
        if not hits:
            raise NotFound("No contributions by this name")

