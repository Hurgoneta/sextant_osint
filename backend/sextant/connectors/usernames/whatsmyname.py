"""Username sweep across 700+ sites with the community WhatsMyName detection rules.

WhatsMyName (https://github.com/WebBreacher/WhatsMyName, CC BY-SA 4.0) publishes, per site, the public
profile URL and how to tell "exists" from "doesn't exist". Sextant downloads the dataset (no personal
data involved) and makes **one ordinary request per site** for the profile URL, through the guarded
HTTP client (SSRF guard, rate limits, honest User-Agent).

Policy (shown in the catalog):
* No impersonation: dataset headers other than Accept/Content-Type are dropped; cookies are never sent.
* Anti-bot protection (Cloudflare challenges, CAPTCHAs, 403/429) is never bypassed → *blocked*.
* robots.txt is respected by default → *skipped*, with the link for the investigator.
* Platforms whose terms forbid automated lookups (Facebook, Instagram, X, TikTok, Threads, Snapchat, LinkedIn),
  entries using internal APIs or third-party mirrors of them, and adult/dating sites (unless enabled) are excluded.
* "Found" means the page matched the dataset's marker; markers go stale, and existence is never identity.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser

from ...registry.platforms import WMN_CATEGORIES, platform_for_host
from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import SourceUnavailable
from ...sdk.http import FetchError
from ...util.extract import parse_html
from ...util.normalize import name_tokens
from .._common import add_account, avatar_fingerprint

DATASET_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json"
DATASET_REFRESH = 7 * 86_400
ROBOTS_TOKEN = "Sextant"
CONCURRENCY = 12
MAX_PAGE = 1_500_000
ADULT_CATEGORIES = {"xx NSFW xx", "dating"}
SAFE_HEADERS = {"content-type", "accept", "accept-language"}
EXCLUDED = {
    "facebook": "Facebook's terms forbid automated access",
    "instagram": "Instagram's terms forbid automated access",
    "instagram (imginn)": "Third-party mirror of Instagram — would sidestep Instagram's restrictions",
    "instagram_archives": "Third-party archive of Instagram — would sidestep Instagram's restrictions",
    "x": "Uses an internal X endpoint; X's terms forbid automated access",
    "tiktok": "TikTok's terms forbid automated access",
    "threads": "Threads' terms forbid automated access",
    "snapchat": "Snapchat's terms forbid automated access",
    "linkedin": "LinkedIn's terms forbid automated access",
    "reddit": "Checked by the dedicated Reddit connector (official API)",
}
DEDICATED_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "news.ycombinator.com", "dev.to", "keybase.io",
                   "mastodon.social", "bsky.app", "lichess.org", "chess.com", "hub.docker.com", "gravatar.com",
                   "reddit.com", "huggingface.co", "crates.io", "lobste.rs", "lemmy.world", "npmjs.com")
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "just a moment...", "attention required! | cloudflare", "ddos-guard",
                     "g-recaptcha", "h-captcha", "hcaptcha", "smartcaptcha", "checking your browser", "anubis",
                     "verify you are human", "are you a robot")
TITLE_STOPWORDS = {"profile", "profiles", "user", "users", "member", "members", "page", "on", "official", "account",
                   "channel", "photos", "videos", "posts", "followers", "following", "and", "the", "of", "see", "s",
                   "home", "about", "blog", "portfolio", "music", "playlists", "games", "overview", "public", "by"}
INFOSTEALER_SITES = {"hudsonrock"}
MENTION_SITES = {"internet archive user contribution search", "internet archive username mentions", "filmot channel search",
                 "filmot unlisted videos", "unlisted videos", "sepia search (peertube)", "searchengines"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class Site:
    name: str
    category: str
    uri_check: str
    uri_pretty: str
    e_code: int
    e_string: str
    m_code: int
    m_string: str
    post_body: str | None = None
    headers: dict | None = None
    strip_bad_char: str = ""

    def account(self, username: str) -> str:
        return "".join(c for c in username if c not in self.strip_bad_char) if self.strip_bad_char else username

    def url(self, username: str, pretty: bool = False) -> str:
        return (self.uri_pretty if pretty else self.uri_check).replace("{account}", quote(self.account(username), safe=""))

    @property
    def host(self) -> str:
        return (urlsplit(self.uri_check.replace("{account}", "x")).hostname or "").lower()


def load_sites(doc: dict, include_adult: bool, platforms: list[str] | None = None) -> tuple[list[Site], list[dict]]:
    """Validates the dataset and applies policy. Returns (sites to check, excluded with reasons)."""
    sites, excluded = [], []
    wanted = {p.lower() for p in platforms or []}
    for raw in doc.get("sites") or []:
        try:
            uri = str(raw["uri_check"])
            site = Site(name=str(raw["name"]), category=str(raw.get("cat") or "misc"), uri_check=uri,
                        uri_pretty=str(raw.get("uri_pretty") or uri), e_code=int(raw["e_code"]),
                        e_string=str(raw.get("e_string") or ""), m_code=int(raw["m_code"]),
                        m_string=str(raw.get("m_string") or ""), post_body=raw.get("post_body"),
                        headers=raw.get("headers") or None, strip_bad_char=str(raw.get("strip_bad_char") or ""))
        except (KeyError, TypeError, ValueError):
            continue
        if raw.get("valid") is False or not uri.startswith(("https://", "http://")) or "{account}" not in uri:
            continue
        reason = EXCLUDED.get(site.name.lower())
        if not reason and any(site.host == h or site.host.endswith("." + h) for h in DEDICATED_HOSTS):
            reason = "Checked by a dedicated connector (official API)"
        if not reason and site.headers and any(k.lower() == "cookie" for k in site.headers):
            reason = "Check requires sending a cookie"
        if not reason and site.category in ADULT_CATEGORIES and not include_adult:
            reason = "Adult/dating site — enable in Settings to include"
        if not reason and wanted:
            known = platform_for_host(site.host)
            ids = {slug(site.name), site.category.lower(), (known.id if known else ""), (known.category if known else ""),
                   WMN_CATEGORIES.get(site.category, "other")}
            if not ids & wanted:
                reason = "Not among the platforms selected for this investigation"
        if reason:
            excluded.append({"name": site.name, "category": site.category, "reason": reason, "url": site.uri_pretty})
        else:
            sites.append(site)
    return sites, excluded


def evaluate(site: Site, status: int, body: str) -> tuple[str, str]:
    exists = status == site.e_code and (site.e_string in body if site.e_string else True)
    missing = status == site.m_code and (site.m_string in body if site.m_string else True)
    if exists and not missing:
        return "found", "Profile page matched the site's 'account exists' marker"
    if missing and not exists:
        return "not_found", ""
    low = body[:30_000].lower()
    if status in (401, 403, 429, 503) or any(m in low for m in CHALLENGE_MARKERS):
        return "blocked", "Blocked by the site's anti-bot protection (not bypassed)"
    return "unknown", f"Inconclusive (HTTP {status}) — the site's detection rule may be outdated"


_TITLE_PREFIX = re.compile(r"^(?:pay|userpage of|profile of|photos by|videos by|music by|posts by|stories by)\s+(.+?)(?:\s+using\b.*)?$", re.I)
_POSSESSIVE = re.compile(r"^(.+?)['’]s?\b(?:\s|$)")
_NOT_A_NAME = {"favorite", "favorites", "items", "item", "collection", "closet", "shop", "store", "userpage", "using",
               "pay", "wishlist", "list", "library", "gallery", "playlist", "feed", "dashboard", "welcome", "log", "in"}


def display_name_from_title(title: str, username: str, site_name: str) -> str | None:
    """'Jane Doe (@jdoe) • Pinterest' -> 'Jane Doe'. None unless it looks like a person's name — a wrong display
    name would become wrong correlation evidence."""
    if not title:
        return None
    head = re.split(r"\s[|•·–—-]{1,2}\s|\s\(|\s@|:\s", title, maxsplit=1)[0].strip()
    m = _TITLE_PREFIX.match(head)
    if m:
        head = m.group(1).strip()
    if "'" in head or "’" in head:
        m = _POSSESSIVE.match(head)
        head = m.group(1).strip() if m else head
    drop = set(name_tokens(username)) | set(name_tokens(site_name)) | TITLE_STOPWORDS
    if any(w.lower().strip(".,") in _NOT_A_NAME for w in head.split()):
        return None
    toks = [t for t in name_tokens(head) if t not in drop]
    if 2 <= len(toks) <= 4 and all(t.isalpha() for t in toks) and len(head) <= 60:
        return head
    return None


def site_kind(site: Site) -> str:
    n = site.name.lower()
    if n in INFOSTEALER_SITES:
        return "infostealer"
    if n in MENTION_SITES or site.category == "archived":
        return "mention"
    return "account"


def page_profile(site: Site, username: str, url: str, body: str, content_type: str) -> dict:
    if "html" not in content_type:
        return {"profile_url": url}
    page = parse_html(body[:MAX_PAGE], url)
    title = page.meta.get("og:title") or page.meta.get("twitter:title") or page.title
    bio = (page.meta.get("og:description") or page.meta.get("description") or "")[:300]
    image = page.meta.get("og:image")
    return {"display_name": display_name_from_title(title, username, site.name), "bio": bio or None,
            "avatar_url": image if image and image.startswith("https://") else None,
            "links": page.me_links[:10], "verified_links": page.me_links[:10], "profile_url": url}


class RobotsCache:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._parsers:
                self._parsers[origin] = await self._fetch(origin)
        rp = self._parsers[origin]
        return True if rp is None else rp.can_fetch(ROBOTS_TOKEN, url)

    async def _fetch(self, origin: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            resp = await self.ctx.get(origin + "/robots.txt", max_bytes=500_000, retries=0, cache_ttl=86_400)
        except FetchError:
            return None
        if resp.status in (401, 403):
            rp.disallow_all = True
            return rp
        if resp.status != 200 or "html" in resp.headers.get("content-type", ""):
            return None
        rp.parse(resp.text.splitlines())
        return rp


@register
class WhatsMyName(Connector):
    manifest = Manifest(
        name="whatsmyname", title="Username on 700+ sites (WhatsMyName)", category="usernames",
        description="Checks whether a username exists on hundreds of social, gaming, coding, art, music, forum and "
                    "regional sites using the community WhatsMyName rules. Respects robots.txt; never bypasses anti-bot "
                    "protection.",
        access="free", rate_limit=RateLimit(15, 15), website="https://github.com/WebBreacher/WhatsMyName",
        documentation="https://github.com/WebBreacher/WhatsMyName#format", reliability="medium", official_api=False,
        access_method="web_page", license="CC BY-SA 4.0 (dataset by WebBreacher and contributors)", timeout=900,
        max_concurrency=1, coverage="700+ sites (minus policy exclusions)",
        capabilities=("account existence on 700+ sites", "display name / bio / rel=me links from the public page",
                      "profile photo fingerprint", "per-site check status"),
        disclosure=Disclosure(("hundreds of individual websites", "raw.githubusercontent.com (dataset)"),
                              "the username — as one public-profile request to each site"),
    )

    async def _dataset(self, ctx) -> dict:
        path = ctx.data_dir / "wmn" / "wmn-data.json" if ctx.data_dir else None
        cached = None
        if path and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
            if cached and time.time() - path.stat().st_mtime < DATASET_REFRESH:
                return cached
        try:
            resp = await ctx.get(DATASET_URL, cache_ttl=None, max_bytes=5_000_000, retries=1)
            doc = resp.json() if resp.status == 200 else None
        except (FetchError, ValueError):
            doc = None
        if not doc or not isinstance(doc.get("sites"), list):
            if cached:
                return cached
            raise SourceUnavailable("Could not download the WhatsMyName dataset")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, path)
        return doc

    @transform(inputs=("username",), outputs=("account", "website", "breach", "image", "name"),
               title="Username sweep across 700+ sites (WhatsMyName)", priority=0.65, estimated_requests=700)
    async def sweep(self, entity, ctx):
        username = entity.display
        sites, excluded = load_sites(await self._dataset(ctx), bool(ctx.info.option("include_adult")),
                                     ctx.info.option("platforms"))
        respect_robots = ctx.info.option("respect_robots", True) is not False
        robots = RobotsCache(ctx)
        sem = asyncio.Semaphore(CONCURRENCY)
        host_locks: dict[str, asyncio.Lock] = {}
        counts: dict[str, int] = {}

        async def check(site: Site) -> None:
            pretty = site.url(username, pretty=True)
            title = (platform_for_host(site.host).name if platform_for_host(site.host) else site.name)
            pid = platform_for_host(site.host).id if platform_for_host(site.host) else f"wmn:{slug(site.name)}"
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", site.account(username)):
                ctx.out.check(pid, "skipped", detail="Username format not supported here", url=pretty,
                              platform_title=title, category=site.category)
                return
            async with sem, host_locks.setdefault(site.host, asyncio.Lock()):
                url = site.url(username)
                if respect_robots and not await robots.allowed(url):
                    ctx.out.check(pid, "skipped", url=pretty, platform_title=title, category=site.category,
                                  detail="robots.txt asks automated clients not to fetch this page — open the link yourself")
                    counts["skipped"] = counts.get("skipped", 0) + 1
                    return
                headers = {k: v for k, v in (site.headers or {}).items() if k.lower() in SAFE_HEADERS}
                try:
                    if site.post_body:
                        resp = await ctx.post(url, content=site.post_body.replace("{account}", site.account(username)).encode(),
                                              headers=headers, follow_redirects=False, max_bytes=MAX_PAGE, retries=0)
                    else:
                        resp = await ctx.get(url, headers=headers, follow_redirects=False, max_bytes=MAX_PAGE, retries=0,
                                             cache_ttl=86_400)
                except FetchError as exc:
                    if exc.kind == "budget":
                        raise
                    ctx.out.check(pid, "blocked" if exc.kind == "rate_limited" else "error", url=pretty,
                                  platform_title=title, category=site.category, detail=f"Request failed ({exc.kind})")
                    counts["error"] = counts.get("error", 0) + 1
                    return
            status, detail = evaluate(site, resp.status, resp.text)
            counts[status] = counts.get(status, 0) + 1
            if status != "found":
                ctx.out.check(pid, status, detail=detail, url=pretty, platform_title=title, category=site.category)
                return
            kind = site_kind(site)
            src = ctx.out.source(pretty, title=f"{site.name} public page", kind="web_page",
                                 request=f"profile URL for “{username}”")
            if kind == "infostealer":
                b = ctx.out.entity("breach", f"{slug(site.name)}:{username.casefold()}", label=f"Info-stealer logs ({site.name})",
                                   source=src, props={"data_classes": ["Credentials captured by info-stealer malware (not retrieved)"],
                                                      "description": f"{site.name} reports infected-machine credentials for "
                                                                     f"the username “{username}” — may be someone else."})
                ctx.out.link(entity.key, b, "EXPOSED_IN", source=src, confidence=0.6, evidence="Same username in info-stealer data")
            elif kind == "mention":
                w = ctx.out.entity("website", pretty, label=f"{site.name}: results for “{username}”", source=src,
                                   props={"url": pretty, "title": f"{site.name} — {username}", "category": "website",
                                          "matched": ["username"]})
                ctx.out.link(entity.key, w, "MENTIONED_IN", source=src, evidence="Username found in archived/indexed content")
            else:
                profile = page_profile(site, username, pretty, resp.text, resp.headers.get("content-type", ""))
                profile["avatar_fp"] = await avatar_fingerprint(ctx, profile.get("avatar_url"))
                acc = add_account(ctx.out, src, platform=pid, username=site.account(username), profile=profile,
                                  link_from=entity.key, platform_title=title)
                if acc:
                    ctx.out.prop(acc, "category", site.category, source=src)
            ctx.out.check(pid, "found", detail=detail, url=pretty, platform_title=title, category=site.category)

        await asyncio.gather(*(check(s) for s in sites))
        for e in excluded:
            known = platform_for_host((urlsplit(e["url"].replace("{account}", "x")).hostname or ""))
            ctx.out.check(known.id if known else f"wmn:{slug(e['name'])}", "skipped", detail=f"Not automated: {e['reason']}",
                          url=e["url"].replace("{account}", quote(username, safe="")), platform_title=e["name"],
                          category=e["category"])
        ctx.out.set_status("found" if counts.get("found") else "empty",
                           f"{len(sites)} sites checked: {counts.get('found', 0)} found, {counts.get('not_found', 0)} not "
                           f"found, {counts.get('blocked', 0)} blocked, {counts.get('unknown', 0)} inconclusive, "
                           f"{counts.get('skipped', 0)} skipped (robots.txt), {counts.get('error', 0)} unreachable; "
                           f"{len(excluded)} excluded by policy")
