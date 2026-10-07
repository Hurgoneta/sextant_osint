"""National registers published as open data with public APIs: Latvia, Slovakia, Israel, Taiwan (company
registers) and Colombia (public procurement, SECOP II). Each was verified live on 2026-10-06; each runs only
for organizations of its own country, or for the investigation's input when it points to that country.

Personal data is limited to what the register publishes for officers (name, role, dates). Masked personal
ID numbers and officers' street addresses are deliberately not imported.
"""

from __future__ import annotations

import json

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import name_tokens, org_name_key
from .._common import add_record, person_names
from ._registry import add_officer, add_org, org_accepts, org_name, seed_person


def name_matches(query: str, candidate: str) -> bool:
    """Every token of the query (legal-form suffixes ignored) appears in the candidate — full-text APIs also return
    loose matches."""
    q = set((org_name_key(query) or "").split()) or set(name_tokens(query))
    c = set(name_tokens(candidate))
    return bool(q) and q <= c


# --------------------------------------------------------------------------- Latvia

LV_API = "https://data.gov.lv/dati/api/3/action/datastore_search"
LV_COMPANIES = "25e80bf3-f107-4ab4-89ef-251b5b9374e9"
LV_OFFICERS = "e665114a-73c2-4375-9470-55874b4cfa6b"


@register
class LvRegister(Connector):
    manifest = Manifest(
        name="lv_ur", title="Latvia — Register of Enterprises (open data)", category="companies",
        description="Latvian companies and organizations by name, and their board members and representatives; "
                    "board members searchable by name.", access="free", rate_limit=RateLimit(2, 3),
        website="https://www.ur.gov.lv/", documentation="https://data.gov.lv/dati/lv/dataset/uz",
        reliability="official", public_record=True, jurisdictions=("LV",), supported_countries=("LV",),
        license="CC0 1.0 (Register of Enterprises of the Republic of Latvia)",
        capabilities=("companies by name", "registration number, legal form, address, registration/termination date",
                      "board members and representatives (name, body, appointment date)", "board members by name"),
        disclosure=Disclosure(("data.gov.lv",), "organization and person names"),
    )

    async def _query(self, ctx, resource: str, **params) -> list[dict]:
        resp = await ctx.get(LV_API, params={"resource_id": resource, **params})
        return ((ctx.json(ctx.expect(resp)) or {}).get("result") or {}).get("records") or []

    async def _officers(self, ctx, src, org_key: str, regcode, query_name: str | None = None) -> None:
        rows = await self._query(ctx, LV_OFFICERS, filters=json.dumps({"at_legal_entity_registration_number": int(regcode)}),
                                 limit=30)
        for o in rows:
            if o.get("entity_type") != "NATURAL_PERSON" or not o.get("name"):
                continue
            parts = str(o["name"]).split()
            full = " ".join(parts[1:] + parts[:1]) if len(parts) == 2 else str(o["name"])   # "Surname Given" -> "Given Surname"
            add_officer(ctx.out, src, org_key, registry="lv-ur", record_id=f"{regcode}:{o.get('id')}", name=full,
                        role=(o.get("position") or o.get("governing_body") or "").replace("_", " ").lower(),
                        start=str(o.get("registered_on") or "")[:10] or None, query_name=query_name)

    @transform(inputs=("organization",), outputs=("organization", "person"), title="Latvia: companies with this name",
               priority=0.5, accepts=org_accepts("LV"), estimated_requests=4)
    async def companies(self, entity, ctx):
        name = org_name(entity)
        rows = [r for r in await self._query(ctx, LV_COMPANIES, q=name, limit=10) if name_matches(name, str(r.get("name")))]
        if not rows:
            raise NotFound("No Latvian register entry with this name")
        for r in rows[:5]:
            src = ctx.out.source(f"{LV_API}?resource_id={LV_COMPANIES}&q={r.get('regcode')}", title="Latvian Register of Enterprises",
                                 request=f"name “{name}”")
            key = add_org(ctx.out, src, scheme="lv-ur", number=str(r.get("regcode")), name=r.get("name"), jurisdiction="LV",
                          props={"legal_form": r.get("type_text"), "address": r.get("address"),
                                 "incorporated": str(r.get("registered") or "")[:10] or None,
                                 "dissolved": str(r.get("terminated") or "")[:10] or None,
                                 "status": "terminated" if r.get("terminated") else "registered"},
                          link_from=entity.key, query_name=name)
            if key and len(rows) <= 3:
                await self._officers(ctx, src, key, r.get("regcode"))

    @transform(inputs=("person",), outputs=("person", "organization"), title="Latvia: board members with this name",
               priority=0.55, accepts=seed_person("LV"), sends="full name “{value}”")
    async def officers_by_name(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        toks = name.split()
        query = " ".join(toks[-1:] + toks[:-1])                    # the register writes "Surname Given"
        rows = [o for o in await self._query(ctx, LV_OFFICERS, q=query, limit=20)
                if o.get("entity_type") == "NATURAL_PERSON" and name_matches(name, str(o.get("name")))]
        if not rows:
            raise NotFound("No Latvian board member with this name")
        names: dict = {}
        for o in rows[:10]:
            regcode = o.get("at_legal_entity_registration_number")
            if regcode not in names:
                hit = await self._query(ctx, LV_COMPANIES, filters=json.dumps({"regcode": int(regcode)}), limit=1)
                names[regcode] = hit[0] if hit else {}
            company = names[regcode]
            src = ctx.out.source(f"{LV_API}?resource_id={LV_OFFICERS}&q={regcode}", title="Latvian Register of Enterprises — officers",
                                 request=f"name “{name}”")
            org = add_org(ctx.out, src, scheme="lv-ur", number=str(regcode), name=company.get("name") or f"Latvian entity {regcode}",
                          jurisdiction="LV", props={"address": company.get("address"), "legal_form": company.get("type_text")})
            parts = str(o["name"]).split()
            full = " ".join(parts[1:] + parts[:1]) if len(parts) == 2 else str(o["name"])
            add_officer(ctx.out, src, org, registry="lv-ur", record_id=f"{regcode}:{o.get('id')}", name=full,
                        role=(o.get("position") or o.get("governing_body") or "").replace("_", " ").lower(),
                        start=str(o.get("registered_on") or "")[:10] or None, query_name=name)


# --------------------------------------------------------------------------- Slovakia

SK_API = "https://api.statistics.sk/rpo/v1"


def _current(items: list[dict] | None, key: str = "value") -> str:
    items = items or []
    live = [i for i in items if not i.get("validTo")]
    pick = (live or items[-1:] or [{}])[-1]
    return str(pick.get(key) or "")


@register
class SkRpo(Connector):
    manifest = Manifest(
        name="sk_rpo", title="Slovakia — Register of Legal Entities (RPO)", category="companies",
        description="Slovak legal entities and entrepreneurs by name (with former names), and the current members of "
                    "their statutory and supervisory bodies.", access="free", rate_limit=RateLimit(2, 3),
        website="https://rpo.statistics.sk/", documentation="https://susrrpo.docs.apiary.io/",
        reliability="official", public_record=True, jurisdictions=("SK",), supported_countries=("SK",),
        license="CC BY 4.0 (Statistical Office of the Slovak Republic)",
        capabilities=("entities by name, including former names", "IČO, addresses, establishment/termination",
                      "statutory body and supervisory board members (name, role, since)"),
        disclosure=Disclosure(("api.statistics.sk",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization", "person"), title="Slovakia: entities with this name",
               priority=0.5, accepts=org_accepts("SK"), estimated_requests=4)
    async def entities(self, entity, ctx):
        name = org_name(entity)
        results: list[dict] = []
        for q in dict.fromkeys([name, org_name_key(name) or name]):   # "SLOVNAFT, a.s." → also "slovnaft"
            resp = await ctx.get(f"{SK_API}/search", params={"fullName": q})
            results = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
            if results:
                break
        results = [r for r in results if any(name_matches(name, str(n.get("value"))) for n in r.get("fullNames") or [])]
        if not results:
            raise NotFound("No Slovak register entry with this name")
        key_q = org_name_key(name)
        results.sort(key=lambda r: org_name_key(_current(r.get("fullNames"))) != key_q)      # exact name first
        for r in results[:5]:
            current = _current(r.get("fullNames"))
            ico = _current(r.get("identifiers"))
            url = f"https://rpo.statistics.sk/rpo/#/detail/{r.get('id')}"
            src = ctx.out.source(url, title="Register právnických osôb", request=f"name “{name}”")
            addr = (r.get("addresses") or [{}])[-1]
            key = add_org(ctx.out, src, scheme="sk-ico", number=ico or str(r.get("id")), name=current, jurisdiction="SK",
                          props={"incorporated": r.get("establishment"), "dissolved": r.get("termination"),
                                 "former_names": [n["value"] for n in r.get("fullNames") or [] if n.get("validTo")][:10],
                                 "address": " ".join(str(x) for x in (addr.get("street"), addr.get("buildingNumber"),
                                                                      (addr.get("municipality") or {}).get("value")) if x)},
                          link_from=entity.key, query_name=name)
            exact = org_name_key(current) == org_name_key(name)
            if not key or not (exact or len(results) <= 2):      # officers only for the matching entity, not namesakes
                continue
            d = await ctx.get(f"{SK_API}/entity/{r.get('id')}", params={"showHistoricalData": "false"})
            if d.status != 200:
                continue
            detail = ctx.json(d) or {}
            for body in (detail.get("statutoryBodies") or []) + (detail.get("stakeholders") or []):
                pn = body.get("personName") or {}
                full = pn.get("formatedName") or " ".join([*(pn.get("givenNames") or []), *(pn.get("familyNames") or [])])
                if not full:
                    continue
                role = (body.get("statutoryBodyMember") or {}).get("value") or (body.get("stakeholderType") or {}).get("value") or ""
                where = (body.get("address") or {})
                add_officer(ctx.out, src, key, registry="sk-rpo", record_id=f"{r.get('id')}:{full.casefold()}:{role}", name=full,
                            role=role, start=body.get("validFrom"),
                            address=", ".join(x for x in ((where.get("municipality") or {}).get("value"),
                                                          (where.get("country") or {}).get("value")) if x) or None)


# --------------------------------------------------------------------------- Israel

IL_API = "https://data.gov.il/api/3/action/datastore_search"
IL_COMPANIES = "f004176c-b85f-4542-8901-7b3176f9a054"


@register
class IlCompanies(Connector):
    manifest = Manifest(
        name="il_companies", title="Israel — Registrar of Companies (open data)", category="companies",
        description="Israeli companies by Hebrew or English name: company number, type, status, purpose, incorporation "
                    "date and address (Israel Corporations Authority dataset).", access="free", rate_limit=RateLimit(2, 3),
        website="https://www.gov.il/en/departments/israeli_corporations_authority",
        documentation="https://data.gov.il/dataset/ica_companies", reliability="official", public_record=True,
        jurisdictions=("IL",), supported_countries=("IL",), license="data.gov.il open licence (Other — Open)",
        capabilities=("companies by Hebrew or English name", "company number, status, type, purpose", "incorporation date, city"),
        disclosure=Disclosure(("data.gov.il",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="Israel: companies with this name", priority=0.5,
               accepts=org_accepts("IL"))
    async def companies(self, entity, ctx):
        name = org_name(entity)
        field = "שם חברה" if any("\u0590" <= ch <= "\u05ff" for ch in name) else "שם באנגלית"   # Hebrew or English name
        resp = await ctx.get(IL_API, params={"resource_id": IL_COMPANIES, "q": json.dumps({field: name}, ensure_ascii=False),
                                             "limit": 15})
        rows = ((ctx.json(ctx.expect(resp)) or {}).get("result") or {}).get("records") or []
        rows = [r for r in rows if name_matches(name, str(r.get("שם באנגלית") or "")) or name_matches(name, str(r.get("שם חברה") or ""))]
        if not rows:
            raise NotFound("No Israeli company with this name")
        for r in rows[:6]:
            num = str(r.get("מספר חברה"))
            src = ctx.out.source(f"{IL_API}?resource_id={IL_COMPANIES}&q={num}", title="Israel Registrar of Companies (open data)",
                                 request=f"name “{name}”")
            heb, eng = r.get("שם חברה"), r.get("שם באנגלית")
            add_org(ctx.out, src, scheme="il-ica", number=num, name=str(eng or heb).replace("~", '"'), jurisdiction="IL",
                    props={"alt_names": [str(heb).replace("~", '"')] if heb and eng else None, "status": r.get("סטטוס חברה"),
                           "legal_form": r.get("סוג תאגיד"), "incorporated": r.get("תאריך התאגדות"),
                           "description": r.get("מטרת החברה"),
                           "address": " ".join(str(x) for x in (r.get("שם רחוב"), r.get("מספר בית"), r.get("שם עיר")) if x)},
                    link_from=entity.key, query_name=name)


# --------------------------------------------------------------------------- Taiwan

TW_API = "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"


@register
class TwGcis(Connector):
    manifest = Manifest(
        name="tw_gcis", title="Taiwan — company registration (GCIS open data)", category="companies",
        description="Active Taiwanese companies by name (keyword) with their registered representative, capital, address "
                    "and setup date (Department of Commerce, Ministry of Economic Affairs).", access="free",
        rate_limit=RateLimit(1, 2), website="https://findbiz.nat.gov.tw/", documentation="https://data.gcis.nat.gov.tw/od/rule",
        reliability="official", public_record=True, jurisdictions=("TW",), supported_countries=("TW",),
        license="Open Government Data License, version 1.0 (Taiwan)",
        capabilities=("companies by name (Chinese)", "unified business number", "registered representative",
                      "capital, address, setup date"),
        disclosure=Disclosure(("data.gcis.nat.gov.tw",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization", "person"), title="Taiwan: companies with this name",
               priority=0.5, accepts=org_accepts("TW"))
    async def companies(self, entity, ctx):
        name = org_name(entity).replace("'", "")
        resp = await ctx.get(TW_API, params={"$format": "json", "$filter": f"Company_Name like {name} and Company_Status eq 01",
                                            "$skip": "0", "$top": "10"})   # the API answers only with a status filter (01 = active)
        if resp.status == 200 and not resp.content.strip():
            raise NotFound("No Taiwanese company with this name")
        rows = ctx.json(ctx.expect(resp)) or []
        if not rows:
            raise NotFound("No Taiwanese company with this name")
        for r in rows[:8]:
            ban = str(r.get("Business_Accounting_NO"))
            src = ctx.out.source(f"https://findbiz.nat.gov.tw/fts/query/QueryList/queryList.do?qryCond={ban}",
                                 title="GCIS company registration", request=f"name “{name}”")
            setup = str(r.get("Company_Setup_Date") or "")
            year = f"{int(setup[:3]) + 1911}-{setup[3:5]}-{setup[5:7]}" if len(setup) == 7 and setup.isdigit() else None
            key = add_org(ctx.out, src, scheme="tw-ban", number=ban, name=r.get("Company_Name"), jurisdiction="TW",
                          props={"status": r.get("Company_Status_Desc"), "address": r.get("Company_Location"),
                                 "incorporated": year, "capital": r.get("Capital_Stock_Amount"),
                                 "registry": r.get("Register_Organization_Desc"),
                                 "representative": str(r.get("Responsible_Name") or "").strip() or None},
                          link_from=entity.key, query_name=name)
            rep = str(r.get("Responsible_Name") or "").strip()
            if key and rep and " " in rep:     # romanised names; a CJK name is kept as the company's "representative"
                add_officer(ctx.out, src, key, registry="tw-gcis", record_id=f"{ban}:rep", name=rep, role="Representative (代表人)")


# --------------------------------------------------------------------------- Colombia (procurement)

CO_API = "https://www.datos.gov.co/resource/jbjy-vk9h.json"


@register
class CoSecop(Connector):
    manifest = Manifest(
        name="co_secop", title="Colombia — SECOP II public contracts", category="procurement",
        description="Colombian public contracts (SECOP II) awarded to a supplier with this name: buyer, value, signing "
                    "date, object and the official process link.", access="free", rate_limit=RateLimit(0.5, 1),
        website="https://www.colombiacompra.gov.co/", documentation="https://www.datos.gov.co/d/jbjy-vk9h",
        reliability="official", public_record=True, jurisdictions=("CO",), supported_countries=("CO",), timeout=180,
        license="CC BY-SA 4.0 (datos.gov.co)",
        capabilities=("contracts awarded to a supplier", "buyer entity, contract value, signing date, object"),
        disclosure=Disclosure(("www.datos.gov.co",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("public_record",), title="Colombia: SECOP II contracts with this supplier",
               priority=0.4, accepts=org_accepts("CO"))
    async def contracts(self, entity, ctx):
        name = org_name(entity).upper().replace("'", "")
        resp = await ctx.get(CO_API, params={
            "$limit": "15", "$order": "fecha_de_firma DESC",
            "$where": f"upper(proveedor_adjudicado) like '%{name}%'",
            "$select": "nombre_entidad,proveedor_adjudicado,documento_proveedor,valor_del_contrato,fecha_de_firma,"
                       "objeto_del_contrato,urlproceso,id_contrato"})
        rows = ctx.json(ctx.expect(resp)) or []
        if not rows:
            raise NotFound("No SECOP II contract names this supplier")
        for r in rows:
            url = (r.get("urlproceso") or {}).get("url") if isinstance(r.get("urlproceso"), dict) else r.get("urlproceso")
            src = ctx.out.source(url or CO_API, title=f"SECOP II contract {r.get('id_contrato')}", request=f"supplier “{name}”")
            add_record(ctx.out, src, key=f"co-secop:{r.get('id_contrato')}",
                       title=f"SECOP II {r.get('id_contrato')}: {str(r.get('objeto_del_contrato') or '')[:140]}",
                       record_type="procurement_contract", jurisdiction="CO", authority=str(r.get("nombre_entidad") or ""),
                       official_url=url, link_from=entity.key, link_type="ANNOUNCED_IN",
                       props={"reference": r.get("id_contrato"), "date": str(r.get("fecha_de_firma") or "")[:10],
                              "summary": json.dumps({"supplier": r.get("proveedor_adjudicado"),
                                                     "supplier_id": r.get("documento_proveedor"),
                                                     "value_cop": r.get("valor_del_contrato")}, ensure_ascii=False)[:500]})
