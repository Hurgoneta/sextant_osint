"""Sources discovered from maintained public datasets — per country, with provenance.

The curated registry (``registry/jurisdictions.py``) holds sources a person reviewed. This module adds what
maintained datasets know about each country, so no country depends on someone remembering to type it in:

    dataset                         gives, per country
    ─────────────────────────────── ───────────────────────────────────────────────────────────────
    GLEIF Registration Authorities  business / company registers (the authorities LEIs are validated against)
    OpenSanctions dataset index     official sanctions, PEP, debarment, wanted and regulatory lists
    Wikidata (CC0)                  gazettes, statistics offices, courts, parliaments, archives, central banks,
                                    data-protection and election authorities, news agencies, open-data portals…
    IANA root zone + RDAP bootstrap the ccTLD registry and whether it answers RDAP
    RIR delegated statistics        the internet number registry and allocated resources
    GDELT lookup                    whether GDELT can filter news by outlets in that country
    GLEIF API                       how many legal entities (LEIs) are registered there

Every entry says which dataset it came from and when the dataset was retrieved. A discovered entry is a
*potential* source: nobody has checked what it publishes or whether it can be searched by name, and the
UI labels it "discovered (dataset)". When its host matches a curated source of the same country, it is
marked as corroborating that source instead of being listed twice.

Refresh with ``python -m sextant.tools.import_datasets all``; URLs are checked by ``verify_catalog``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from . import countries as C
from .jurisdictions import KIND_GROUP, SOURCE_GROUPS, SOURCE_KINDS, jurisdictions

DATA = Path(__file__).resolve().parents[1] / "data" / "discovered"

DATASETS = {
    "gleif_ra": "GLEIF Registration Authorities list",
    "opensanctions": "OpenSanctions dataset index",
    "wikidata": "Wikidata",
    "iana": "IANA root zone database",
    "rir": "RIR delegated statistics",
    "gdelt": "GDELT source-country lookup",
}


@dataclass
class DiscoveredSource:
    id: str
    country: str
    kind: str
    title: str
    url: str
    dataset: str                       # key of DATASETS
    dataset_ref: str = ""              # RA code, OpenSanctions dataset name, Wikidata QID, ccTLD…
    connector: str = ""                # connector that can query it (if any)
    connector_note: str = ""
    notes: str = ""
    metrics: dict = field(default_factory=dict)
    corroborates: str = ""             # id of the curated source with the same host

    @property
    def group(self) -> str:
        return KIND_GROUP.get(self.kind, "government")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_title"] = SOURCE_KINDS.get(self.kind, self.kind)
        d["group"] = self.group
        d["group_title"] = SOURCE_GROUPS[self.group]
        d["dataset_title"] = DATASETS.get(self.dataset, self.dataset)
        d["provenance"] = "discovered"
        return d


def _read(name: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def dataset_info() -> dict[str, dict]:
    """Provenance of each dataset snapshot (URL, licence, retrieval time, size)."""
    out = {}
    for key, file in (("gleif_ra", "gleif_ra.json"), ("opensanctions", "opensanctions.json"), ("wikidata", "wikidata.json"),
                      ("iana", "iana_cctld.json"), ("rir", "rir.json"), ("gdelt", "gdelt.json"),
                      ("gleif_counts", "gleif_counts.json")):
        d = _read(file)
        if d:
            items = d.get("items")
            out[key] = {"title": DATASETS.get(key, "GLEIF LEI counts per country"), "source_url": d.get("source_url"),
                        "license": d.get("license"), "retrieved_at": d.get("retrieved_at"),
                        "items": len(items) if hasattr(items, "__len__") else 0}
    return out


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _ra_kind(r: dict) -> str:
    text = " ".join(str(r.get(k) or "") for k in ("register", "organization", "register_local", "organization_local")).lower()
    if "charit" in text or "non-profit" in text or "nonprofit" in text or "association" in text and "register of associations" in text:
        return "charity_register"
    if "statistic" in text:
        return "statistics"
    if any(w in text for w in ("tax", "revenue", "fiscal", "impuest", "contribuyente", "impôt", "impot", "steuer", "tribut")):
        return "tax_authority"
    if "securities" in text or "financial" in text or "central bank" in text or "monetary" in text:
        return "financial_regulator"
    return "company_registry"


OS_COLLECTION_CONNECTORS = (("sanctions", "opensanctions_bulk"), ("default", "opensanctions"))


@lru_cache(maxsize=1)
def by_country() -> dict[str, list[DiscoveredSource]]:
    reg = C.countries()
    out: dict[str, list[DiscoveredSource]] = {code: [] for code in reg}

    def add(src: DiscoveredSource) -> None:
        if src.country in out and src.url.startswith(("https://", "http://")):
            out[src.country].append(src)

    for r in _read("gleif_ra.json").get("items", []):
        if not r.get("website"):
            continue
        register_ = r.get("register") or r.get("register_local") or ""
        org = r.get("organization") or r.get("organization_local") or ""
        title = register_ or org or f"Registration authority {r['ra_code']}"
        if org and register_ and org not in title:
            title = f"{register_} — {org}"
        local = r.get("register_local") or r.get("organization_local")
        local = local if local and local not in title else ""
        juris = r.get("jurisdiction") or ""
        sub = f" ({juris})" if juris and r.get("jurisdiction_code", "")[:2] == r["country"] and "-" in r.get("jurisdiction_code", "") else ""
        add(DiscoveredSource(id=f"gleif:{r['ra_code']}", country=r["country"], kind=_ra_kind(r), title=title + sub,
                             url=r["website"], dataset="gleif_ra", dataset_ref=r["ra_code"],
                             notes=" · ".join(x for x in (local, r.get("comments")) if x)[:300]))

    for d in _read("opensanctions.json").get("items", []):
        if not d.get("country") or not d.get("official"):
            continue
        conn, note = "", ""
        for coll, name in OS_COLLECTION_CONNECTORS:
            if coll in d.get("collections", []):
                conn = name
                note = ("Matched locally against the OpenSanctions sanctions collection (opt-in, non-commercial licence)"
                        if name == "opensanctions_bulk" else "Searchable through the OpenSanctions API (key required)")
                break
        add(DiscoveredSource(id=f"os:{d['name']}", country=d["country"], kind=d["kind"] if d["kind"] in SOURCE_KINDS else "regulatory",
                             title=d.get("title") or d["name"], url=d.get("url") or d.get("dataset_url"),
                             dataset="opensanctions", dataset_ref=d["name"], connector=conn, connector_note=note,
                             notes=f"Publisher: {d.get('publisher', '')}. {d.get('summary', '')}"[:300],
                             metrics={"entities": d.get("target_count") or d.get("entity_count") or 0,
                                      "updated_at": d.get("updated_at", "")}))

    for w in _read("wikidata.json").get("items", []):
        if w.get("kind") not in SOURCE_KINDS:
            continue
        add(DiscoveredSource(id=f"wd:{w['country']}:{w.get('qid') or w['kind']}:{_host(w['url'])}", country=w["country"],
                             kind=w["kind"], title=w["title"], url=w["url"], dataset="wikidata",
                             dataset_ref=w.get("qid", "")))

    for cc, t in (_read("iana_cctld.json").get("items") or {}).items():
        rdap = t.get("rdap") or []
        add(DiscoveredSource(id=f"iana:{t['tld'].lstrip('.')}", country=cc, kind="domain_registry",
                             title=f"{t['tld']} registry — {t.get('manager', '')}".strip(" —"),
                             url=t.get("registry_url") or t["iana_url"], dataset="iana", dataset_ref=t["tld"],
                             connector="rdap" if rdap else "",
                             connector_note=("Registration data via RDAP: " + ", ".join(rdap)) if rdap else
                             "No RDAP service in the IANA bootstrap — registration data only via the registry's own WHOIS/website",
                             metrics={"rdap": bool(rdap), "whois": t.get("whois", "")}))

    for cc, r in (_read("rir.json").get("items") or {}).items():
        add(DiscoveredSource(id=f"rir:{cc.lower()}", country=cc, kind="internet_registry",
                             title=f"{r['primary_rir']} — IP addresses and ASNs allocated in this country",
                             url=r["rir_url"], dataset="rir", dataset_ref=r["primary_rir"], connector="ripestat",
                             connector_note="RIPEstat answers for resources of all five RIRs",
                             metrics={"asn": r.get("asn", 0), "ipv4_addresses": r.get("ipv4_addresses", 0),
                                      "ipv6_blocks": r.get("ipv6_blocks", 0)}))

    for cc, g in (_read("gdelt.json").get("items") or {}).items():
        add(DiscoveredSource(id=f"gdelt:{cc.lower()}", country=cc, kind="news_media",
                             title=f"GDELT — worldwide news coverage from media outlets in {reg[cc].name if cc in reg else cc}",
                             url="https://www.gdeltproject.org/", dataset="gdelt", dataset_ref=g["fips"], connector="gdelt",
                             connector_note=f"News search filtered with sourcecountry:{g['fips']} (articles in 65 languages)"))

    # corroboration: a discovered URL on the same host as a curated source of the same country
    js = jurisdictions()
    for cc, items in out.items():
        curated = {_host(s.url): s.id for s in (js[cc].sources if cc in js else []) if s.provenance == "curated"}
        seen: set[tuple[str, str]] = set()
        kept = []
        for s in items:
            s.corroborates = curated.get(_host(s.url), "")
            key = (s.kind, _host(s.url) + urlsplit(s.url).path.rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            kept.append(s)
        out[cc] = kept
    return out


@lru_cache(maxsize=1)
def lei_counts() -> dict[str, int]:
    return _read("gleif_counts.json").get("items") or {}


def all_discovered() -> list[DiscoveredSource]:
    return [s for items in by_country().values() for s in items]


def for_country(code: str) -> list[DiscoveredSource]:
    return by_country().get(code.upper(), [])


_SAFE_ID = re.compile(r"[^A-Za-z0-9:_.-]+")


def verification_key(s: DiscoveredSource) -> str:
    return _SAFE_ID.sub("_", s.id)
