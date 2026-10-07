"""National company registers with official APIs: UK, France, Norway, Czechia, Finland, Estonia, Poland, Brazil,
Australia. Each runs only for organizations of its own country (or on the investigation's input)."""

from __future__ import annotations

import base64
import json
import re

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, SourceUnavailable
from .._common import person_names
from ._registry import add_officer, add_org, org_accepts, org_name, seed_person

# --------------------------------------------------------------------------- United Kingdom


@register
class UkCompaniesHouse(Connector):
    manifest = Manifest(
        name="uk_companies_house", title="UK Companies House", category="companies",
        description="UK companies, officers (with month/year of birth, nationality, occupation) and disqualified directors; "
                    "searchable by officer name.", access="free_key",
        credentials=(Credential("COMPANIES_HOUSE_API_KEY", "Companies House API key", "Free REST API key.",
                                obtain_url="https://developer.company-information.service.gov.uk/"),),
        cost="Free", rate_limit=RateLimit(1.5, 5, "600 requests / 5 minutes"), website="https://find-and-update.company-information.service.gov.uk/",
        documentation="https://developer-specs.company-information.service.gov.uk/", reliability="official",
        public_record=True, jurisdictions=("GB",), supported_countries=("GB",), license="Open Government Licence",
        capabilities=("companies by name", "officers by name with month/year of birth", "appointments of an officer",
                      "registered address, status, incorporation date"),
        disclosure=Disclosure(("api.company-information.service.gov.uk",), "organization and person names"),
    )

    def _h(self, ctx) -> dict:
        (key,) = ctx.require("COMPANIES_HOUSE_API_KEY")
        return {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}

    @transform(inputs=("organization",), outputs=("organization",), title="Companies House: companies with this name",
               priority=0.5, accepts=org_accepts("GB"))
    async def companies(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://api.company-information.service.gov.uk/search/companies",
                             params={"q": name, "items_per_page": 10}, headers=self._h(ctx))
        for c in (ctx.json(ctx.expect(resp)) or {}).get("items") or []:
            url = f"https://find-and-update.company-information.service.gov.uk/company/{c.get('company_number')}"
            src = ctx.out.source(url, title="Companies House company", request=f"name “{name}”")
            add_org(ctx.out, src, scheme="gb-coh", number=c.get("company_number"), name=c.get("title"), jurisdiction="GB",
                    props={"status": c.get("company_status"), "legal_form": c.get("company_type"),
                           "incorporated": c.get("date_of_creation"), "dissolved": c.get("date_of_cessation"),
                           "address": c.get("address_snippet")}, link_from=entity.key, query_name=name)

    @transform(inputs=("person",), outputs=("person", "organization"), title="Companies House: officers with this name",
               priority=0.6, accepts=seed_person("GB"), estimated_requests=6, sends="full name “{value}”")
    async def officers(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        h = self._h(ctx)
        resp = await ctx.get("https://api.company-information.service.gov.uk/search/officers",
                             params={"q": name, "items_per_page": 10}, headers=h)
        items = (ctx.json(ctx.expect(resp)) or {}).get("items") or []
        if not items:
            raise NotFound("No officers with this name")
        for o in items[:6]:
            link = (o.get("links") or {}).get("self") or ""
            dob = o.get("date_of_birth") or {}
            r2 = await ctx.get(f"https://api.company-information.service.gov.uk{link}", params={"items_per_page": 10},
                               headers=h)
            apps = (ctx.json(r2) or {}).get("items") or [] if r2.status == 200 else []
            oid = link.strip("/").split("/")[1] if link.count("/") >= 2 else o.get("title")
            for a in apps[:10]:
                to = a.get("appointed_to") or {}
                url = f"https://find-and-update.company-information.service.gov.uk{link}"
                src = ctx.out.source(url, title="Companies House officer appointments", request=f"name “{name}”")
                org = add_org(ctx.out, src, scheme="gb-coh", number=to.get("company_number"), name=to.get("company_name"),
                              jurisdiction="GB", props={"status": to.get("company_status")})
                add_officer(ctx.out, src, org, registry="gb-coh-officer", record_id=str(oid), name=a.get("name") or o.get("title", ""),
                            role=a.get("officer_role", ""),
                            birth_date=f"{dob['year']}-{int(dob['month']):02d}" if dob.get("year") and dob.get("month") else None,
                            nationality=a.get("nationality"), occupation=a.get("occupation"),
                            address=a.get("country_of_residence"), start=a.get("appointed_on"), end=a.get("resigned_on"),
                            query_name=name)


# --------------------------------------------------------------------------- France


@register
class FrEntreprises(Connector):
    manifest = Manifest(
        name="fr_entreprises", title="France — Recherche d'entreprises", category="companies",
        description="French companies (SIREN) and their directors (name, year/month of birth, role); searchable by "
                    "director name.", access="free", rate_limit=RateLimit(5, 7, "7 requests/second"),
        website="https://annuaire-entreprises.data.gouv.fr/", documentation="https://recherche-entreprises.api.gouv.fr/docs/",
        reliability="official", public_record=True, jurisdictions=("FR",), supported_countries=("FR",),
        license="Licence Ouverte 2.0", capabilities=("companies by name", "companies by director name",
                                                     "directors with year/month of birth", "registered office, status"),
        disclosure=Disclosure(("recherche-entreprises.api.gouv.fr",), "organization and person names"),
    )

    def _emit(self, ctx, r: dict, src, link_from=None, query_name=None, person_query=None) -> None:
        siege = r.get("siege") or {}
        org = add_org(ctx.out, src, scheme="fr-siren", number=r.get("siren"), name=r.get("nom_complet"), jurisdiction="FR",
                      props={"status": "active" if r.get("etat_administratif") == "A" else "ceased",
                             "incorporated": r.get("date_creation"), "address": siege.get("adresse"),
                             "industry": r.get("activite_principale")}, link_from=link_from, query_name=query_name)
        for i, d in enumerate((r.get("dirigeants") or [])[:12]):
            if d.get("type_dirigeant") != "personne physique":
                continue
            full = f"{d.get('prenoms', '')} {d.get('nom', '')}".strip()
            add_officer(ctx.out, src, org, registry="fr-rne", record_id=f"{r.get('siren')}:{full.casefold()}", name=full,
                        role=d.get("qualite") or "", birth_date=d.get("date_de_naissance") or d.get("annee_de_naissance"),
                        nationality=d.get("nationalite"), query_name=person_query)

    @transform(inputs=("organization",), outputs=("organization", "person"), title="France: companies with this name",
               priority=0.5, accepts=org_accepts("FR"))
    async def companies(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://recherche-entreprises.api.gouv.fr/search", params={"q": name, "per_page": 5})
        for r in (ctx.json(ctx.expect(resp)) or {}).get("results") or []:
            src = ctx.out.source(f"https://annuaire-entreprises.data.gouv.fr/entreprise/{r.get('siren')}",
                                 title="Annuaire des entreprises", request=f"name “{name}”")
            self._emit(ctx, r, src, link_from=entity.key, query_name=name)

    @transform(inputs=("person",), outputs=("organization", "person"), title="France: companies directed by this name",
               priority=0.6, accepts=seed_person("FR"), sends="last name and first names")
    async def by_director(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        toks = name.split()
        resp = await ctx.get("https://recherche-entreprises.api.gouv.fr/search",
                             params={"nom_personne": toks[-1], "prenoms_personne": " ".join(toks[:-1]), "per_page": 10})
        results = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
        if not results:
            raise NotFound("No French company directed by this name")
        for r in results:
            src = ctx.out.source(f"https://annuaire-entreprises.data.gouv.fr/entreprise/{r.get('siren')}",
                                 title="Annuaire des entreprises", request=f"director “{name}”")
            self._emit(ctx, r, src, person_query=name)


# --------------------------------------------------------------------------- Norway


@register
class NoBrreg(Connector):
    manifest = Manifest(
        name="no_brreg", title="Norway — Brønnøysund Register Centre", category="companies",
        description="Norwegian entities and their roles (CEO, board, auditors) with dates of birth.", access="free",
        rate_limit=RateLimit(2.0, 4), website="https://www.brreg.no", documentation="https://data.brreg.no/enhetsregisteret/api/docs/index.html",
        reliability="official", public_record=True, jurisdictions=("NO",), supported_countries=("NO",), license="NLOD",
        capabilities=("entities by name", "roles with names and dates of birth", "business address, industry code"),
        disclosure=Disclosure(("data.brreg.no",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization", "person"), title="Brønnøysund: entities and roles",
               priority=0.5, accepts=org_accepts("NO"), estimated_requests=4)
    async def entities(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://data.brreg.no/enhetsregisteret/api/enheter", params={"navn": name, "size": 3})
        items = ((ctx.json(ctx.expect(resp)) or {}).get("_embedded") or {}).get("enheter") or []
        if not items:
            raise NotFound("No Norwegian entity with this name")
        for e in items:
            nr = e.get("organisasjonsnummer")
            addr = e.get("forretningsadresse") or {}
            src = ctx.out.source(f"https://virksomhet.brreg.no/nb/oppslag/enheter/{nr}", title="Enhetsregisteret",
                                 request=f"name “{name}”")
            org = add_org(ctx.out, src, scheme="no-orgnr", number=nr, name=e.get("navn"), jurisdiction="NO",
                          props={"legal_form": (e.get("organisasjonsform") or {}).get("beskrivelse"),
                                 "incorporated": e.get("stiftelsesdato") or e.get("registreringsdatoEnhetsregisteret"),
                                 "address": ", ".join([*(addr.get("adresse") or []), addr.get("poststed") or ""]).strip(", "),
                                 "website": e.get("hjemmeside"), "industry": (e.get("naeringskode1") or {}).get("beskrivelse")},
                          link_from=entity.key, query_name=name)
            r2 = await ctx.get(f"https://data.brreg.no/enhetsregisteret/api/enheter/{nr}/roller")
            if r2.status != 200:
                continue
            for grp in (ctx.json(r2) or {}).get("rollegrupper") or []:
                for role in grp.get("roller") or []:
                    p = role.get("person") or {}
                    navn = p.get("navn") or {}
                    full = " ".join(x for x in (navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn")) if x)
                    if full:
                        add_officer(ctx.out, src, org, registry="no-brreg", record_id=f"{nr}:{full.casefold()}:{p.get('fodselsdato')}",
                                    name=full, role=(role.get("type") or {}).get("beskrivelse", ""), birth_date=p.get("fodselsdato"))


# --------------------------------------------------------------------------- Czechia


@register
class CzAres(Connector):
    manifest = Manifest(
        name="cz_ares", title="Czechia — ARES", category="companies",
        description="Czech economic subjects and their statutory bodies (names, dates of birth) from the public register.",
        access="free", rate_limit=RateLimit(2.0, 4, "500 requests/minute"), website="https://ares.gov.cz",
        documentation="https://ares.gov.cz/swagger-ui/", reliability="official", public_record=True,
        jurisdictions=("CZ",), supported_countries=("CZ",),
        capabilities=("companies by name", "statutory body members with dates of birth", "seat address"),
        disclosure=Disclosure(("ares.gov.cz",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization", "person"), title="ARES: subjects and statutory bodies",
               priority=0.5, accepts=org_accepts("CZ"), estimated_requests=4)
    async def subjects(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.post("https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/vyhledat",
                              content=json.dumps({"obchodniJmeno": name, "pocet": 3}).encode(),
                              headers={"Content-Type": "application/json", "Accept": "application/json"})
        subjects = (ctx.json(ctx.expect(resp)) or {}).get("ekonomickeSubjekty") or []
        if not subjects:
            raise NotFound("No Czech subject with this name")
        for s in subjects:
            ico = s.get("ico")
            src = ctx.out.source(f"https://ares.gov.cz/ekonomicke-subjekty?ico={ico}", title="ARES", request=f"name “{name}”")
            org = add_org(ctx.out, src, scheme="cz-ico", number=ico, name=s.get("obchodniJmeno"), jurisdiction="CZ",
                          props={"address": (s.get("sidlo") or {}).get("textovaAdresa"), "incorporated": s.get("datumVzniku"),
                                 "dissolved": s.get("datumZaniku")}, link_from=entity.key, query_name=name)
            r2 = await ctx.get(f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-vr/{ico}")
            if r2.status != 200:
                continue
            for z in ((ctx.json(r2) or {}).get("zaznamy") or [])[:1]:
                for organ in z.get("statutarniOrgany") or []:
                    for m in organ.get("clenoveOrganu") or []:
                        fo = m.get("fyzickaOsoba") or {}
                        full = f"{fo.get('jmeno', '')} {fo.get('prijmeni', '')}".strip()
                        if full and not m.get("datumVymazu"):
                            add_officer(ctx.out, src, org, registry="cz-vr", record_id=f"{ico}:{full.casefold()}",
                                        name=full, role=organ.get("nazevOrganu", ""), birth_date=fo.get("datumNarozeni"),
                                        address=(fo.get("adresa") or {}).get("textovaAdresa"))


# --------------------------------------------------------------------------- Finland / Estonia


@register
class FiPrh(Connector):
    manifest = Manifest(
        name="fi_prh", title="Finland — PRH / YTJ", category="companies",
        description="Finnish companies from the Business Information System open data.", access="free",
        rate_limit=RateLimit(1.0, 2), website="https://tietopalvelu.ytj.fi/", documentation="https://avoindata.prh.fi/en",
        reliability="official", public_record=True, jurisdictions=("FI",), supported_countries=("FI",), license="CC BY 4.0",
        capabilities=("companies by name", "business id", "addresses", "company form and status", "website"),
        disclosure=Disclosure(("avoindata.prh.fi",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="PRH: Finnish companies with this name",
               priority=0.4, accepts=org_accepts("FI"))
    async def companies(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://avoindata.prh.fi/opendata-ytj-api/v3/companies", params={"name": name})
        comps = (ctx.json(ctx.expect(resp)) or {}).get("companies") or []
        for c in comps[:5]:
            bid = (c.get("businessId") or {}).get("value")
            names = [n.get("name") for n in c.get("names") or [] if n.get("name")]
            src = ctx.out.source(f"https://tietopalvelu.ytj.fi/yritys/{bid}", title="YTJ", request=f"name “{name}”")
            add_org(ctx.out, src, scheme="fi-ytj", number=bid, name=names[0] if names else name, jurisdiction="FI",
                    props={"incorporated": (c.get("businessId") or {}).get("registrationDate"),
                           "website": (c.get("website") or {}).get("url")}, link_from=entity.key, query_name=name)


@register
class EeAriregister(Connector):
    manifest = Manifest(
        name="ee_ariregister", title="Estonia — e-Business Register", category="companies",
        description="Estonian legal entities (name, registry code, status, address) from the public autocomplete service.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://ariregister.rik.ee/",
        documentation="https://ariregister.rik.ee/eng", reliability="official", public_record=True, jurisdictions=("EE",),
        supported_countries=("EE",), capabilities=("companies by name", "registry code", "status", "legal address"),
        disclosure=Disclosure(("ariregister.rik.ee",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="e-Business Register: Estonian entities",
               priority=0.4, accepts=org_accepts("EE"))
    async def companies(self, entity, ctx):
        name = org_name(entity)
        resp = await ctx.get("https://ariregister.rik.ee/est/api/autocomplete", params={"q": name})
        for c in ((ctx.json(ctx.expect(resp)) or {}).get("data") or [])[:5]:
            src = ctx.out.source(c.get("url") or "https://ariregister.rik.ee/", title="e-Business Register", request=f"name “{name}”")
            add_org(ctx.out, src, scheme="ee-reg", number=str(c.get("reg_code")), name=c.get("name"), jurisdiction="EE",
                    props={"status": c.get("status"), "address": c.get("legal_address")}, link_from=entity.key, query_name=name)


# --------------------------------------------------------------------------- Poland / Brazil (lookup by number)


@register
class PlKrs(Connector):
    manifest = Manifest(
        name="pl_krs", title="Poland — KRS (by KRS number)", category="companies",
        description="Current extract of a Polish company by KRS number: name, seat, management board members.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://wyszukiwarka-krs.ms.gov.pl/",
        documentation="https://api-krs.ms.gov.pl/", reliability="official", public_record=True, jurisdictions=("PL",),
        supported_countries=("PL",), capabilities=("company extract by KRS number", "management board members"),
        disclosure=Disclosure(("api-krs.ms.gov.pl",), "the KRS number"),
    )

    @transform(inputs=("organization",), outputs=("person",), title="KRS: current extract and board", priority=0.5,
               accepts=lambda self, e, info: bool(re.fullmatch(r"\d{10}", str(e.get("registration_number") or "")))
               and str(e.get("jurisdiction") or "PL").upper().startswith("PL"))
    async def extract(self, entity, ctx):
        krs = str(entity.get("registration_number"))
        resp = await ctx.get(f"https://api-krs.ms.gov.pl/api/krs/OdpisAktualny/{krs}", params={"rejestr": "P", "format": "json"})
        if resp.status == 404:
            raise NotFound("No KRS entry with this number")
        d = ((ctx.json(ctx.expect(resp)) or {}).get("odpis") or {}).get("dane") or {}
        src = ctx.out.source(f"https://wyszukiwarka-krs.ms.gov.pl/details?krs={krs}", title="KRS extract", request=f"KRS {krs}")
        for p in ((d.get("dzial2") or {}).get("reprezentacja") or {}).get("sklad") or []:
            first = (p.get("imiona") or {}).get("imie", "")
            last = (p.get("nazwisko") or {}).get("nazwiskoICzlon", "")
            add_officer(ctx.out, src, entity.key, registry="pl-krs", record_id=f"{krs}:{first}{last}".casefold(),
                        name=f"{first} {last}", role=p.get("funkcjaWOrganie", ""))


@register
class BrasilCnpj(Connector):
    manifest = Manifest(
        name="brasil_cnpj", title="Brazil — CNPJ (Receita Federal open data)", category="companies",
        description="Brazilian company by CNPJ with partners/administrators (QSA), via the BrasilAPI mirror of Receita Federal "
                    "open data.", access="free", rate_limit=RateLimit(1.0, 2), website="https://brasilapi.com.br",
        documentation="https://brasilapi.com.br/docs#tag/CNPJ", reliability="high", public_record=True,
        jurisdictions=("BR",), supported_countries=("BR",),
        capabilities=("company by CNPJ", "partners and administrators with age bracket", "registered contact details"),
        disclosure=Disclosure(("brasilapi.com.br",), "the CNPJ number"),
    )

    @transform(inputs=("organization",), outputs=("person", "email", "phone"), title="CNPJ: company and partners",
               priority=0.5, accepts=lambda self, e, info: len(re.sub(r"\D", "", str(e.get("registration_number") or ""))) == 14)
    async def lookup(self, entity, ctx):
        cnpj = re.sub(r"\D", "", str(entity.get("registration_number")))
        resp = await ctx.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}")
        if resp.status == 404:
            raise NotFound("No company with this CNPJ")
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", title="CNPJ (Receita Federal)", request=f"CNPJ {cnpj}")
        ctx.out.props(entity.key, {"legal_name": d.get("razao_social"), "status": d.get("descricao_situacao_cadastral"),
                                   "incorporated": d.get("data_inicio_atividade"),
                                   "address": f"{d.get('municipio', '')}/{d.get('uf', '')}"}, source=src)
        for s in d.get("qsa") or []:
            add_officer(ctx.out, src, entity.key, registry="br-qsa", record_id=f"{cnpj}:{str(s.get('nome_socio')).casefold()}",
                        name=s.get("nome_socio") or "", role=s.get("qualificacao_socio") or "", start=s.get("data_entrada_sociedade"))
        if d.get("email"):
            ek = ctx.out.entity("email", d["email"], source=src)
            ctx.out.link(entity.key, ek, "USES", source=src, evidence="Contact email registered with the Receita Federal")


# --------------------------------------------------------------------------- Australia


@register
class AuAbn(Connector):
    manifest = Manifest(
        name="au_abn", title="Australia — ABN Lookup", category="companies",
        description="Australian Business Register: ABNs, entity and business names, state and postcode.", access="free_key",
        credentials=(Credential("ABN_LOOKUP_GUID", "ABN Lookup GUID", "Free authentication GUID after registering for the "
                                "web services.", obtain_url="https://abr.business.gov.au/Tools/WebServices"),),
        rate_limit=RateLimit(1.0, 2), website="https://abr.business.gov.au/", documentation="https://abr.business.gov.au/Tools/WebServices",
        reliability="official", public_record=True, jurisdictions=("AU",), supported_countries=("AU",),
        capabilities=("entities and business names by name", "ABN status", "state/postcode"),
        disclosure=Disclosure(("abr.business.gov.au",), "the organization name"),
    )

    @transform(inputs=("organization",), outputs=("organization",), title="ABN Lookup: Australian entities", priority=0.4,
               accepts=org_accepts("AU"))
    async def names(self, entity, ctx):
        (guid,) = ctx.require("ABN_LOOKUP_GUID")
        name = org_name(entity)
        resp = await ctx.get("https://abr.business.gov.au/json/MatchingNames.aspx",
                             params={"name": name, "maxResults": 10, "guid": guid})
        text = ctx.expect(resp).text
        m = re.search(r"\((\{.*\})\)\s*;?\s*$", text, re.S)
        if not m:
            raise SourceUnavailable("Unexpected ABN Lookup response")
        data = json.loads(m.group(1))
        if data.get("Message"):
            raise SourceUnavailable(data["Message"])
        for n in (data.get("Names") or [])[:8]:
            src = ctx.out.source(f"https://abr.business.gov.au/ABN/View?abn={n.get('Abn')}", title="ABN Lookup",
                                 request=f"name “{name}”")
            add_org(ctx.out, src, scheme="au-abn", number=n.get("Abn"), name=n.get("Name"), jurisdiction="AU",
                    props={"status": n.get("AbnStatus"), "address": f"{n.get('State', '')} {n.get('Postcode', '')}".strip()},
                    link_from=entity.key, query_name=name)

