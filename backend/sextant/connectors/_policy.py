"""Site policy shared by every username checker (WhatsMyName engine, Maigret and Sherlock adapters).

Platforms whose terms forbid automated access are never contacted automatically — neither directly nor through
third-party mirrors or viewers of them (ImgInn, Picuki, Nitter…), which would sidestep the same restriction.
The investigator gets manual links for them instead. Adult and dating sites are checked only when the
"Include adult & dating sites" setting is on.
"""

from __future__ import annotations

from urllib.parse import urlsplit

FORBIDDEN = {
    "facebook": ("facebook.com", "fb.com", "messenger.com"),
    "instagram": ("instagram.com",),
    "x": ("x.com", "twitter.com"),
    "twitter": ("x.com", "twitter.com"),
    "tiktok": ("tiktok.com",),
    "threads": ("threads.net", "threads.com"),
    "snapchat": ("snapchat.com",),
    "linkedin": ("linkedin.com",),
}
FORBIDDEN_HOSTS = tuple(sorted({h for hosts in FORBIDDEN.values() for h in hosts}))
ADULT_TAGS = frozenset({"porn", "erotic", "webcam", "dating", "nsfw", "xx nsfw xx"})


def _host(url: str | None) -> str:
    try:
        h = (urlsplit(str(url or "").replace("{}", "x").replace("{username}", "x")).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def forbidden_reason(name: str, *urls: str | None, source: str | None = None) -> str | None:
    """Why a site must not be checked automatically, or None."""
    n = (name or "").strip().lower()
    if n in FORBIDDEN:
        return f"{name}'s terms forbid automated access"
    if source and source.strip().lower() in FORBIDDEN:
        return f"Third-party mirror of {source} — would sidestep {source}'s restrictions"
    for u in urls:
        h = _host(u)
        if h and any(h == f or h.endswith("." + f) for f in FORBIDDEN_HOSTS):
            return f"{h}: the platform's terms forbid automated access"
    return None


def adult(tags) -> bool:
    return bool({str(t).lower() for t in tags or ()} & ADULT_TAGS)
