"""Sanctions and wanted lists, matched **locally**: UN, EU, US OFAC, UK FCDO, FBI wanted list, and the OpenSanctions
bulk dataset (non-commercial licence).

Each list is published in full by its authority. The whole list is downloaded (no query — nothing about
the investigation is sent), a compact name index is cached for 24 h in the data folder, and names are
compared on this computer.

A match on a name is almost always a *namesake*. Records link only to the name string they carry, never
to the target, and the resolver keeps name-only matches at "possible name match".
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as SafeET

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NeedsCredentials, NotFound, SourceUnavailable, TransformError
from ...sdk.http import FetchError
from ...util.normalize import compare_names, fold, name_tokens
from .._common import add_record, person_names

MAX_LIST_BYTES = 80 * 1024 * 1024
REFRESH_SECONDS = 24 * 3600
_YEAR = re.compile(r"\b(19\d{2}|20[0-2]\d)\b")

Entry = dict  # {id, names[], birth_years[], nationality, programme, listed_on, url}


def _years(*texts) -> list[int]:
    out: list[int] = []
    for t in texts:
        for y in _YEAR.findall(str(t or "")):
            if int(y) not in out:
                out.append(int(y))
    return out


def _clean(names) -> list[str]:
    out = []
    for n in names:
        n = re.sub(r"\s+", " ", str(n or "")).strip(" ,;")
        if n and n not in out and len(name_tokens(n)) >= 2:
            out.append(n)
    return out


# --------------------------------------------------------------------------- parsers (pure)

def parse_un_xml(raw: bytes) -> list[Entry]:
    root = SafeET.fromstring(raw)              # defusedxml: no DTDs, no entity expansion
    out = []
    for ind in root.iter("INDIVIDUAL"):
        def t(tag: str, el=ind) -> str:
            return (el.findtext(tag) or "").strip()
        primary = " ".join(x for x in (t("FIRST_NAME"), t("SECOND_NAME"), t("THIRD_NAME"), t("FOURTH_NAME")) if x)
        aliases = [a.findtext("ALIAS_NAME") for a in ind.iter("INDIVIDUAL_ALIAS")]
        dob = [" ".join(filter(None, (d.findtext("DATE"), d.findtext("YEAR"), d.findtext("FROM_YEAR"), d.findtext("TO_YEAR"))))
               for d in ind.iter("INDIVIDUAL_DATE_OF_BIRTH")]
        nat = ind.find("NATIONALITY")
        out.append({
            "id": t("REFERENCE_NUMBER") or t("DATAID"), "names": _clean([primary, *aliases]), "birth_years": _years(*dob),
            "nationality": ", ".join(v.text for v in (nat.iter("VALUE") if nat is not None else []) if v.text),
            "programme": t("UN_LIST_TYPE"), "listed_on": t("LISTED_ON"),
            "url": "https://www.un.org/securitycouncil/content/un-sc-consolidated-list",
        })
    return [e for e in out if e["names"]]


def parse_ofac(sdn: bytes, alt: bytes | None) -> list[Entry]:
    aliases: dict[str, list[str]] = {}
    if alt:
        for row in csv.reader(io.StringIO(alt.decode("utf-8", "replace"))):
            if len(row) >= 4:
                aliases.setdefault(row[0].strip(), []).append(row[3])
    out = []
    for row in csv.reader(io.StringIO(sdn.decode("utf-8", "replace"))):
        if len(row) < 12 or row[2].strip().lower() != "individual":
            continue
        ent = row[0].strip()
        remarks = row[11]
        dob = " ".join(re.findall(r"DOB ([^;]+);", remarks))
        out.append({
            "id": ent, "names": _clean([row[1], *aliases.get(ent, [])]), "birth_years": _years(dob),
            "nationality": ", ".join(re.findall(r"nationality ([^;]+);", remarks)), "programme": row[3].strip(),
            "listed_on": None, "url": "https://sanctionssearch.ofac.treas.gov/",
        })
    return [e for e in out if e["names"]]


EU_BIRTH_COLUMNS = ("BirthDate_BirthDate", "BirthDate_Year", "BirthDate_YearRangeFrom", "BirthDate_YearRangeTo")


def parse_eu_csv(raw: bytes) -> list[Entry]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")), delimiter=";")
    people: dict[str, Entry] = {}
    for row in reader:
        if (row.get("Entity_SubjectType_ClassificationCode") or "").lower() != "person":
            continue
        eid = row.get("Entity_LogicalId") or ""
        e = people.setdefault(eid, {"id": row.get("Entity_EU_ReferenceNumber") or eid, "names": [], "birth_years": [],
                                    "nationality": "", "programme": row.get("Entity_Regulation_Programme") or "",
                                    "listed_on": row.get("Entity_DesignationDate") or row.get("Entity_Regulation_PublicationDate"),
                                    "url": "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions"})
        whole = row.get("NameAlias_WholeName") or " ".join(filter(None, (row.get("NameAlias_FirstName"), row.get("NameAlias_MiddleName"), row.get("NameAlias_LastName"))))
        if whole:
            e["names"].append(whole)
        for k in EU_BIRTH_COLUMNS:          # not BirthDate_Regulation_* (those are publication dates)
            e["birth_years"].extend(y for y in _years(row.get(k)) if y not in e["birth_years"])
        nat = row.get("Citizenship_CountryDescription") or row.get("Citizenship_CountryIso2Code")
        if nat and nat not in e["nationality"]:
            e["nationality"] = ", ".join(filter(None, (e["nationality"], nat)))
    out = []
    for e in people.values():
        e["names"] = _clean(e["names"])
        if e["names"]:
            out.append(e)
    return out


def parse_uk_csv(raw: bytes) -> list[Entry]:
    text = raw.decode("utf-8-sig", "replace")
    if text.startswith("Report Date"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    people: dict[str, Entry] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("Designation Type") or "").strip().lower() != "individual":
            continue
        uid = row.get("Unique ID") or ""
        e = people.setdefault(uid, {"id": uid, "names": [], "birth_years": [], "nationality": row.get("Nationality(/ies)") or "",
                                    "programme": row.get("Regime Name") or "", "listed_on": row.get("Date Designated"),
                                    "url": "https://www.gov.uk/government/publications/the-uk-sanctions-list"})
        name = " ".join(filter(None, (row.get(f"Name {i}") for i in (1, 2, 3, 4, 5, 6))))
        if name:
            e["names"].append(name)
        e["birth_years"].extend(y for y in _years(row.get("D.O.B")) if y not in e["birth_years"])
    out = []
    for e in people.values():
        e["names"] = _clean(e["names"])
        if e["names"]:
            out.append(e)
    return out


FBI_EXCLUDE = ("missing", "victim")


def parse_fbi_items(items: list[dict]) -> list[Entry]:
    out = []
    for it in items:
        if (it.get("poster_classification") or "").lower() in FBI_EXCLUDE:
            continue
        subjects = it.get("subjects") or []
        if any("unidentified" in s.lower() or "missing" in s.lower() for s in subjects):
            continue
        title = str(it.get("title") or "").split(" - ", 1)[0]
        out.append({
            "id": it.get("uid") or it.get("url"), "names": _clean([title, *(it.get("aliases") or [])]),
            "birth_years": _years(*(it.get("dates_of_birth_used") or [])), "nationality": it.get("nationality") or "",
            "programme": ", ".join(subjects), "listed_on": (it.get("publication") or "")[:10] or None, "url": it.get("url"),
        })
    return [e for e in out if e["names"]]


# --------------------------------------------------------------------------- store

class WatchlistStore:
    """Compact name indexes of public lists in <data_dir>/watchlists (no personal data)."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "watchlists"

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load(self, name: str) -> dict | None:
        try:
            return json.loads(self.path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, name: str, source: str, entries: list[Entry]) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        doc = {"fetched_at": time.time(), "source": source, "count": len(entries), "entries": entries}
        tmp = self.path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path(name))
        return doc


# --------------------------------------------------------------------------- connectors


@dataclass(frozen=True)
class ListSpec:
    name: str
    title: str
    publisher: str
    host: str
    urls: tuple[str, ...]
    fetch: str                                   # files | fbi
    parse: Callable[..., list[Entry]]
    approx_size: str
    jurisdiction: str
    website: str
    record_type: str = "sanctions_listing"
    license: str = "Public sector information"


def match_entries(doc: dict, names: list[str]) -> list[tuple[dict, str, str]]:
    out = []
    for seed_name in names:
        rare = max(name_tokens(seed_name), key=len, default="")
        for entry in doc["entries"]:
            if rare and rare not in fold(" ".join(entry["names"])):
                continue                                      # cheap pre-filter
            for listed in entry["names"]:
                how = compare_names(seed_name, listed)
                if how:
                    out.append((entry, listed, how))
                    break
    return out


def make_list_connector(spec: ListSpec) -> None:
    class _List(Connector):
        manifest = Manifest(
            name=spec.name, title=spec.title, category="sanctions",
            description=f"Downloads the complete {spec.publisher} list ({spec.approx_size}, refreshed daily) and matches "
                        f"names on this computer.", access="free", rate_limit=RateLimit(2, 4), website=spec.website,
            reliability="official", public_record=True, jurisdictions=(spec.jurisdiction,), license=spec.license,
            official_api=True, access_method="dataset", timeout=900,
            capabilities=("listed persons with the same or containing name", "aliases, years of birth, nationality",
                          "programme / regime", "official listing reference"),
            disclosure=Disclosure((spec.host,), "nothing about the investigation — the full public list is downloaded",
                                  "download"),
        )

        async def _download(self, ctx) -> list[Entry]:
            if spec.fetch == "fbi":
                items: list[dict] = []
                for page in range(1, 60):
                    resp = await ctx.get(spec.urls[0], params={"pageSize": 50, "page": page}, cache_ttl=None)
                    if resp.status != 200:
                        raise SourceUnavailable(f"{spec.host} answered HTTP {resp.status}")
                    batch = (resp.json() or {}).get("items") or []
                    items.extend(batch)
                    if len(batch) < 50:
                        break
                return spec.parse(items)
            blobs = []
            for url in spec.urls:
                resp = await ctx.get(url, cache_ttl=None, max_bytes=MAX_LIST_BYTES, retries=1)
                if resp.status != 200 or resp.truncated:
                    raise SourceUnavailable(f"Could not download the list from {spec.host} (HTTP {resp.status})")
                blobs.append(resp.content)
            return spec.parse(*blobs)

        @transform(inputs=("person",), outputs=("public_record", "name"), title=f"{spec.title}: name match (local)",
                   priority=0.7, accepts=lambda self, e, info: bool(person_names(e, info)),
                   sends="nothing (the list is downloaded and matched locally)")
        async def match(self, entity, ctx):
            if ctx.data_dir is None:
                raise SourceUnavailable("No local data folder for list storage")
            store = WatchlistStore(ctx.data_dir)
            doc = store.load(spec.name)
            if doc is None or time.time() - doc.get("fetched_at", 0) > REFRESH_SECONDS:
                try:
                    doc = store.save(spec.name, spec.urls[0], await self._download(ctx))
                except (TransformError, FetchError, ValueError, ParseError) as exc:
                    if doc is None:
                        raise SourceUnavailable(f"List unavailable: {exc}") from None
            names = person_names(entity, ctx.info)
            hits = match_entries(doc, names)
            ctx.out.set_status("found" if hits else "empty",
                               f"{doc['count']:,} listed persons checked locally — {len(hits) or 'no'} name match(es)")
            for entry, listed, how in hits:
                src = ctx.out.source(entry.get("url") or spec.website, title=f"{spec.title} entry {entry.get('id')}",
                                     kind="dataset", request="none (local match)")
                add_record(ctx.out, src, key=f"{spec.name}:{entry['id']}", title=f"{spec.title}: {listed}",
                           record_type=spec.record_type, jurisdiction=spec.jurisdiction, authority=spec.publisher,
                           official_url=entry.get("url") or spec.website, matched_name=listed, name_match=how,
                           query_name=names[0],
                           props={"reference": entry.get("id"), "date": entry.get("listed_on"),
                                  "birth_years": entry.get("birth_years"), "nationality": entry.get("nationality"),
                                  "category": entry.get("programme"), "list_name": spec.title,
                                  "summary": "A name match is usually a namesake. Banks screen against this list, so a "
                                             "namesake can still cause payment delays."})

    _List.__name__ = f"{spec.name.title().replace('_', '')}List"
    register(_List())


SPECS = (
    ListSpec("un_sanctions", "UN sanctions list", "UN Security Council", "scsanctions.un.org",
             ("https://scsanctions.un.org/resources/xml/en/consolidated.xml",), "files", parse_un_xml, "≈2 MB", "INT",
             "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"),
    ListSpec("eu_sanctions", "EU sanctions list", "European Commission", "webgate.ec.europa.eu",
             ("https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",),
             "files", parse_eu_csv, "≈25 MB", "EU",
             "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions"),
    ListSpec("ofac_sdn", "US OFAC SDN list", "US Treasury (OFAC)", "sanctionslistservice.ofac.treas.gov",
             ("https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
              "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ALT.CSV"),
             "files", parse_ofac, "≈7 MB", "US", "https://sanctionslist.ofac.treas.gov/"),
    ListSpec("uk_sanctions", "UK sanctions list", "UK FCDO", "sanctionslist.fcdo.gov.uk",
             ("https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",), "files", parse_uk_csv, "≈50 MB", "GB",
             "https://www.gov.uk/government/publications/the-uk-sanctions-list", license="Open Government Licence"),
    ListSpec("fbi_wanted", "FBI wanted list", "FBI", "api.fbi.gov", ("https://api.fbi.gov/wanted/v1/list",), "fbi",
             parse_fbi_items, "≈1,300 entries, ~26 requests", "US", "https://www.fbi.gov/wanted", record_type="wanted_notice"),
)

for _spec in SPECS:
    make_list_connector(_spec)


# --------------------------------------------------------------------------- OpenSanctions

OS_BULK = "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"


def parse_opensanctions_csv(path: Path) -> list[Entry]:
    """targets.simple.csv → persons only (streamed; the file is ~74 MB)."""
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            if (row.get("schema") or "") != "Person":
                continue
            names = _clean([row.get("name"), *str(row.get("aliases") or "").split(";")][:10])
            if not names:
                continue
            out.append({"id": row.get("id"), "names": names, "birth_years": _years(row.get("birth_date")),
                        "nationality": row.get("countries") or "", "programme": row.get("sanctions") or row.get("dataset") or "",
                        "listed_on": (row.get("first_seen") or "")[:10] or None,
                        "url": f"https://www.opensanctions.org/entities/{row.get('id')}/"})
    return out


@register
class OpenSanctionsBulk(Connector):
    manifest = Manifest(
        name="opensanctions_bulk", title="OpenSanctions (bulk, local match)", category="sanctions",
        description="Downloads OpenSanctions' consolidated sanctions dataset (≈74 MB, 200+ sources) and matches names on "
                    "this computer.", access="free", rate_limit=RateLimit(1, 1), website="https://www.opensanctions.org",
        documentation="https://www.opensanctions.org/docs/bulk/", reliability="high", public_record=True,
        license="CC BY-NC 4.0 — non-commercial use only", access_method="dataset", timeout=900, default_enabled=False,
        legal="The bulk data is licensed for non-commercial use; commercial users need an OpenSanctions licence.",
        opt_in_reason="74 MB download and a non-commercial licence — enable it if both are fine for you.",
        capabilities=("sanctioned persons from 200+ official lists", "aliases, birth years, nationality"),
        disclosure=Disclosure(("data.opensanctions.org",), "nothing about the investigation — the dataset is downloaded",
                              "download"),
    )

    @transform(inputs=("person",), outputs=("public_record", "name"), title="OpenSanctions: name match (local)",
               priority=0.6, accepts=lambda self, e, info: bool(person_names(e, info)), sends="nothing (local match)")
    async def match(self, entity, ctx):
        if ctx.data_dir is None:
            raise SourceUnavailable("No local data folder")
        store = WatchlistStore(ctx.data_dir)
        doc = store.load("opensanctions")
        if doc is None or time.time() - doc.get("fetched_at", 0) > REFRESH_SECONDS:
            raw = ctx.data_dir / "watchlists" / "opensanctions.csv"
            try:
                await ctx.download(OS_BULK, raw, max_bytes=250 * 1024 * 1024)
                doc = store.save("opensanctions", OS_BULK, parse_opensanctions_csv(raw))
            except (TransformError, FetchError, ValueError, OSError) as exc:
                if doc is None:
                    raise SourceUnavailable(f"Dataset unavailable: {exc}") from None
            finally:
                raw.unlink(missing_ok=True)
        names = person_names(entity, ctx.info)
        hits = match_entries(doc, names)
        ctx.out.set_status("found" if hits else "empty", f"{doc['count']:,} sanctioned persons checked locally")
        for entry, listed, how in hits:
            src = ctx.out.source(entry["url"], title="OpenSanctions entity", kind="dataset", request="none (local match)")
            add_record(ctx.out, src, key=f"opensanctions:{entry['id']}", title=f"OpenSanctions: {listed}",
                       record_type="sanctions_listing", jurisdiction="INT", authority="OpenSanctions (aggregated)",
                       official_url=entry["url"], matched_name=listed, name_match=how, query_name=names[0],
                       props={"reference": entry["id"], "birth_years": entry["birth_years"], "nationality": entry["nationality"],
                              "category": entry["programme"], "date": entry["listed_on"]})


@register
class OpenSanctionsApi(Connector):
    manifest = Manifest(
        name="opensanctions", title="OpenSanctions API", category="sanctions",
        description="Screens a name against 200+ sanctions, wanted-person and politically-exposed-person (PEP) sources.",
        access="free_key",
        credentials=(Credential("OPENSANCTIONS_API_KEY", "OpenSanctions API key", "Free for journalism, civil society and "
                                "academia (on application); commercial use is metered (€0.10/query).",
                                obtain_url="https://www.opensanctions.org/api/"),),
        cost="Free (public interest) / €0.10 per query", cost_per_call_usd=0.11, rate_limit=RateLimit(1, 2),
        website="https://www.opensanctions.org", documentation="https://api.opensanctions.org/", reliability="high",
        public_record=True, license="CC BY-NC 4.0 data; API terms apply",
        capabilities=("sanctions listings", "PEP positions", "wanted notices", "aliases, birth dates, nationality"),
        disclosure=Disclosure(("api.opensanctions.org",), "the full name"),
    )

    @transform(inputs=("person",), outputs=("public_record", "name"), title="OpenSanctions API: screening (incl. PEPs)",
               priority=0.6, accepts=lambda self, e, info: bool(person_names(e, info)))
    async def search(self, entity, ctx):
        (key,) = ctx.require("OPENSANCTIONS_API_KEY")
        name = person_names(entity, ctx.info)[0]
        resp = await ctx.get("https://api.opensanctions.org/search/default", params={"q": name, "limit": 20},
                             headers={"Authorization": f"ApiKey {key}", "Accept": "application/json"})
        if resp.status in (401, 403):
            raise NeedsCredentials("OpenSanctions rejected the API key")
        hits = 0
        for r in (ctx.json(ctx.expect(resp)) or {}).get("results") or []:
            if r.get("schema") != "Person":
                continue
            props = r.get("properties") or {}
            listed_names = (props.get("name") or [r.get("caption") or ""]) + (props.get("alias") or [])
            match = next(((n, m) for n in listed_names for m in [compare_names(name, n)] if m), None)
            if not match:
                continue
            hits += 1
            url = f"https://www.opensanctions.org/entities/{r.get('id')}/"
            src = ctx.out.source(url, title="OpenSanctions entity", request=f"name “{name}”")
            add_record(ctx.out, src, key=f"opensanctions:{r.get('id')}", title=f"OpenSanctions: {r.get('caption')}",
                       record_type="pep_or_sanctions" if "role.pep" in (props.get("topics") or []) else "sanctions_listing",
                       jurisdiction="INT", authority="OpenSanctions", official_url=url, matched_name=match[0],
                       name_match=match[1], query_name=name,
                       props={"reference": r.get("id"), "birth_years": _years(*(props.get("birthDate") or [])),
                              "nationality": ", ".join(props.get("nationality") or props.get("country") or []),
                              "category": ", ".join(props.get("topics") or []),
                              "list_name": ", ".join(r.get("datasets") or [])[:200]})
        if not hits:
            raise NotFound("No listed person with this name")
