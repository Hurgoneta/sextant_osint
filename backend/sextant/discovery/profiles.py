"""Profile URLs and result titles → platform, handle, display name. Reusable by every connector and the assistant.

Search results usually show a person's *display name* in the title while the handle only appears in the URL, so the URL
is parsed first and the title second:

* ``parse_profile`` recognises a profile link through the platform registry (curated patterns, ``?user={username}``
  templates, the investigator's own platforms) and, when the URL points *below* a profile (``/<user>/_saved/``,
  ``/@<user>/video/123``, ``/<user>/status/1``), trims the sub-page first. Platforms whose first path segment is always
  the account (``any_subpath`` in ``data/platforms/discovery.toml``: Pinterest boards, GitHub repositories) are trimmed
  to that segment. A trimmed match says so (``how``), because a board or a repository is weaker evidence than a
  profile page.
* ``title_identity`` reads the common result-title shapes (``Name (@handle) • Instagram``, ``Name (handle) - Profile |
  Pinterest``, ``handle (Name) · GitHub``, ``Name | LinkedIn``) and reports whether the handle in the title agrees
  with the URL.

Nothing here makes a network request.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..registry import platforms as P
from ..util.normalize import canonical_url, name_tokens

DATA = Path(__file__).resolve().parents[1] / "data" / "platforms" / "discovery.toml"


@dataclass(frozen=True)
class Target:
    platform: str
    priority: float
    site: str
    countries: tuple[str, ...] = ()
    any_subpath: bool = False


@dataclass(frozen=True)
class ProfileHit:
    platform: P.Platform
    username: str
    profile_url: str          # canonical profile URL (the profile page, not the sub-page that was found)
    found_url: str            # the URL as found
    how: str                  # "profile" | "subpage" | "owner"

    @property
    def evidence(self) -> str:
        return {"profile": "The result is the profile page itself",
                "subpage": "The result is a page of this profile (followers, posts, a video…)",
                "owner": "The result is content owned by this account (a board, a repository…)"}[self.how]


@lru_cache(maxsize=1)
def _config() -> tuple[tuple[Target, ...], frozenset[str], frozenset[str]]:
    with open(DATA, "rb") as fh:
        doc = tomllib.load(fh)
    targets = []
    for raw in doc.get("target", []):
        targets.append(Target(platform=str(raw["platform"]), priority=float(raw.get("priority", 0.3)),
                              site=str(raw.get("site") or ""), countries=tuple(raw.get("countries") or ()),
                              any_subpath=bool(raw.get("any_subpath"))))
    return (tuple(targets), frozenset(str(s).lower() for s in doc.get("subpages", [])),
            frozenset(str(s).lower() for s in doc.get("not_handles", [])))


def targets() -> tuple[Target, ...]:
    """Built-in discovery targets whose platform exists in this installation."""
    return tuple(t for t in _config()[0] if P.get(t.platform) is not None)


def subpages() -> frozenset[str]:
    return _config()[1]


def _owner_platforms() -> frozenset[str]:
    return frozenset(t.platform for t in _config()[0] if t.any_subpath)


def parse_profile(url: str) -> ProfileHit | None:
    """The account a URL belongs to, or None. Built-in platforms first, then the investigator's own (in scope)."""
    if not url or not isinstance(url, str):
        return None
    hit = P.parse_profile_url(url)
    if hit:
        return ProfileHit(hit[0], hit[1], _profile_url(hit[0], hit[1], url), url, "profile")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    segs = [s for s in (parts.path or "").split("/") if s]
    if len(segs) < 2:
        return None
    sub = subpages()
    for cut in range(len(segs) - 1, 0, -1):
        trimmed = urlunsplit((parts.scheme or "https", parts.netloc, "/" + "/".join(segs[:cut]) + "/", "", ""))
        hit = P.parse_profile_url(trimmed)
        if not hit:
            continue
        if hit[1].lower() in _config()[2]:
            return None                              # /pin/123, /p/abc, /watch/…: content, not an account
        rest = segs[cut]
        if rest.lower() in sub:
            return ProfileHit(hit[0], hit[1], _profile_url(hit[0], hit[1], trimmed), url, "subpage")
        if cut == 1 and hit[0].id in _owner_platforms():
            return ProfileHit(hit[0], hit[1], _profile_url(hit[0], hit[1], trimmed), url, "owner")
        return None
    return None


def _profile_url(p: P.Platform, username: str, fallback: str) -> str:
    built = p.profile(username)
    return canonical_url(built) or built if built else (canonical_url(fallback) or fallback)


# --------------------------------------------------------------------------- result titles

_SUFFIX = re.compile(r"\s*(?:[-|•·–—/:]\s*)?(?:on\s+)?(?:profile\s*[|-]\s*)?"
                     r"(instagram|pinterest|tiktok|twitter|x|github|gitlab|youtube|facebook|linkedin|reddit|twitch|"
                     r"spotify|soundcloud|threads|medium|behance|vk|vkontakte|bluesky|tumblr|flickr|snapchat|linktree|"
                     r"telegram|quora|patreon|vimeo|letterboxd|goodreads|deviantart)\b.*$", re.I)
_NAME_HANDLE = re.compile(r"^(?P<name>[^()@|•·]{2,80}?)\s*\(\s*@?(?P<handle>[A-Za-z0-9_.\-]{2,60})\s*\)")
_HANDLE_NAME = re.compile(r"^@?(?P<handle>[A-Za-z0-9_.\-]{2,60})\s*\((?P<name>[^()@]{2,80})\)")
_AT_HANDLE = re.compile(r"(?<![\w@])@(?P<handle>[A-Za-z0-9_.]{2,30})\b")


@dataclass(frozen=True)
class TitleIdentity:
    display_name: str | None
    handle_in_title: str | None
    handle_agrees: bool | None       # None when the title shows no handle


def title_identity(title: str, username: str | None = None) -> TitleIdentity:
    """Display name and handle shown in a result title, and whether that handle is the one in the URL."""
    t = " ".join(str(title or "").split())[:300]
    if not t:
        return TitleIdentity(None, None, None)
    name = handle = None
    m = _NAME_HANDLE.match(t)
    if m and _looks_like_name(m.group("name")):
        name, handle = m.group("name").strip(" -|"), m.group("handle")
    else:
        m = _HANDLE_NAME.match(t)
        if m and _looks_like_name(m.group("name")):
            name, handle = m.group("name").strip(), m.group("handle")
    if name is None:
        head = _SUFFIX.sub("", t).strip(" -|•·")
        for sep in (" (", " - ", " | ", " – ", " • ", " · ", " on "):
            if sep in head:
                head = head.split(sep, 1)[0].strip()
        if _looks_like_name(head):
            name = head
    if handle is None:
        m = _AT_HANDLE.search(t)
        if m:
            handle = m.group("handle")
    agrees = None if handle is None or not username else handle.strip(".").casefold() == username.casefold()
    return TitleIdentity(name, handle, agrees)


def _looks_like_name(text: str) -> bool:
    text = (text or "").strip()
    toks = name_tokens(text)
    return 2 <= len(text) <= 80 and 2 <= len(toks) <= 6 and not any(c.isdigit() for c in text) and "http" not in text


def handle_name_overlap(handle: str, names: list[str]) -> list[str]:
    """Name tokens (≥ 3 letters) that appear inside a handle: 'mirafictuse' ⊃ 'mira', 'fictus'. A cue for ranking
    and for deciding which discovered handles to pivot on, never identity evidence."""
    h = re.sub(r"[^a-z0-9]", "", (handle or "").casefold())
    out = []
    for n in names:
        for tok in name_tokens(n):
            if len(tok) >= 3 and tok in h and tok not in out:
                out.append(tok)
    return out


__all__ = ["Target", "ProfileHit", "TitleIdentity", "targets", "subpages", "parse_profile", "title_identity",
           "handle_name_overlap"]
