"""Shared helpers for company-register connectors."""

from __future__ import annotations

from ...util.normalize import name_tokens, normalize_name, org_name_key
from .._common import person_names


def add_org(out, src, *, scheme: str, number: str, name: str, jurisdiction: str, props: dict | None = None,
            link_from: str | None = None, query_name: str | None = None) -> str | None:
    p = {"name": name, "registration_number": number, "jurisdiction": jurisdiction, "registry": scheme}
    p.update({k: v for k, v in (props or {}).items() if v not in (None, "", [], {})})
    key = out.entity("organization", f"{scheme}:{number}", label=name, source=src, props=p,
                     evidence=f"Entry in {scheme} register")
    if key and link_from and link_from != key:
        exact = query_name and org_name_key(query_name) == org_name_key(name)
        out.link(link_from, key, "ASSOCIATED_WITH", source=src, confidence=0.9 if exact else 0.5,
                 evidence=f"Register search for “{query_name}” returned “{name}”" + (" (same name)" if exact else ""))
    return key


def add_officer(out, src, org_key: str | None, *, registry: str, record_id: str, name: str, role: str = "",
                birth_date: str | None = None, birth_year: int | None = None, nationality: str | None = None,
                address: str | None = None, start: str | None = None, end: str | None = None,
                occupation: str | None = None, query_name: str | None = None) -> str | None:
    n = normalize_name(name.title() if name.isupper() else name)
    if not n or len(name_tokens(n)) < 2:
        return None
    by = birth_year or (int(str(birth_date)[:4]) if birth_date and str(birth_date)[:4].isdigit() else None)
    pk = out.entity("person", f"{registry}:{record_id}", label=n, source=src,
                    props={"full_name": n, "birth_date": birth_date, "nationality": nationality,
                           "location": address, "role": role, "occupation": occupation,
                           "registry_birth_year": by},
                    evidence=f"Officer/party listed by {registry}")
    if by:
        out.prop(pk, "birth_date", birth_date or str(by), source=src, evidence="Date of birth as published by the register")
    ne = out.entity("name", n, source=src)
    out.link(pk, ne, "HAS_NAME", source=src, evidence="Name as written in the register")
    if query_name:
        qn = out.entity("name", query_name, source=src)
        if qn and ne and qn != ne:
            out.link(qn, ne, "ASSOCIATED_WITH", source=src, confidence=0.5,
                     evidence=f"Register search for “{query_name}” returned “{n}”")
    if org_key:
        out.link(pk, org_key, "MEMBER_OF", source=src, props={"role": role, "from": start, "to": end},
                 evidence=f"{role or 'Officer'}" + (f" since {start}" if start else "") + (f" until {end}" if end else ""))
    return pk


def org_accepts(country: str | None):
    """National registers only run on organizations of their country (or the investigation's input when the
    country is unknown); global ones run on any named organization."""
    def accepts(_self, e, info) -> bool:
        name = e.get("name") or (e.label if not e.label.startswith("Unknown") else "")
        if not name or len(str(name)) < 2:
            return False
        if country is None:
            return True
        j = str(e.get("jurisdiction") or "").upper()[:2]
        if j:
            return j == country
        if info.countries:                    # the investigation points to countries: only their registers
            return country in info.countries
        return e.is_seed                      # country unknown: the input is tried in every register
    return accepts


def seed_person(country: str | None = None):
    """Person-name searches in registers run for the investigation's target (and its aliases) only."""
    def accepts(_self, e, info) -> bool:
        if not (e.is_seed and person_names(e, info)):
            return False
        return country is None or not info.countries or country in info.countries
    return accepts


def org_name(e) -> str:
    return str(e.get("name") or e.label)
