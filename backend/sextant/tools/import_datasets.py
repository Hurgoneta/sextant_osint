"""Builds the global registries from maintained public datasets (data-driven source discovery).

    python -m sextant.tools.import_datasets all
    python -m sextant.tools.import_datasets countries iana rir gleif opensanctions wikidata gdelt platforms
    python -m sextant.tools.import_datasets --cache-dir ~/.cache/sextant-import all     # reuse downloads

Outputs (each records the dataset, its URL, its licence and when it was retrieved):

    data/countries/countries.json        UN M49 + Wikidata (names, ISO codes, regions, languages, FIPS)
    data/discovered/iana_cctld.json      IANA root zone database + RDAP bootstrap (ccTLD registries)
    data/discovered/rir.json             RIR delegated statistics (internet number registry per country)
    data/discovered/gleif_ra.json        GLEIF Registration Authorities list (business registers worldwide)
    data/discovered/opensanctions.json   OpenSanctions dataset index (official sanctions/PEP/debarment publishers)
    data/discovered/wikidata.json        Wikidata (CC0): gazettes, statistics offices, courts, parliaments, archives…
    data/discovered/gdelt.json           GDELT source-country lookup (news filtered by country of the outlet)
    data/discovered/gleif_counts.json    number of LEI records per country (GLEIF API)
    data/platforms/imported/*.json       WhatsMyName, Sherlock and Maigret site lists (metadata only)

Discovered entries are *potential* sources: they come from a maintained list, not from a person
reviewing them, and the registry and UI label them that way. ``verify_catalog`` checks their URLs.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import tomllib
from datetime import datetime, timezone
from hashlib import sha1
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import USER_AGENT

DATA = Path(__file__).resolve().parents[1] / "data"
DISCOVERED = DATA / "discovered"
COUNTRIES = DATA / "countries"
IMPORTED_PLATFORMS = DATA / "platforms" / "imported"

URLS = {
    "m49": "https://unstats.un.org/unsd/methodology/m49/overview/",
    "wikidata_sparql": "https://query.wikidata.org/sparql",
    "iana_root": "https://www.iana.org/domains/root/db",
    "iana_rdap": "https://data.iana.org/rdap/dns.json",
    "gleif_ra_page": "https://www.gleif.org/en/about-lei/code-lists/gleif-registration-authorities-list",
    "opensanctions": "https://data.opensanctions.org/datasets/latest/index.json",
    "gdelt_countries": "http://data.gdeltproject.org/api/v2/guides/LOOKUP-COUNTRIES.TXT",
    "gleif_api": "https://api.gleif.org/api/v1/lei-records",
    "whatsmyname": "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json",
    "sherlock": "https://raw.githubusercontent.com/sherlock-project/sherlock/master/sherlock_project/resources/data.json",
    "maigret": "https://raw.githubusercontent.com/soxoj/maigret/main/maigret/resources/data.json",
}
RIR_STATS = {
    "RIPE NCC": "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest",
    "ARIN": "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "APNIC": "https://ftp.apnic.net/stats/apnic/delegated-apnic-extended-latest",
    "LACNIC": "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest",
    "AFRINIC": "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-extended-latest",
}
RIR_SITES = {"RIPE NCC": "https://www.ripe.net/", "ARIN": "https://www.arin.net/", "APNIC": "https://www.apnic.net/",
             "LACNIC": "https://www.lacnic.net/", "AFRINIC": "https://afrinic.net/"}
LICENSES = {
    "m49": "UN Statistics Division — free to use with attribution (UN terms of use)",
    "wikidata": "CC0 1.0 (Wikidata)",
    "iana": "IANA root zone database / RDAP bootstrap registry — public registry data",
    "rir": "RIR delegated statistics — public statistics published by the five RIRs",
    "gleif": "GLEIF Registration Authorities list — published free of charge by GLEIF",
    "opensanctions": "OpenSanctions dataset metadata — CC BY-NC 4.0 (attribution: OpenSanctions.org)",
    "gdelt": "GDELT Project lookup tables — free and open, with attribution",
    "whatsmyname": "CC BY-SA 4.0 — WhatsMyName by WebBreacher and contributors",
    "sherlock": "MIT — Sherlock Project contributors",
    "maigret": "MIT — Maigret (soxoj) contributors",
}

# The 193 UN member states. The Holy See and the State of Palestine (UN observer states) complete the 195.
UN_MEMBERS = set("""
AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI CV KH CM CA CF TD CL CN CO KM CG
CR CI HR CU CY CZ KP CD DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN GW GY HT HN HU IS IN
ID IR IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV LB LS LR LY LI LT LU MG MW MY MV ML MT MH MR MU MX FM MD MC MN ME MA
MZ MM NA NR NP NL NZ NI NE NG MK NO OM PK PW PA PG PY PE PH PL PT QA KR RO RU RW KN LC VC WS SM ST SA SN RS SC SL SG
SK SI SB SO ZA SS ES LK SD SR SE CH SY TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US UY UZ VU VE VN YE ZM ZW
""".split())
OBSERVER_STATES = {"VA", "PS"}


# --------------------------------------------------------------------------- download helpers

class Fetcher:
    def __init__(self, cache_dir: Path | None, delay: float = 0.0) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self.client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(90.0), trust_env=False,
                                   headers={"User-Agent": USER_AGENT})

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None, retries: int = 3) -> bytes:
        key = sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
        if self.cache_dir:
            cached = self.cache_dir / key
            if cached.exists():
                return cached.read_bytes()
        last = None
        for attempt in range(retries):
            try:
                resp = self.client.get(url, params=params, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    wait = resp.headers.get("retry-after", "")
                    time.sleep(min(int(wait), 300) if wait.isdigit() else 20 * (attempt + 1))
                    continue
                resp.raise_for_status()
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    (self.cache_dir / key).write_bytes(resp.content)
                if self.delay:
                    time.sleep(self.delay)
                return resp.content
            except httpx.HTTPError as exc:
                last = str(exc)
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Could not download {url}: {last}")

    def sparql(self, query: str) -> list[dict]:
        raw = self.get(URLS["wikidata_sparql"], params={"query": query},
                       headers={"Accept": "application/sparql-results+json"}, retries=6)
        out = []
        for b in json.loads(raw)["results"]["bindings"]:
            out.append({k: v["value"] for k, v in b.items()})
        time.sleep(5.0)
        return out


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write(path: Path, dataset: str, source_url: str | list[str], items, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"dataset": dataset, "source_url": source_url, "license": LICENSES[dataset.split(":")[0]],
           "retrieved_at": now(), **extra, "items": items}
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    n = len(items) if hasattr(items, "__len__") else "?"
    print(f"  wrote {path.relative_to(DATA.parent)} ({n} items)")


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    return u


# --------------------------------------------------------------------------- countries

class _TableParser(HTMLParser):
    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id, self.inside, self.rows, self.row, self.cell = table_id, False, [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table" and dict(attrs).get("id") == self.table_id:
            self.inside = True
        if not self.inside:
            return
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = ""

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.inside = False

    def handle_data(self, data):
        if self.inside and self.cell is not None:
            self.cell += data


def m49_rows(f: Fetcher) -> list[dict]:
    p = _TableParser("downloadTableEN")
    p.feed(f.get(URLS["m49"]).decode("utf-8", "replace"))
    header, *rows = p.rows
    return [dict(zip(header, r)) for r in rows if len(r) == len(header)]


def import_countries(f: Fetcher) -> None:
    overrides = tomllib.loads((COUNTRIES / "overrides.toml").read_text(encoding="utf-8"))
    rows = {r["ISO-alpha2 Code"]: r for r in m49_rows(f) if r.get("ISO-alpha2 Code")}
    wd = {r["iso"]: r for r in f.sparql("""
        SELECT ?iso (SAMPLE(?en) AS ?name) (SAMPLE(?fips) AS ?fips) (SAMPLE(?q) AS ?qid)
               (GROUP_CONCAT(DISTINCT ?nat; separator="|") AS ?natives)
               (GROUP_CONCAT(DISTINCT ?lc; separator="|") AS ?langs) WHERE {
          ?q wdt:P297 ?iso. FILTER NOT EXISTS { ?q wdt:P576 [] }
          OPTIONAL { ?q rdfs:label ?en FILTER(lang(?en) = "en") }
          OPTIONAL { ?q wdt:P901 ?fips }
          OPTIONAL { ?q wdt:P1705 ?nat }
          OPTIONAL { ?q wdt:P37 ?l. ?l wdt:P218 ?lc }
        } GROUP BY ?iso""")}
    names = overrides.get("names", {})
    regions = overrides.get("regions", {})
    subregions = overrides.get("subregions", {})
    extra_aliases = overrides.get("aliases", {})
    territories = overrides.get("territories", {})
    tld_exceptions = overrides.get("tld", {})
    fips_over = overrides.get("fips", {})
    lang_over = overrides.get("languages", {})
    sovereign = UN_MEMBERS | OBSERVER_STATES
    missing = sorted(c for c in sovereign if c not in rows)
    if missing:
        raise SystemExit(f"UN M49 table lacks {missing} — check the source before regenerating")
    out = []
    for code in sorted(sovereign | set(territories)):
        r = rows.get(code, {})
        w = wd.get(code, {})
        t = territories.get(code, {})
        m49_name = r.get("Country or Area") or t.get("name") or w.get("name") or code
        name = names.get(code) or t.get("name") or m49_name
        region_m49 = r.get("Region Name", "")
        sub = r.get("Intermediate Region Name") or r.get("Sub-region Name") or t.get("subregion", "")
        region = regions.get(code) or t.get("region") or {
            "Africa": "Africa", "Europe": "Europe", "Oceania": "Oceania"}.get(region_m49) or _americas_asia(r)
        aliases = [a for a in dict.fromkeys(
            [m49_name, w.get("name", ""), *(w.get("natives", "").split("|")), *extra_aliases.get(code, [])])
            if a and a.lower() != name.lower()]
        status = ("un_member" if code in UN_MEMBERS else "un_observer_state" if code in OBSERVER_STATES
                  else t.get("status", "territory"))
        out.append({
            "code": code, "iso3": r.get("ISO-alpha3 Code") or t.get("iso3", ""), "m49": r.get("M49 Code", ""),
            "name": name, "status": status, "region": region, "subregion": subregions.get(code) or sub,
            "aliases": aliases, "languages": lang_over.get(code) or sorted(x for x in w.get("langs", "").split("|") if x),
            "tld": tld_exceptions.get(code, "." + code.lower()), "fips": fips_over.get(code) or w.get("fips", ""), "wikidata": (w.get("qid") or "").rsplit("/", 1)[-1],
            "ldc": bool(r.get("Least Developed Countries (LDC)")), "lldc": bool(r.get("Land Locked Developing Countries (LLDC)")),
            "sids": bool(r.get("Small Island Developing States (SIDS)")),
            **({"sovereign_of": t["sovereign_of"]} if t.get("sovereign_of") else {}),
        })
    n = sum(1 for c in out if c["status"] in ("un_member", "un_observer_state"))
    if n != 195:
        raise SystemExit(f"Expected 195 sovereign states, built {n}")
    path = COUNTRIES / "countries.json"
    doc = {"dataset": "m49+wikidata", "source_url": [URLS["m49"], URLS["wikidata_sparql"]],
           "license": f"{LICENSES['m49']}; {LICENSES['wikidata']}", "retrieved_at": now(),
           "note": "193 UN member states + 2 UN observer states (Holy See, State of Palestine) = 195. Territories and "
                   "partially recognised states with their own registers are listed separately and never counted "
                   "among the 195.", "items": out}
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(DATA.parent)} ({len(out)} entries, {n} sovereign states)")


def _americas_asia(r: dict) -> str:
    sub, inter = r.get("Sub-region Name", ""), r.get("Intermediate Region Name", "")
    if sub == "Northern America":
        return "North America"
    if inter == "South America":
        return "South America"
    if inter in ("Caribbean", "Central America"):
        return "Central America & Caribbean"
    if sub == "Western Asia":
        return "Middle East"
    return "Asia"


def country_codes() -> dict[str, dict]:
    doc = json.loads((COUNTRIES / "countries.json").read_text(encoding="utf-8"))
    return {c["code"]: c for c in doc["items"]}


# --------------------------------------------------------------------------- IANA

class _RootDbParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows, self.row, self.cell, self.in_tbody = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self.in_tbody = True
        elif self.in_tbody and tag == "tr":
            self.row = []
        elif self.row is not None and tag == "td":
            self.cell = ""

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None and self.row is not None:
            self.row.append(" ".join(self.cell.split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data


def import_iana(f: Fetcher, details: bool = True) -> None:
    countries = country_codes()
    p = _RootDbParser()
    p.feed(f.get(URLS["iana_root"]).decode("utf-8", "replace"))
    rdap = json.loads(f.get(URLS["iana_rdap"]))
    rdap_by_tld = {t: svc[1] for svc in rdap.get("services", []) for t in svc[0]}
    tld_to_cc = {c["tld"].lstrip("."): code for code, c in countries.items()}
    items = {}
    for row in p.rows:
        if len(row) < 3:
            continue
        tld, typ, manager = row[0].lstrip("."), row[1], row[2]
        if typ != "country-code" or tld not in tld_to_cc or tld.startswith("xn--"):
            continue
        entry = {"tld": "." + tld, "country": tld_to_cc[tld], "manager": manager,
                 "iana_url": f"https://www.iana.org/domains/root/db/{tld}.html",
                 "rdap": rdap_by_tld.get(tld, []), "registry_url": "", "whois": ""}
        if details:
            html = f.get(entry["iana_url"]).decode("utf-8", "replace")
            m = re.search(r"URL for registration services:</b>\s*<a href=\"([^\"]+)\"", html)
            entry["registry_url"] = m.group(1) if m else ""
            m = re.search(r"WHOIS Server:</b>\s*([A-Za-z0-9.-]+)", html)
            entry["whois"] = m.group(1) if m else ""
            time.sleep(0.4)
        items[entry["country"]] = entry
    write(DISCOVERED / "iana_cctld.json", "iana", [URLS["iana_root"], URLS["iana_rdap"]], items,
          rdap_bootstrap_publication=rdap.get("publication"))


# --------------------------------------------------------------------------- RIR delegated statistics

def import_rir(f: Fetcher) -> None:
    items: dict[str, dict] = {}
    for rir, url in RIR_STATS.items():
        for line in f.get(url).decode("utf-8", "replace").splitlines():
            parts = line.split("|")
            if len(parts) < 7 or parts[0].startswith("#") or parts[1] in ("*", "") or parts[2] not in ("asn", "ipv4", "ipv6"):
                continue
            if parts[6] not in ("allocated", "assigned"):
                continue
            cc = parts[1].upper()
            e = items.setdefault(cc, {"rir": {}, "asn": 0, "ipv4_addresses": 0, "ipv6_blocks": 0})
            e["rir"][rir] = e["rir"].get(rir, 0) + 1
            if parts[2] == "asn":
                e["asn"] += int(parts[4])
            elif parts[2] == "ipv4":
                e["ipv4_addresses"] += int(parts[4])
            else:
                e["ipv6_blocks"] += 1
    for cc, e in items.items():
        primary = max(e["rir"].items(), key=lambda kv: kv[1])[0]
        e["primary_rir"], e["rir_url"] = primary, RIR_SITES[primary]
        e["rir"] = sorted(e["rir"])
    write(DISCOVERED / "rir.json", "rir", list(RIR_STATS.values()), items)


# --------------------------------------------------------------------------- GLEIF registration authorities

def import_gleif(f: Fetcher) -> None:
    page = f.get(URLS["gleif_ra_page"]).decode("utf-8", "replace")
    links = re.findall(r'href="([^"]+_ra-list-v[0-9.]+\.csv)"', page)
    if not links:
        raise RuntimeError("GLEIF page no longer links a RA list CSV")
    url = links[0] if links[0].startswith("http") else "https://www.gleif.org" + links[0]
    text = f.get(url).decode("utf-8-sig", "replace")
    items = []
    for r in csv.DictReader(io.StringIO(text)):
        cc = (r.get("Country Code") or "").strip().upper()
        if not cc:
            continue
        items.append({"ra_code": r["Registration Authority Code"], "country": cc[:2],
                      "jurisdiction": r.get("Jurisdiction (country or region)", ""),
                      "jurisdiction_code": cc, "register": r.get("International name of Register", ""),
                      "register_local": r.get("Local name of Register", ""),
                      "organization": r.get("International name of organisation responsible for the Register", ""),
                      "organization_local": r.get("Local name of organisation responsible for the Register", ""),
                      "website": norm_url(r.get("Website", "")), "comments": (r.get("Comments") or "")[:300]})
    write(DISCOVERED / "gleif_ra.json", "gleif", url, items)


def import_gleif_counts(f: Fetcher) -> None:
    items = {}
    for code in sorted(country_codes()):
        raw = f.get(URLS["gleif_api"], params={"filter[entity.legalAddress.country]": code, "page[size]": "1"})
        items[code] = json.loads(raw).get("meta", {}).get("pagination", {}).get("total", 0)
        time.sleep(0.3)
    write(DISCOVERED / "gleif_counts.json", "gleif:counts", URLS["gleif_api"], items)


# --------------------------------------------------------------------------- OpenSanctions dataset index

OS_TAG_KIND = (("list.sanction", "sanctions"), ("list.pep", "pep"), ("list.debarment", "debarment"),
               ("list.wanted", "wanted"), ("list.risk", "regulatory"), ("list.regulatory", "regulatory"),
               ("sector.maritime", "regulatory"), ("list.export", "sanctions"), ("juris.court", "judgments"),
               ("list.crime", "wanted"), ("list.disqualified", "regulatory"))


def import_opensanctions(f: Fetcher) -> None:
    doc = json.loads(f.get(URLS["opensanctions"]))
    items = []
    for d in doc.get("datasets", []):
        if d.get("type") != "source" or d.get("deprecated") or d.get("disabled") or d.get("hidden"):
            continue
        pub = d.get("publisher") or {}
        tags = d.get("tags") or []
        kind = next((k for t, k in OS_TAG_KIND if t in tags), "aggregator")
        country = (pub.get("country") or "").upper()
        items.append({"name": d["name"], "title": d.get("title", ""), "summary": (d.get("summary") or "")[:400],
                      "url": d.get("url") or pub.get("url") or "", "dataset_url": f"https://www.opensanctions.org/datasets/{d['name']}/",
                      "publisher": pub.get("name", ""), "publisher_url": pub.get("url", ""),
                      "official": bool(pub.get("official")), "country": country if len(country) == 2 else "",
                      "coverage_countries": [c.upper() for c in (d.get("coverage") or {}).get("countries", []) if len(c) == 2],
                      "kind": kind, "tags": tags, "collections": d.get("collections") or [],
                      "entity_count": d.get("entity_count") or 0, "target_count": d.get("target_count") or 0,
                      "updated_at": d.get("updated_at") or d.get("last_change") or ""})
    write(DISCOVERED / "opensanctions.json", "opensanctions", URLS["opensanctions"], items)


# --------------------------------------------------------------------------- Wikidata institutions

WIKIDATA_CLASSES: dict[str, tuple[tuple[str, ...], int]] = {
    # kind: (classes, max per country)
    "gazette": (("Q2065227",), 3),
    "statistics": (("Q480242",), 2),
    "company_registry": (("Q1394657", "Q1428914"), 3),
    "central_bank": (("Q66344",), 1),
    "data_protection": (("Q3242920",), 2),
    "election_authority": (("Q935741",), 2),
    "archives": (("Q2122214",), 2),
    "library": (("Q22806",), 2),
    "court": (("Q190752", "Q32766", "Q1767520"), 3),
    "anti_corruption": (("Q4774348",), 2),
    "ombudsman": (("Q169180",), 1),
    "tax_authority": (("Q573607",), 1),
    "intellectual_property": (("Q1148446",), 2),
    "stock_exchange": (("Q11691",), 3),
    "news_agency": (("Q192283",), 3),
    "public_broadcaster": (("Q1126006",), 2),
    "open_data": (("Q27031827",), 3),
    "bar_association": (("Q1865205",), 2),
    "chamber_of_commerce": (("Q1123526",), 2),
    "property": (("Q191072",), 2),
    "news_media": (("Q1110794",), 6),
}


def import_wikidata(f: Fetcher) -> None:
    countries = country_codes()
    items: list[dict] = []
    for kind, (classes, limit) in WIKIDATA_CLASSES.items():
        rows = []
        for cls in classes:
            # join order fixed by hand: the default optimizer starts from P17 and times out
            rows += f.sparql(f"""
                SELECT ?item ?iso ?web ?sl ?en ?mul ?dissolved WHERE {{
                  hint:Query hint:optimizer "None".
                  ?item wdt:P31 wd:{cls}.
                  ?item wdt:P17 ?country.
                  ?country wdt:P297 ?iso.
                  ?item wdt:P856 ?web.
                  ?item wikibase:sitelinks ?sl.
                  OPTIONAL {{ ?item wdt:P576 ?dissolved }}
                  OPTIONAL {{ ?item rdfs:label ?en FILTER(lang(?en) = "en") }}
                  OPTIONAL {{ ?item rdfs:label ?mul FILTER(lang(?mul) = "mul") }}
                }}""")
        best: dict[str, dict] = {}
        for r in rows:
            if r.get("dissolved") or r["iso"] not in countries:
                continue
            cur = best.setdefault(r["item"], r)
            if r["web"].startswith("https://") and not cur["web"].startswith("https://"):
                best[r["item"]] = r
        per: dict[str, list[dict]] = {}
        for r in best.values():
            per.setdefault(r["iso"], []).append(r)
        for iso, rs in per.items():
            rs.sort(key=lambda r: -int(r.get("sl") or 0))
            for r in rs[:limit]:
                label = r.get("en") or r.get("mul") or ""
                if not label:
                    continue
                items.append({"qid": r["item"].rsplit("/", 1)[-1], "country": iso, "kind": kind, "title": label,
                              "url": norm_url(r["web"]), "sitelinks": int(r.get("sl") or 0)})
        print(f"  wikidata {kind}: {sum(min(len(v), limit) for v in per.values())} items in {len(per)} countries", flush=True)
    # national institutions referenced directly by the country item
    rows = f.sparql("""
        SELECT ?iso ?gov ?leg ?legLabel ?legweb ?court ?courtLabel ?courtweb WHERE {
          ?c wdt:P297 ?iso. MINUS { ?c wdt:P576 [] }
          OPTIONAL { ?c wdt:P856 ?gov }
          OPTIONAL { ?c wdt:P194 ?leg. ?leg wdt:P856 ?legweb. OPTIONAL { ?leg rdfs:label ?legLabel FILTER(lang(?legLabel) = "en") } }
          OPTIONAL { ?c wdt:P209 ?court. ?court wdt:P856 ?courtweb. OPTIONAL { ?court rdfs:label ?courtLabel FILTER(lang(?courtLabel) = "en") } }
        }""")
    seen = {(i["country"], i["url"]) for i in items}
    for r in rows:
        iso = r["iso"]
        if iso not in countries:
            continue
        for kind, url, label, qid in (("government_portal", r.get("gov"), f"{countries[iso]['name']} — official government portal", ""),
                                      ("parliament", r.get("legweb"), r.get("legLabel", ""), r.get("leg", "")),
                                      ("court", r.get("courtweb"), r.get("courtLabel", ""), r.get("court", ""))):
            url = norm_url(url or "")
            if not url or (iso, url) in seen or not label or re.fullmatch(r"Q\d+", label):
                continue
            seen.add((iso, url))
            items.append({"qid": (qid or "").rsplit("/", 1)[-1], "country": iso, "kind": kind, "title": label,
                          "url": url, "sitelinks": 0})
    write(DISCOVERED / "wikidata.json", "wikidata", URLS["wikidata_sparql"], items,
          classes={k: list(v[0]) for k, v in WIKIDATA_CLASSES.items()})


# --------------------------------------------------------------------------- GDELT

def import_gdelt(f: Fetcher) -> None:
    countries = country_codes()
    lookup = {}
    for line in f.get(URLS["gdelt_countries"]).decode("utf-8", "replace").splitlines():
        if "\t" in line:
            code, name = line.split("\t", 1)
            lookup[code.strip()] = name.strip()
    items = {}
    for iso, c in countries.items():
        fips = c.get("fips")
        if fips and fips in lookup:
            items[iso] = {"fips": fips, "gdelt_name": lookup[fips]}
    write(DISCOVERED / "gdelt.json", "gdelt", URLS["gdelt_countries"], items)


# --------------------------------------------------------------------------- platform datasets

def _host(url: str) -> str:
    h = (urlsplit(url.replace("{account}", "x").replace("{username}", "x").replace("{}", "x")).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def import_platforms(f: Fetcher) -> None:
    countries = country_codes()
    wmn = json.loads(f.get(URLS["whatsmyname"]))
    items = []
    for s in wmn.get("sites", []):
        uri = str(s.get("uri_pretty") or s.get("uri_check") or "")
        if "{account}" not in uri:
            continue
        items.append({"id": f"wmn:{_slug(str(s['name']))}", "name": s["name"], "category": s.get("cat") or "misc",
                      "host": _host(uri), "profile_url": uri.replace("{account}", "{username}"),
                      "valid": s.get("valid", True) is not False, "countries": []})
    write(IMPORTED_PLATFORMS / "whatsmyname.json", "whatsmyname", URLS["whatsmyname"], items)

    sh = json.loads(f.get(URLS["sherlock"]))
    items = []
    for name, s in sh.items():
        if name.startswith("$") or not isinstance(s, dict) or "{}" not in str(s.get("url", "")):
            continue
        items.append({"id": f"sherlock:{name.lower()}", "name": name, "category": "", "host": _host(s["url"]),
                      "profile_url": s["url"].replace("{}", "{username}"), "valid": True,
                      "username_pattern": s.get("regexCheck", ""), "countries": []})
    write(IMPORTED_PLATFORMS / "sherlock.json", "sherlock", URLS["sherlock"], items)

    mg = json.loads(f.get(URLS["maigret"]))
    items = []
    engines = mg.get("engines") or {}
    for name, site in (mg.get("sites") or {}).items():
        s = dict(((engines.get(site.get("engine") or "") or {}).get("site")) or {})   # forum-engine templates
        s.update(site)
        url = str(s.get("url") or "")
        main = str(s.get("urlMain") or "")
        if "{username}" not in url:
            continue
        full = url.replace("{urlMain}", main.rstrip("/")).replace("{urlSubpath}", str(s.get("urlSubpath") or ""))
        tags = [t for t in s.get("tags") or [] if isinstance(t, str)]
        ccs = sorted({t.upper() for t in tags if len(t) == 2 and t.upper() in countries} |
                     ({"GB"} if "uk" in tags else set()))
        items.append({"id": f"maigret:{name.lower()}", "name": name, "category": next((t for t in tags if len(t) > 2), ""),
                      "tags": tags, "host": _host(full or main), "profile_url": full, "valid": not s.get("disabled"),
                      "engine": s.get("engine", ""), "countries": ccs, "alexa_rank": s.get("alexaRank"),
                      "mirror_of": s.get("source") or ""})
    write(IMPORTED_PLATFORMS / "maigret.json", "maigret", URLS["maigret"], items)


# --------------------------------------------------------------------------- entry point

STEPS = ("countries", "iana", "rir", "gleif", "gleif_counts", "opensanctions", "wikidata", "gdelt", "platforms")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("steps", nargs="+", choices=(*STEPS, "all"))
    ap.add_argument("--cache-dir", type=Path, help="keep raw downloads here and reuse them")
    ap.add_argument("--no-iana-details", action="store_true", help="skip the per-ccTLD IANA pages (registry URL, WHOIS)")
    args = ap.parse_args(argv)
    steps = STEPS if "all" in args.steps else tuple(s for s in STEPS if s in args.steps)
    f = Fetcher(args.cache_dir)
    for step in steps:
        print(f"[{step}]")
        if step == "countries":
            import_countries(f)
        elif step == "iana":
            import_iana(f, details=not args.no_iana_details)
        elif step == "rir":
            import_rir(f)
        elif step == "gleif":
            import_gleif(f)
        elif step == "gleif_counts":
            import_gleif_counts(f)
        elif step == "opensanctions":
            import_opensanctions(f)
        elif step == "wikidata":
            import_wikidata(f)
        elif step == "gdelt":
            import_gdelt(f)
        elif step == "platforms":
            import_platforms(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
