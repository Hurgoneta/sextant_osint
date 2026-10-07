"""YouTube (Data API v3, free key) and Twitch (Helix, free client credentials)."""

from __future__ import annotations

import re
import time

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NeedsCredentials, NotFound, Skipped, SourceUnavailable
from .._common import add_account, avatar_fingerprint


@register
class YouTube(Connector):
    manifest = Manifest(
        name="youtube", title="YouTube Data API", category="video",
        description="Finds a YouTube channel by @handle and reads its public channel details.",
        access="free_key",
        credentials=(Credential("YOUTUBE_API_KEY", "YouTube Data API v3 key", "Free Google Cloud API key with the YouTube "
                                "Data API v3 enabled.", obtain_url="https://console.cloud.google.com/apis/library/youtube.googleapis.com"),),
        cost="Free (10,000 quota units/day; a handle lookup costs 1)", rate_limit=RateLimit(2.0, 4),
        website="https://www.youtube.com", documentation="https://developers.google.com/youtube/v3/docs/channels/list",
        reliability="high", capabilities=("channel by handle", "title, description, country, creation date",
                                          "subscriber/video counts", "avatar"),
        disclosure=Disclosure(("www.googleapis.com",), "the handle being checked"),
    )

    @transform(inputs=("username",), outputs=("account", "image", "name"), title="YouTube: channel by @handle", priority=0.6)
    async def username_lookup(self, entity, ctx):
        u = entity.display
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", u):
            raise Skipped("Not a valid YouTube handle")
        (key,) = ctx.require("YOUTUBE_API_KEY")
        resp = await ctx.get("https://www.googleapis.com/youtube/v3/channels",
                             params={"part": "snippet,statistics", "forHandle": f"@{u}", "key": key})
        if resp.status in (400, 403) and b"API key" in resp.content:
            raise NeedsCredentials("YouTube rejected the API key")
        items = (ctx.json(ctx.expect(resp)) or {}).get("items") or []
        if not items:
            ctx.out.check("youtube", "not_found", platform_title="YouTube")
            raise NotFound(f"No YouTube channel @{u}")
        ch = items[0]
        sn, st = ch.get("snippet") or {}, ch.get("statistics") or {}
        thumb = ((sn.get("thumbnails") or {}).get("high") or (sn.get("thumbnails") or {}).get("default") or {}).get("url")
        handle = (sn.get("customUrl") or f"@{u}").lstrip("@")
        src = ctx.out.source(f"https://www.youtube.com/channel/{ch.get('id')}", title="YouTube Data API — channels.list",
                             request=f"handle “@{u}”")
        profile = {"username": handle, "display_name": sn.get("title"), "bio": sn.get("description"),
                   "location": sn.get("country"), "created_at": sn.get("publishedAt"), "avatar_url": thumb,
                   "user_id": ch.get("id"), "profile_url": f"https://www.youtube.com/@{handle}",
                   "followers": int(st["subscriberCount"]) if str(st.get("subscriberCount", "")).isdigit() else None,
                   "avatar_fp": await avatar_fingerprint(ctx, thumb), "extra": {"videos": st.get("videoCount")}}
        acc = add_account(ctx.out, src, platform="youtube", username=handle, profile=profile, link_from=entity.key)
        ctx.out.check("youtube", "found", url=profile["profile_url"], account=acc, platform_title="YouTube")


@register
class Twitch(Connector):
    manifest = Manifest(
        name="twitch", title="Twitch Helix API", category="video",
        description="Finds a Twitch account by login name.", access="free_key",
        credentials=(Credential("TWITCH_CLIENT_ID", "Twitch client id", secret=False, obtain_url="https://dev.twitch.tv/console/apps"),
                     Credential("TWITCH_CLIENT_SECRET", "Twitch client secret")),
        cost="Free", rate_limit=RateLimit(2.0, 4, "800 points/minute per app token"), website="https://www.twitch.tv",
        documentation="https://dev.twitch.tv/docs/api/reference/#get-users", reliability="high",
        capabilities=("account existence", "display name", "description", "creation date", "avatar"),
        disclosure=Disclosure(("id.twitch.tv", "api.twitch.tv"), "the login being checked"),
    )
    _token: tuple[str, float] | None = None

    async def _app_token(self, ctx) -> tuple[str, str]:
        cid, secret = ctx.require("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
        if self._token and self._token[1] > time.time() + 60:
            return cid, self._token[0]
        resp = await ctx.post("https://id.twitch.tv/oauth2/token",
                              data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
        if resp.status != 200:
            raise NeedsCredentials(f"Twitch rejected the client credentials (HTTP {resp.status})")
        d = ctx.json(resp)
        token = d.get("access_token") if isinstance(d, dict) else None
        if not token:
            raise SourceUnavailable("Twitch did not return an access token")
        type(self)._token = (token, time.time() + float(d.get("expires_in") or 3600))
        return cid, token

    @transform(inputs=("username",), outputs=("account", "image"), title="Twitch: account by login", priority=0.5)
    async def username_lookup(self, entity, ctx):
        u = entity.display.lower()
        if not re.fullmatch(r"[a-z0-9_]{4,25}", u):
            raise Skipped("Not a valid Twitch login")
        cid, token = await self._app_token(ctx)
        resp = await ctx.get("https://api.twitch.tv/helix/users", params={"login": u},
                             headers={"Client-Id": cid, "Authorization": f"Bearer {token}"})
        data = (ctx.json(ctx.expect(resp)) or {}).get("data") or []
        if not data:
            ctx.out.check("twitch", "not_found", platform_title="Twitch")
            raise NotFound(f"No Twitch account “{u}”")
        d = data[0]
        if not isinstance(d, dict) or not d.get("login"):
            raise SourceUnavailable("Twitch returned an unexpected user record")
        src = ctx.out.source(f"https://www.twitch.tv/{d['login']}", title="Twitch Helix — users", request=f"login “{u}”")
        profile = {"username": d["login"], "display_name": d.get("display_name"), "bio": d.get("description"),
                   "created_at": d.get("created_at"), "avatar_url": d.get("profile_image_url"), "user_id": d.get("id"),
                   "profile_url": f"https://www.twitch.tv/{d['login']}",
                   "avatar_fp": await avatar_fingerprint(ctx, d.get("profile_image_url")),
                   "extra": {"broadcaster_type": d.get("broadcaster_type")}}
        acc = add_account(ctx.out, src, platform="twitch", username=d["login"], profile=profile, link_from=entity.key)
        ctx.out.check("twitch", "found", url=profile["profile_url"], account=acc, platform_title="Twitch")
