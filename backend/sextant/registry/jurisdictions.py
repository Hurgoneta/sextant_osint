"""Worldwide registry of legitimate public sources, per jurisdiction.

Every country in the country registry (195 sovereign states + territories, ``registry/countries.py``) is a
jurisdiction here, including the ones with no known source: they appear with zero sources and with the
reason, instead of being left out. Two pseudo-jurisdictions hold sources that are not tied to a country
(``INT`` international, ``EU`` European Union).

Curated sources live in ``data/jurisdictions/*.toml`` (``[[country]]`` tables). A ``[[regional]]`` table
declares a source shared by several countries (PacLII, OHADA…): it is attached to *each* listed country
as its own entry, never to a continent-wide placeholder. ``[[bloc]]`` tables declare memberships (EU, EEA,
Council of Europe) so supranational sources are shown for each member state.

Availability differs by country, and the registry encodes that instead of assuming it:

* A company register may list directors by name in one country and only company numbers in another.
* Court judgments are searchable by party name in some countries and anonymised by law in many
  (Germany, France, Netherlands, Spain…). There, a name search is impossible by design.
* Criminal-record registers are **not public** almost anywhere. Only the data subject can request
  their own extract, and the registry says so instead of pretending otherwise.

Each source states its kind, access model, whether anyone can search it by a person's name, whether
an API exists, and which connector (if any) automates it. Everything else is a link for the
investigator. ``python -m sextant.tools.verify_catalog`` re-checks every URL; results are stored
in ``data/jurisdictions/_verification.json`` and shown in the UI.

Sources discovered from maintained datasets (GLEIF, OpenSanctions, Wikidata, IANA…) are kept apart in
``registry/discovered.py`` and labelled as such.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from . import countries as C

DATA = Path(__file__).resolve().parents[1] / "data" / "jurisdictions"

SOURCE_KINDS = {
    "company_registry": "Company / business register",
    "beneficial_ownership": "Beneficial-ownership register",
    "court_portal": "Court case portal (dockets / hearing lists)",
    "judgments": "Published judgments / case law",
    "court": "Court (official website)",
    "gazette": "Official gazette",
    "insolvency": "Insolvency / bankruptcy register",
    "sanctions": "Sanctions list",
    "pep": "Politically exposed persons list",
    "debarment": "Debarment / exclusion list",
    "wanted": "Wanted persons",
    "procurement": "Public procurement",
    "regulatory": "Regulatory register (financial, data protection, etc.)",
    "financial_regulator": "Financial / securities regulator",
    "central_bank": "Central bank",
    "stock_exchange": "Stock exchange (listed-company filings)",
    "data_protection": "Data-protection authority",
    "professional_register": "Professional register (lawyers, doctors, auditors…)",
    "bar_association": "Bar association / lawyer register",
    "licensing": "Licensing register",
    "legislation": "Legislation",
    "open_data": "Government open-data portal",
    "statistics": "National statistics office",
    "government_portal": "Government portal",
    "parliament": "Parliament / legislature",
    "tax_authority": "Tax authority",
    "anti_corruption": "Anti-corruption agency",
    "ombudsman": "Ombudsman",
    "property": "Land / property register",
    "elections": "Election & political-finance data",
    "election_authority": "Election authority",
    "charity_register": "Charity / non-profit register",
    "public_officials": "Public officials, asset & interest declarations",
    "lobbying": "Lobbying / transparency register",
    "intellectual_property": "Patents & trademarks",
    "offender_registry": "Public offender registry",
    "own_record": "Request your own criminal-record certificate (not public)",
    "people_directory": "Public people / phone directory",
    "business_directory": "Business directory",
    "chamber_of_commerce": "Chamber of commerce",
    "academic_directory": "Academic / research directory",
    "archives": "National archives",
    "library": "National library",
    "news_agency": "News agency",
    "news_media": "News media",
    "public_broadcaster": "Public broadcaster",
    "search_engine": "Local search engine",
    "forum": "Local forum / community",
    "domain_registry": "Domain (ccTLD) registry",
    "internet_registry": "Internet number registry (RIR)",
    "commercial_provider": "Commercial intelligence provider",
    "aggregator": "Cross-jurisdiction aggregator / investigative database",
}

# The source categories every country is evaluated against (what the coverage screens group by)
SOURCE_GROUPS = {
    "government": "Government",
    "corporate": "Corporate",
    "public_records": "Public records",
    "judicial": "Judicial / legal",
    "professional": "Professional",
    "regulatory": "Regulatory & sanctions",
    "procurement": "Procurement",
    "news": "News",
    "documents": "Documents & official publications",
    "archives": "Archives & libraries",
    "web": "Web & local search",
    "social": "Social & community",
    "domains": "Domains",
    "infrastructure": "Infrastructure",
    "commercial": "Commercial providers",
    "other": "Other",                  # sources the investigator adds in categories without an official-source group
}
KIND_GROUP = {
    "company_registry": "corporate", "beneficial_ownership": "corporate", "stock_exchange": "corporate",
    "chamber_of_commerce": "corporate", "business_directory": "corporate", "charity_register": "corporate",
    "intellectual_property": "corporate",
    "court_portal": "judicial", "judgments": "judicial", "court": "judicial",
    "insolvency": "public_records", "property": "public_records", "offender_registry": "public_records",
    "own_record": "public_records", "wanted": "public_records", "people_directory": "public_records",
    "aggregator": "public_records",
    "professional_register": "professional", "licensing": "professional", "bar_association": "professional",
    "academic_directory": "professional",
    "regulatory": "regulatory", "financial_regulator": "regulatory", "central_bank": "regulatory",
    "data_protection": "regulatory", "sanctions": "regulatory", "pep": "regulatory", "debarment": "regulatory",
    "procurement": "procurement",
    "news_agency": "news", "news_media": "news", "public_broadcaster": "news",
    "gazette": "documents", "legislation": "documents", "library": "archives", "archives": "archives",
    "government_portal": "government", "parliament": "government", "statistics": "government",
    "open_data": "government", "tax_authority": "government", "ombudsman": "government",
    "anti_corruption": "government", "elections": "government", "election_authority": "government",
    "public_officials": "government", "lobbying": "government",
    "search_engine": "web", "forum": "social",
    "domain_registry": "domains", "internet_registry": "infrastructure",
    "commercial_provider": "commercial",
}
# Groups that hold "public records" in the coverage matrix's Records column
RECORD_GROUPS = ("corporate", "public_records", "judicial", "regulatory", "procurement", "professional")

ACCESS = {
    "free_web": "Free, web search",
    "free_api": "Free API",
    "free_download": "Free bulk download",
    "registration": "Free with registration / API key",
    "paid": "Paid / subscription required",
    "on_request": "On request (not searchable online)",
    "restricted": "Restricted (legal interest / eID required)",
    "unknown": "Not yet reviewed",
}

REGIONS = ("International", *C.REGIONS)
PSEUDO = {"INT": "International", "EU": "European Union"}


@dataclass
class JurisdictionSource:
    id: str
    title: str
    kind: str
    url: str
    access: str = "free_web"
    name_search: bool = False          # can *anyone* search it by a person's name?
    person_data: str = ""              # which personal data appears (e.g. "directors' names, birth month/year")
    api: str = ""                      # API / documentation URL
    connector: str = ""                # connector automating it
    anonymized: bool = False           # published without party names
    language: str = ""
    notes: str = ""
    country: str = ""
    regional: str = ""                 # id of the regional body when shared by several countries
    provenance: str = "curated"        # curated | regional | bloc:<id>

    @property
    def group(self) -> str:
        return KIND_GROUP.get(self.kind, "government")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_title"] = SOURCE_KINDS.get(self.kind, self.kind)
        d["access_title"] = ACCESS.get(self.access, self.access)
        d["group"] = self.group
        d["group_title"] = SOURCE_GROUPS[self.group]
        return d


@dataclass
class Jurisdiction:
    code: str                          # ISO-3166-1 alpha-2, or INT / EU
    name: str
    region: str
    subregion: str = ""
    status: str = ""                   # country status (un_member, un_observer_state, territory…), "" for INT/EU
    legal_notes: str = ""
    criminal_records_public: bool = False
    judgments_anonymized: bool | None = None
    sources: list[JurisdictionSource] = field(default_factory=list)
    aliases: tuple[str, ...] = ()      # names used to recognise the country in free text
    blocs: tuple[str, ...] = ()        # EU, EEA, CoE…
    curated: bool = False              # a curated [[country]] table exists

    @property
    def sovereign(self) -> bool:
        return self.status in C.SOVEREIGN_STATUSES

    def to_dict(self, verification: dict | None = None) -> dict:
        v = verification or {}
        return {"code": self.code, "name": self.name, "region": self.region, "subregion": self.subregion,
                "status": self.status, "sovereign": self.sovereign, "blocs": list(self.blocs),
                "legal_notes": self.legal_notes, "criminal_records_public": self.criminal_records_public,
                "judgments_anonymized": self.judgments_anonymized,
                "sources": [{**s.to_dict(), "verification": v.get(s.id)} for s in self.sources],
                "counts": {k: sum(1 for s in self.sources if s.kind == k) for k in {s.kind for s in self.sources}},
                "automated": sum(1 for s in self.sources if s.connector)}


@dataclass
class Bloc:
    id: str
    title: str
    members: tuple[str, ...]
    jurisdiction: str = ""             # pseudo-jurisdiction holding the bloc's own sources (EU)
    url: str = ""


def _source(raw: dict, file: str, country: str, **extra) -> JurisdictionSource:
    src = JurisdictionSource(**{k: v for k, v in raw.items() if k in JurisdictionSource.__dataclass_fields__}, **extra)
    src.country = country
    if src.kind not in SOURCE_KINDS:
        raise ValueError(f"{file}: {src.id}: unknown kind {src.kind!r}")
    if src.access not in ACCESS:
        raise ValueError(f"{file}: {src.id}: unknown access {src.access!r}")
    return src


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Jurisdiction], dict[str, Bloc]]:
    registry = C.countries()
    out: dict[str, Jurisdiction] = {
        code: Jurisdiction(code=code, name=c.name, region=c.region, subregion=c.subregion, status=c.status,
                           aliases=c.aliases)
        for code, c in registry.items()}
    blocs: dict[str, Bloc] = {}
    regional: list[tuple[str, dict]] = []
    for file in sorted(DATA.glob("*.toml")):
        with open(file, "rb") as fh:
            doc = tomllib.load(fh)
        for raw in doc.get("country", []):
            code = raw["code"]
            j = out.get(code)
            if j is None:
                if code not in PSEUDO:
                    raise ValueError(f"{file.name}: {code!r} is neither a country in the country registry nor INT/EU "
                                     "— continent-wide placeholders are not allowed; use [[regional]] with a country list")
                j = out[code] = Jurisdiction(code=code, name=raw.get("name", PSEUDO[code]), region="International",
                                             subregion=raw.get("subregion", ""))
            first = not j.curated
            # several tables for one country (in one or more files) are merged
            j.curated = True
            if first or raw.get("legal_notes"):
                j.legal_notes = raw.get("legal_notes", j.legal_notes)
            if "criminal_records_public" in raw:
                j.criminal_records_public = bool(raw["criminal_records_public"])
            if "judgments_anonymized" in raw:
                j.judgments_anonymized = raw["judgments_anonymized"]
            j.aliases = tuple(dict.fromkeys([*j.aliases, *raw.get("aliases", ())]))
            j.sources.extend(_source(s, file.name, code) for s in raw.get("source", []))
        for raw in doc.get("regional", []):
            regional.append((file.name, raw))
        for raw in doc.get("bloc", []):
            blocs[raw["id"]] = Bloc(id=raw["id"], title=raw["title"], members=tuple(raw["members"]),
                                    jurisdiction=raw.get("jurisdiction", ""), url=raw.get("url", ""))
    for file, raw in regional:
        template = raw.get("country_url", "")
        for cc in raw["countries"]:
            if cc not in out:
                raise ValueError(f"{file}: regional source {raw['id']} lists unknown country {cc!r}")
            per = {k: v for k, v in raw.items() if k not in ("countries", "country_url", "id")}
            per["url"] = template.replace("{cc}", cc.lower()).replace("{CC}", cc) if template else raw["url"]
            out[cc].sources.append(_source({**per, "id": f"{raw['id']}-{cc.lower()}"}, file, cc,
                                           regional=raw["id"], provenance="regional"))
    for b in blocs.values():
        for cc in b.members:
            if cc not in out:
                raise ValueError(f"bloc {b.id} lists unknown country {cc!r}")
            out[cc].blocs = (*out[cc].blocs, b.id)
    for j in out.values():
        if j.region not in REGIONS:
            raise ValueError(f"{j.code}: unknown region {j.region!r}")
    ids = [s.id for j in out.values() for s in j.sources]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate jurisdiction source ids: {sorted(dupes)}")
    return out, blocs


def jurisdictions() -> dict[str, Jurisdiction]:
    return _load()[0]


def blocs() -> dict[str, Bloc]:
    return _load()[1]


def bloc_sources(code: str) -> list[tuple[Bloc, JurisdictionSource]]:
    """Supranational sources that apply to a member state (e.g. TED procurement for an EU member)."""
    js = jurisdictions()
    j = js.get(code)
    out = []
    for bid in (j.blocs if j else ()):
        b = blocs()[bid]
        if b.jurisdiction and b.jurisdiction in js:
            out.extend((b, s) for s in js[b.jurisdiction].sources)
    return out


def verification() -> dict:
    path = DATA / "_verification.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def all_sources() -> list[JurisdictionSource]:
    return [s for j in jurisdictions().values() for s in j.sources]


def country_names() -> dict[str, tuple[str, ...]]:
    """ISO-2 -> lower-case names/aliases used to recognise a country in free-text locations."""
    out = {}
    for code, j in jurisdictions().items():
        if len(code) == 2:
            out[code] = tuple(dict.fromkeys([j.name.lower(), *(a.lower() for a in j.aliases if len(a) >= 3)]))
    return out


def payload(countries: list[str] | None = None) -> dict:
    ver = verification()
    js = jurisdictions()
    relevant = [c for c in (countries or []) if c in js]
    regions: dict[str, list[dict]] = {r: [] for r in REGIONS}
    for j in sorted(js.values(), key=lambda j: (j.code not in PSEUDO, j.name)):
        regions.setdefault(j.region, []).append(j.to_dict(ver.get("sources")))
    srcs = all_sources()
    return {
        "regions": [{"region": r, "jurisdictions": items} for r, items in regions.items() if items],
        "relevant": relevant, "kinds": SOURCE_KINDS, "access": ACCESS, "groups": SOURCE_GROUPS,
        "verified_at": ver.get("checked_at"),
        "totals": {"jurisdictions": len(js), "countries": sum(1 for j in js.values() if j.sovereign),
                   "with_curated_sources": sum(1 for j in js.values() if j.sovereign and j.sources),
                   "sources": len(srcs), "automated": sum(1 for s in srcs if s.connector),
                   "name_searchable": sum(1 for s in srcs if s.name_search)},
    }
