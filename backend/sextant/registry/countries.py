"""Country registry: every sovereign state as its own entry, never a continent-wide placeholder.

The canonical list (``data/countries/countries.json``) is built by
``python -m sextant.tools.import_datasets countries`` from the UN M49 table and Wikidata, plus curated
overrides (``data/countries/overrides.toml``):

* 193 UN member states + the Holy See + the State of Palestine (UN observer states) = **195 sovereign states**;
* territories and partially recognised states that run their own registers (Taiwan, Kosovo, Hong Kong,
  Jersey, Cayman Islands…), listed separately and never counted among the 195.

Everything country-aware (jurisdiction sources, discovered sources, connectors' ``supported_countries``,
regional platforms, location parsing) is keyed by these ISO 3166-1 alpha-2 codes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "countries"

SOVEREIGN_STATUSES = ("un_member", "un_observer_state")
STATUS_TITLES = {
    "un_member": "UN member state",
    "un_observer_state": "UN observer state",
    "partially_recognized": "Partially recognised state (not counted among the 195)",
    "special_administrative_region": "Special administrative region (not counted among the 195)",
    "crown_dependency": "Crown dependency (not counted among the 195)",
    "territory": "Territory (not counted among the 195)",
}
REGIONS = ("Europe", "Africa", "Asia", "Middle East", "North America", "Central America & Caribbean", "South America",
           "Oceania")


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    region: str
    status: str
    subregion: str = ""
    iso3: str = ""
    m49: str = ""
    aliases: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    tld: str = ""
    fips: str = ""
    wikidata: str = ""
    ldc: bool = False
    lldc: bool = False
    sids: bool = False
    sovereign_of: str = ""

    @property
    def sovereign(self) -> bool:
        return self.status in SOVEREIGN_STATUSES

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sovereign"] = self.sovereign
        d["status_title"] = STATUS_TITLES.get(self.status, self.status)
        return d


@lru_cache(maxsize=1)
def _doc() -> dict:
    return json.loads((DATA / "countries.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def countries() -> dict[str, Country]:
    out = {}
    for c in _doc()["items"]:
        kw = {k: v for k, v in c.items() if k in Country.__dataclass_fields__}
        kw["aliases"] = tuple(kw.get("aliases") or ())
        kw["languages"] = tuple(kw.get("languages") or ())
        out[c["code"]] = Country(**kw)
    return out


def sovereign() -> list[Country]:
    return [c for c in countries().values() if c.sovereign]


def get(code: str | None) -> Country | None:
    return countries().get((code or "").upper())


def name(code: str) -> str:
    c = get(code)
    return c.name if c else code


def provenance() -> dict:
    d = _doc()
    return {"dataset": d.get("dataset"), "source_url": d.get("source_url"), "license": d.get("license"),
            "retrieved_at": d.get("retrieved_at"), "note": d.get("note")}


def by_tld(tld: str) -> Country | None:
    tld = "." + tld.lower().lstrip(".")
    for c in countries().values():
        if c.tld == tld:
            return c
    return None


__all__ = ["Country", "REGIONS", "STATUS_TITLES", "countries", "sovereign", "get", "name", "provenance", "by_tld"]
