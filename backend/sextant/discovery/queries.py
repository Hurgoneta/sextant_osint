"""Search-query planning for identity discovery: which queries, in which order, within which budget.

A handle that nobody could have guessed from the name (``tavo_in42``, ``mirafictuse``) is still findable when a
search engine has indexed the profile: the result title carries the display name, the URL carries the handle. So the
pipeline asks search engines for the *name on each platform*, not for guessed handles:

    "Mira Fictus Exempla" site:pinterest.com
    "Mira Fictus" site:pinterest.com          (a shorter form people use as a display name)

and, once a handle is discovered, for that *handle on the platforms that cannot be checked automatically*:

    "mirafictuse" site:instagram.com

Queries are ranked, never fired blindly: platform priority (``data/platforms/discovery.toml``) × name-variant prior ×
a boost for platforms common in the investigation's countries × a boost for platforms Sextant cannot check directly
(login walls, manual-only platforms: a search engine is the only automated way to see them), minus platforms where
the identifier was already found. The list is cut at the query budget of the investigation mode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..registry import platforms as P
from ..util.normalize import fold, name_tokens
from .profiles import Target, targets

# queries per run of the person stage, by depth preset (1 quick · 2 balanced · 3/4 deep); per discovered handle
PERSON_CAP = {"1": 6, "2": 12, "3": 20, "4": 24, "custom": 12}
HANDLE_CAP = {"1": 2, "2": 4, "3": 6, "4": 8, "custom": 4}


@dataclass(frozen=True)
class PlannedQuery:
    text: str
    purpose: str
    kind: str                 # name | name_platform | handle_platform
    platform: str | None
    priority: float
    variant: str              # the name or handle form used

    def to_dict(self) -> dict:
        return asdict(self)


def q(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def name_variants(name: str) -> list[tuple[str, str, float]]:
    """(form, why, prior) — the full name first, then the forms people commonly display."""
    name = " ".join((name or "").split())
    toks = name.split(" ")
    out: list[tuple[str, str, float]] = [(name, "the full name as given", 1.0)]
    folded = " ".join(t for t in fold(name).split(" "))
    if folded.casefold() != name.casefold() and all(c.isascii() for c in folded):
        out.append((folded.title(), "the name without diacritics", 0.85))
    if len(toks) >= 3:
        out.append((f"{toks[0]} {toks[1]}", "first two names (a common display name)", 0.7))
        out.append((f"{toks[0]} {toks[-1]}", "first and last name, middle name left out", 0.6))
    elif len(toks) == 2:
        out.append((f"{toks[1]} {toks[0]}", "family name first", 0.45))
    seen: set[str] = set()
    uniq = []
    for form, why, prior in out:
        k = form.casefold()
        if k not in seen and len(name_tokens(form)) >= 2:
            seen.add(k)
            uniq.append((form, why, prior))
    return uniq


def ranked_targets(countries: tuple[str, ...] = (), extra: tuple = (), for_handle: bool = False,
                   only: tuple[str, ...] = ()) -> list[tuple[Target, float]]:
    """Discovery targets with their score for this investigation. ``extra``: the investigator's own platforms (those
    with a profile-URL template become targets too). ``only``: restrict to these platform ids (a focused request such
    as "check Pinterest and TikTok"); a platform that is not a built-in target is searched by its host."""
    out = []
    base = list(targets())
    for pid in only:
        if not any(t.platform == pid for t in base):
            p = P.get(pid) or next((u for u in extra if u.id == pid), None)
            if p is not None and p.hosts:
                base.append(Target(pid, 0.5, p.hosts[0]))
    for t in base:
        if only and t.platform not in only:
            continue
        p = P.get(t.platform)
        score = t.priority
        if countries and set(countries) & set(t.countries):
            score *= 1.6
        if for_handle and p is not None and (not p.automated or p.public_profile == "login_wall"):
            score *= 1.5      # a known handle is checked directly where possible; the web is the only way to see these
        out.append((t, score))
    for up in extra:
        if only and up.id not in only:
            continue
        if any(t.platform == up.id for t, _ in out):
            continue
        if getattr(up, "profile_url", "") and up.hosts:
            regional = bool(countries) and bool(set(countries) & set(up.countries))
            out.append((Target(up.id, 0.5, up.hosts[0], tuple(c for c in up.countries if c != "*")),
                        0.5 * (1.6 if regional else 1.0)))
    out.sort(key=lambda x: -x[1])
    return out


def plan_person(names: list[str], *, countries: tuple[str, ...] = (), found_platforms: frozenset[str] = frozenset(),
                cap: int = 12, extra: tuple = (), only: tuple[str, ...] = ()) -> list[PlannedQuery]:
    """Platform-targeted name queries for a person, best first, at most ``cap``."""
    if not names:
        return []
    variants = name_variants(names[0])
    for alias in names[1:2]:
        variants.append((alias, "an alias you gave", 0.65))
    plan: list[PlannedQuery] = []
    full = variants[0][0]
    if not only:
        plan.append(PlannedQuery(f"{q(full)} social media profile", "Profiles that mention the full name on any platform",
                                 "name", None, 0.97, full))
    for t, score in ranked_targets(countries, extra, only=only):
        title = P.title(t.platform)
        for form, why, prior in variants:
            pr = score * prior * (0.25 if t.platform in found_platforms and not only else 1.0)
            plan.append(PlannedQuery(f"{q(form)} site:{t.site}", f"{title} profiles with {why}", "name_platform",
                                     t.platform, round(pr, 4), form))
    return _cut(plan, cap)


def plan_handle(handle: str, *, countries: tuple[str, ...] = (), found_platforms: frozenset[str] = frozenset(),
                cap: int = 4, extra: tuple = (), only: tuple[str, ...] = ()) -> list[PlannedQuery]:
    """Platform-targeted queries for a discovered handle, favouring platforms that cannot be checked directly."""
    plan = []
    for t, score in ranked_targets(countries, extra, for_handle=True, only=only):
        if t.platform in found_platforms and not only:
            continue
        p = P.get(t.platform)
        if p is not None and not p.valid_username(handle):
            continue
        plan.append(PlannedQuery(f"{q(handle)} site:{t.site}", f"The handle on {P.title(t.platform)}", "handle_platform",
                                 t.platform, round(score, 4), handle))
    return _cut(plan, cap)


def _cut(plan: list[PlannedQuery], cap: int) -> list[PlannedQuery]:
    seen: set[str] = set()
    out = []
    for item in sorted(plan, key=lambda x: -x.priority):
        if item.text in seen:
            continue
        seen.add(item.text)
        out.append(item)
        if len(out) >= cap:
            break
    return out


__all__ = ["PlannedQuery", "PERSON_CAP", "HANDLE_CAP", "name_variants", "ranked_targets", "plan_person", "plan_handle"]
