"""X (Twitter) — official API v2, pay-per-use (since 2026-01). Never scraped; never via third-party mirrors."""

from __future__ import annotations

import re
from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped
from .._common import add_account, avatar_fingerprint

FIELDS = "created_at,description,location,url,profile_image_url,verified,public_metrics,entities,name"


@register
class XApi(Connector):
    manifest = Manifest(
        name="x_api", title="X (Twitter) API", category="social",
        description="Looks up an X account by handle through the official API v2 with your own developer account.",
        access="metered",
        credentials=(Credential("X_BEARER_TOKEN", "X API bearer token", "App-only bearer token of your X developer app "
                                "(pay-per-use credits).", obtain_url="https://developer.x.com/"),),
        cost="≈ $0.01 per user lookup (pay-per-use, 2026)", cost_per_call_usd=0.01, max_calls_per_investigation=25,
        rate_limit=RateLimit(0.5, 1), website="https://x.com", documentation="https://docs.x.com/x-api/users/user-lookup-by-username",
        legal="X Developer Agreement: use only with your own developer account and within its policy.",
        reliability="high", capabilities=("account existence", "name", "bio", "location", "website (expanded)",
                                          "creation date", "avatar", "follower counts"),
        disclosure=Disclosure(("api.x.com",), "the handle being checked"), auto_run=False,
        opt_in_reason="Paid per call — enable automatic use in Data Sources if you want it in auto mode.",
    )

    @transform(inputs=("username",), outputs=("account", "domain", "image", "name"), title="X: find account by handle",
               priority=0.7)
    async def username_lookup(self, entity, ctx):
        u = entity.display
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", u):
            raise Skipped("Not a valid X handle")
        (token,) = ctx.require("X_BEARER_TOKEN")
        resp = await ctx.get(f"https://api.x.com/2/users/by/username/{quote(u)}", params={"user.fields": FIELDS},
                             headers={"Authorization": f"Bearer {token}"})
        data = ctx.json(ctx.expect(resp, not_found=()))
        user = (data or {}).get("data")
        if not user:
            ctx.out.check("x", "not_found", platform_title="X (Twitter)")
            raise NotFound(f"No X account @{u}")
        urls = [x.get("expanded_url") for x in (((user.get("entities") or {}).get("url") or {}).get("urls") or [])
                if x.get("expanded_url")]
        src = ctx.out.source(f"https://x.com/{user['username']}", title="X API v2 — user lookup", request=f"handle “{u}”")
        avatar = str(user.get("profile_image_url") or "").replace("_normal.", "_400x400.") or None
        profile = {"username": user["username"], "display_name": user.get("name"), "bio": user.get("description"),
                   "location": user.get("location"), "website": urls[0] if urls else None, "links": urls[1:],
                   "created_at": user.get("created_at"), "avatar_url": avatar, "user_id": user.get("id"),
                   "followers": (user.get("public_metrics") or {}).get("followers_count"),
                   "profile_url": f"https://x.com/{user['username']}", "avatar_fp": await avatar_fingerprint(ctx, avatar),
                   "extra": {"verified": user.get("verified")}}
        acc = add_account(ctx.out, src, platform="x", username=user["username"], profile=profile, link_from=entity.key)
        ctx.out.check("x", "found", url=profile["profile_url"], account=acc, platform_title="X (Twitter)")
