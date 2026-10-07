"""Internet Archive Wayback Machine — CDX index of archived captures (historical web references)."""

from __future__ import annotations

from collections import Counter

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ..dns.dns import is_apex

CDX = "https://web.archive.org/cdx/search/cdx"
INTERESTING = ("about", "contact", "team", "impressum", "people", "staff", "cv", "resume", "bio", "author")


def _ts(t: str) -> str:
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) >= 8 else t


@register
class Wayback(Connector):
    manifest = Manifest(
        name="wayback", title="Wayback Machine (Internet Archive)", category="archives",
        description="Historical captures of a domain or page: first/last capture, capture counts, and archived "
                    "about/contact/team pages that may have changed since.",
        access="free", rate_limit=RateLimit(0.5, 1, "the CDX server throttles heavy users"),
        website="https://web.archive.org", documentation="https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server",
        reliability="high", timeout=90,
        capabilities=("first/last archived capture", "number of captures per year", "archived about/contact/team pages"),
        disclosure=Disclosure(("web.archive.org",), "the domain or URL", "public"),
    )

    @transform(inputs=("domain",), outputs=("website",), title="Wayback: capture history and archived about/contact pages",
               priority=0.4, accepts=lambda self, e, info: is_apex(e.value))
    async def domain_history(self, entity, ctx):
        resp = await ctx.get(CDX, params={"url": f"{entity.value}/*", "output": "json", "fl": "timestamp,original,statuscode",
                                          "collapse": "urlkey", "limit": 2000, "filter": "statuscode:200"}, retries=1)
        rows = ctx.json(ctx.expect(resp, what="Wayback CDX")) or []
        rows = rows[1:] if rows and rows[0] and rows[0][0] == "timestamp" else rows
        if not rows:
            raise NotFound("No archived captures")
        src = ctx.out.source(f"https://web.archive.org/web/*/{entity.value}/*", title="Wayback CDX index",
                             request=f"domain “{entity.value}”")
        stamps = sorted(r[0] for r in rows)
        years = Counter(t[:4] for t in stamps)
        ctx.out.props(entity.key, {"archive_first_capture": _ts(stamps[0]), "archive_last_capture": _ts(stamps[-1]),
                                   "archive_urls": len(rows), "archive_captures_by_year": dict(sorted(years.items()))},
                      source=src)
        picked = 0
        for ts, original, _ in rows:
            low = original.lower()
            if any(f"/{w}" in low for w in INTERESTING) and picked < 8:
                snap = f"https://web.archive.org/web/{ts}/{original}"
                w = ctx.out.entity("website", snap, label=f"Archived {original} ({_ts(ts)})", source=src,
                                   props={"url": snap, "title": f"Archived page {original}", "published": _ts(ts),
                                          "category": "archive"})
                ctx.out.link(entity.key, w, "REFERENCED_BY", source=src, evidence="Archived capture of a page on the domain")
                picked += 1

    @transform(inputs=("website", "account"), outputs=(), title="Wayback: archived captures of this page", priority=0.2, auto=False,
               accepts=lambda self, e, info: bool(e.get("url") or e.get("profile_url")))
    async def page_history(self, entity, ctx):
        url = str(entity.get("url") or entity.get("profile_url"))
        resp = await ctx.get(CDX, params={"url": url, "output": "json", "fl": "timestamp,statuscode", "limit": 500})
        rows = ctx.json(ctx.expect(resp, what="Wayback CDX")) or []
        rows = rows[1:] if rows and rows[0] and rows[0][0] == "timestamp" else rows
        if not rows:
            raise NotFound("Never archived")
        stamps = sorted(r[0] for r in rows)
        src = ctx.out.source(f"https://web.archive.org/web/*/{url}", title="Wayback CDX index", request=f"URL “{url}”")
        ctx.out.props(entity.key, {"archive_first_capture": _ts(stamps[0]), "archive_last_capture": _ts(stamps[-1]),
                                   "archive_captures": len(rows),
                                   "archive_url": f"https://web.archive.org/web/{stamps[0]}/{url}"}, source=src)
