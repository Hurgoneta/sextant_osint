"""Reddit — official OAuth API (app-only). Since Reddit's Responsible Builder Policy (Nov 2025) new API
access requires approval; unauthenticated JSON returns 403. Without approved credentials the connector
shows "API key required" and never reports "not found"."""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import Blocked, NeedsCredentials, NotFound, Skipped, SourceUnavailable
from .._common import add_account, avatar_fingerprint


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return None


@register
class Reddit(Connector):
    manifest = Manifest(
        name="reddit", title="Reddit", category="forums",
        description="Checks whether a username exists on Reddit and reads the public profile (official OAuth API).",
        access="restricted",
        credentials=(Credential("REDDIT_CLIENT_ID", "Reddit app client id", "Of an approved Reddit API app.", secret=False,
                                obtain_url="https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy"),
                     Credential("REDDIT_CLIENT_SECRET", "Reddit app secret", "Secret of the approved app.")),
        cost="Free with approved access", rate_limit=RateLimit(0.5, 2, "100 requests/minute per OAuth client"),
        legal="Reddit's Responsible Builder Policy: API access must be requested and approved; data use is limited to "
              "the approved purpose.",
        website="https://www.reddit.com", documentation="https://www.reddit.com/dev/api/", reliability="high",
        capabilities=("account existence", "profile title and description", "creation date", "karma", "avatar"),
        disclosure=Disclosure(("www.reddit.com", "oauth.reddit.com"), "the username being checked"),
    )
    _token: tuple[str, float] | None = None

    async def _oauth(self, ctx) -> str:
        cid, secret = ctx.require("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = await ctx.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                              headers={"Authorization": f"Basic {auth}"})
        if resp.status != 200:
            raise NeedsCredentials(f"Reddit OAuth failed (HTTP {resp.status}) — check the app credentials/approval")
        data = ctx.json(resp)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise SourceUnavailable("Reddit OAuth did not return an access token")
        type(self)._token = (token, time.time() + float(data.get("expires_in") or 3600))
        return token

    @transform(inputs=("username",), outputs=("account", "image"), title="Reddit: find account by username", priority=0.7)
    async def username_lookup(self, entity, ctx):
        u = entity.display
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", u):
            raise Skipped("Not a valid Reddit username")
        token = await self._oauth(ctx)
        resp = await ctx.get(f"https://oauth.reddit.com/user/{quote(u)}/about", headers={"Authorization": f"Bearer {token}"})
        if resp.status == 404:
            ctx.out.check("reddit", "not_found", platform_title="Reddit")
            raise NotFound(f"No Reddit user “{u}”")
        if resp.status in (401, 403):
            raise Blocked(f"Reddit refused the request (HTTP {resp.status})")
        if resp.status != 200:
            raise SourceUnavailable(f"Reddit answered HTTP {resp.status}")
        d = (ctx.json(resp) or {}).get("data") or {}
        sub = d.get("subreddit") or {}
        name = d.get("name") or u
        icon = str(d.get("icon_img") or "").split("?", 1)[0] or None
        src = ctx.out.source(f"https://www.reddit.com/user/{name}/about.json", title="Reddit API — user about",
                             request=f"username “{u}”")
        profile = {"username": name, "display_name": sub.get("title") or None, "bio": sub.get("public_description") or None,
                   "created_at": _iso(d.get("created_utc")), "avatar_url": icon,
                   "avatar_fp": await avatar_fingerprint(ctx, icon), "profile_url": f"https://www.reddit.com/user/{name}",
                   "extra": {"karma": d.get("total_karma"), "suspended": bool(d.get("is_suspended"))}}
        acc = add_account(ctx.out, src, platform="reddit", username=name, profile=profile, link_from=entity.key)
        ctx.out.check("reddit", "found", url=profile["profile_url"], account=acc, platform_title="Reddit")
