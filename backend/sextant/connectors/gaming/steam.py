"""Steam — Web API (free key): vanity URL → SteamID → public player summary."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped
from .._common import add_account, avatar_fingerprint

API = "https://api.steampowered.com"


@register
class Steam(Connector):
    manifest = Manifest(
        name="steam", title="Steam Web API", category="gaming",
        description="Resolves a Steam custom URL name and reads the public player summary.", access="free_key",
        credentials=(Credential("STEAM_API_KEY", "Steam Web API key", obtain_url="https://steamcommunity.com/dev/apikey"),),
        cost="Free (100,000 calls/day)", rate_limit=RateLimit(1.0, 2), website="https://steamcommunity.com",
        documentation="https://developer.valvesoftware.com/wiki/Steam_Web_API", reliability="high",
        legal="Steam Web API Terms of Use.",
        capabilities=("account by custom URL", "persona name", "real name (if public)", "country", "creation date", "avatar"),
        disclosure=Disclosure(("api.steampowered.com",), "the custom URL name being checked"),
    )

    @transform(inputs=("username",), outputs=("account", "image", "name", "location"), title="Steam: account by custom URL",
               priority=0.5, estimated_requests=2)
    async def username_lookup(self, entity, ctx):
        u = entity.display
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", u):
            raise Skipped("Not a valid Steam custom URL")
        (key,) = ctx.require("STEAM_API_KEY")
        r1 = await ctx.get(f"{API}/ISteamUser/ResolveVanityURL/v1/", params={"key": key, "vanityurl": u})
        res = (ctx.json(ctx.expect(r1)) or {}).get("response") or {}
        if res.get("success") != 1:
            ctx.out.check("steam", "not_found", platform_title="Steam")
            raise NotFound(f"No Steam account with custom URL “{u}”")
        sid = res["steamid"]
        r2 = await ctx.get(f"{API}/ISteamUser/GetPlayerSummaries/v2/", params={"key": key, "steamids": sid})
        players = ((ctx.json(ctx.expect(r2)) or {}).get("response") or {}).get("players") or []
        p = players[0] if players else {}
        created = p.get("timecreated")
        src = ctx.out.source(f"https://steamcommunity.com/profiles/{sid}", title="Steam Web API — player summary",
                             request=f"custom URL “{u}”")
        profile = {"username": u, "display_name": p.get("realname") or p.get("personaname"), "user_id": sid,
                   "location": p.get("loccountrycode"), "avatar_url": p.get("avatarfull"),
                   "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None,
                   "profile_url": p.get("profileurl") or f"https://steamcommunity.com/id/{u}",
                   "avatar_fp": await avatar_fingerprint(ctx, p.get("avatarfull")),
                   "extra": {"persona_name": p.get("personaname"), "visibility": p.get("communityvisibilitystate")}}
        acc = add_account(ctx.out, src, platform="steam", username=u, profile=profile, link_from=entity.key)
        ctx.out.check("steam", "found", url=profile["profile_url"], account=acc, platform_title="Steam")
