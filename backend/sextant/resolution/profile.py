"""What is known for certain about the target: seeds + everything the investigator confirmed.

The profile is rebuilt on every resolution pass, so a single confirmation can raise or lower the
scores of everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import Entity, Graph
from ..util.extract import SeedMatcher
from ..util.normalize import (
    canonical_url, is_free_email_provider, location_tokens, name_tokens, org_name_key, parse_phone, registered_domain,
    url_host,
)

ANCHOR_CONFIDENCE = 0.85


def trusted(e: Entity) -> bool:
    return e.status in ("seed", "confirmed")


@dataclass
class Anchor:
    key: str
    title: str
    urls: set[str]
    avatar_fp: dict | None
    display_name: str | None


@dataclass
class TargetProfile:
    target_type: str = "person"
    names: list[str] = field(default_factory=list)
    name_tokens: list[str] = field(default_factory=list)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    email_domains: set[str] = field(default_factory=set)
    city_tokens: list[str] = field(default_factory=list)
    country_tokens: list[str] = field(default_factory=list)
    employers: set[str] = field(default_factory=set)          # org name keys
    occupations: set[str] = field(default_factory=set)
    org_ids: set[str] = field(default_factory=set)            # registration numbers, LEIs of trusted organizations
    birth_year: int | None = None
    nationality: set[str] = field(default_factory=set)
    photos: list[tuple[str, dict]] = field(default_factory=list)
    anchors: dict[str, Anchor] = field(default_factory=dict)
    matcher: SeedMatcher = field(default_factory=SeedMatcher)

    @property
    def has_location(self) -> bool:
        return bool(self.city_tokens or self.country_tokens)


def _int(v) -> int | None:
    try:
        return int(str(v)[:4])
    except (TypeError, ValueError):
        return None


def build_profile(g: Graph, anchors: set[str]) -> TargetProfile:
    p = TargetProfile()
    target = g.target
    if target is not None:
        p.target_type = target.type
        if target.type == "person":
            p.names = [str(n) for n in [*target.values("full_name"), *target.all_list("aliases")] if n]
            by = _int(target.get("birth_year"))
            p.birth_year = by
            p.nationality = {str(n).casefold() for n in target.values("nationality")}
            p.occupations = {str(o).casefold() for o in target.values("occupation")}
            p.employers = {k for k in (org_name_key(str(o)) for o in target.values("employer")) if k}
        elif target.type == "organization":
            p.names = [str(n) for n in target.values("name") if n] or [target.label]
            p.org_ids = {str(x).casefold() for x in [*target.values("registration_number"), *target.values("lei")] if x}
    if p.names:
        p.name_tokens = name_tokens(p.names[0])
    for e in g.entities.values():
        if not trusted(e) or e.key == g.target_key:
            continue
        t = e.type
        if t == "email":
            p.emails.add(e.value)
            dom = e.value.rsplit("@", 1)[1]
            if not is_free_email_provider(dom):
                p.email_domains.add(registered_domain(dom) or dom)
        elif t == "phone":
            p.phones.add(e.value)
        elif t == "username":
            p.usernames.add(e.value)
        elif t == "domain":
            p.domains.add(registered_domain(e.value) or e.value)
        elif t == "website":
            rd = registered_domain(url_host(e.value))
            if rd and e.status == "seed":
                p.domains.add(rd)
        elif t == "location":
            text = str(e.get("text") or e.label)
            parts = [x for x in text.split(",") if x.strip()]
            if parts and not p.city_tokens:
                p.city_tokens = location_tokens(parts[0])
                p.country_tokens = location_tokens(parts[-1]) if len(parts) > 1 else []
        elif t == "organization":
            k = org_name_key(str(e.get("name") or e.label))
            if k:
                p.employers.add(k)
            p.org_ids |= {str(x).casefold() for x in [*e.values("registration_number"), *e.values("lei")] if x}
        elif t == "account" and e.status == "confirmed":
            u = e.get("username")
            if u:
                p.usernames.add(str(u).casefold())
        elif t == "image" and (e.get("user_photo") or e.status in ("seed", "confirmed")):
            if not e.get("flat"):
                p.photos.append((e.label, {k: e.get(k) for k in ("dhash", "phash", "dhash_flip", "phash_flip", "flat")}))
    for key in anchors:
        acc = g.get(key)
        if acc is None:
            continue
        urls = {u for u in (canonical_url(str(acc.get("profile_url") or "")),) if u}
        p.anchors[acc.key] = Anchor(acc.key, str(acc.get("platform_title") or acc.get("platform") or "account"), urls,
                                    acc.get("avatar_fp"), acc.get("display_name"))
    phones = {}
    for ph in p.phones:
        info = parse_phone(ph)
        phones[ph] = info.search_variants if info else [ph]
    p.matcher = SeedMatcher(names=list(p.names[:3]), emails=sorted(p.emails), phones=phones,
                            usernames=sorted(p.usernames), domains=sorted(p.domains))
    return p
