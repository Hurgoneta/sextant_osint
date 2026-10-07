"""Which countries an investigation points to — inferred locally from the input, never sent anywhere.

Signals: the region of a phone number, the country-code TLD of a domain or website, country names and aliases
in a location (any of the 195 states and the territories in the country registry), and countries the
investigator selects explicitly. The result decides which country-specific sources are planned.
"""

from __future__ import annotations

from ..registry.countries import get as country_get
from ..registry.jurisdictions import country_names
from ..util.normalize import fold, location_tokens, parse_phone, registered_domain
from .input import ParsedInput

# ccTLDs used as generic TLDs: a domain under them says nothing about a country
CCTLD_GENERIC = {"io", "co", "me", "tv", "ai", "app", "dev", "cc", "ws", "fm", "am", "to", "ly", "gg", "xyz", "eu", "nu",
                 "tk", "ml", "ga", "cf", "gq", "la", "vc", "sh", "ac", "im", "so", "st", "pw"}


def infer_countries(parsed: ParsedInput, explicit: list[str] | None = None) -> list[dict]:
    """[{code, name, why}] in order of discovery (explicit choices first)."""
    out: dict[str, dict] = {}

    def add(code: str, why: str) -> None:
        code = (code or "").upper()
        c = country_get(code)
        if c is None:
            return
        if code not in out:
            out[code] = {"code": code, "name": c.name, "why": []}
        if why not in out[code]["why"]:
            out[code]["why"].append(why)

    for code in explicit or []:
        add(code, "selected by you")
    for s in parsed.seeds:
        if s.type == "phone":
            info = parse_phone(s.value)
            if info and info.region:
                add(info.region, f"phone {s.label}")
        if s.type in ("domain", "website"):
            host = s.value.split("/")[2] if "://" in s.value else s.value
            tld = (registered_domain(host) or "").rsplit(".", 1)[-1]
            if len(tld) == 2 and tld not in CCTLD_GENERIC:
                add("GB" if tld == "uk" else tld.upper(), f"domain .{tld}")
    names = _alias_tokens()
    for s in parsed.seeds:
        if s.type != "location":
            continue
        toks = f" {' '.join(location_tokens(fold(s.label)))} "
        for code, aliases in names.items():
            if any(a and f" {a} " in toks for a in aliases):
                add(code, f"location “{s.label}”")
    return list(out.values())


_ALIAS_CACHE: dict[str, tuple[str, ...]] = {}


def _alias_tokens() -> dict[str, tuple[str, ...]]:
    """Country names and aliases as normalised token strings ("côte d'ivoire" -> "cote d ivoire")."""
    if not _ALIAS_CACHE:
        for code, aliases in country_names().items():
            _ALIAS_CACHE[code] = tuple(dict.fromkeys(" ".join(location_tokens(fold(a))) for a in aliases))
    return _ALIAS_CACHE
