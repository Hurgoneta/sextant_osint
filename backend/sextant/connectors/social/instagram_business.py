"""Instagram — Graph API *Business Discovery* (public business/creator accounts only).

Personal accounts cannot be looked up through any official API; they stay manual links. Scrapers and
third-party Instagram mirrors are never used.
"""

from __future__ import annotations

import re

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NeedsCredentials, NotFound, Skipped, SourceUnavailable
from .._common import add_account, avatar_fingerprint

GRAPH = "https://graph.facebook.com/v21.0"
FIELDS = "username,name,biography,website,profile_picture_url,followers_count,media_count,id"


@register
class InstagramBusiness(Connector):
    manifest = Manifest(
        name="instagram_business", title="Instagram Business Discovery", category="social",
        description="Public profile data of Instagram *business and creator* accounts through Meta's Graph API.",
        access="restricted",
        credentials=(Credential("INSTAGRAM_ACCESS_TOKEN", "Instagram Graph API access token",
                                "Token of a Meta app with instagram_basic / Business Discovery access.",
                                obtain_url="https://developers.facebook.com/docs/instagram-platform/"),
                     Credential("INSTAGRAM_BUSINESS_ID", "Your Instagram business account id",
                                "The IG business account the requests are made on behalf of.", secret=False)),
        cost="Free with an approved Meta app", rate_limit=RateLimit(0.5, 1, "Graph API app rate limits"),
        legal="Meta Platform Terms. Only business/creator accounts are discoverable; personal accounts are not.",
        website="https://www.instagram.com", documentation="https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery",
        reliability="high", capabilities=("business/creator account existence", "name", "biography", "website",
                                          "profile picture", "follower and media counts"),
        disclosure=Disclosure(("graph.facebook.com",), "the handle being checked"),
    )

    @transform(inputs=("username",), outputs=("account", "domain", "image", "name"),
               title="Instagram: business/creator account by handle", priority=0.5)
    async def username_lookup(self, entity, ctx):
        u = entity.display
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", u):
            raise Skipped("Not a valid Instagram handle")
        token, ig_id = ctx.require("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ID")
        resp = await ctx.get(f"{GRAPH}/{ig_id}", params={"fields": f"business_discovery.username({u}){{{FIELDS}}}",
                                                         "access_token": token}, cache_ttl=86_400)
        data = ctx.json(resp) if resp.content else {}
        if resp.status == 400:
            err = (data or {}).get("error") or {}
            if err.get("code") in (190, 102):
                raise NeedsCredentials("Instagram token invalid or expired")
            ctx.out.check("instagram", "not_found", detail="No public business/creator account with this handle "
                          "(personal accounts cannot be checked)", platform_title="Instagram")
            raise NotFound("No Instagram business/creator account with this handle")
        if resp.status != 200:
            raise SourceUnavailable(f"Graph API answered HTTP {resp.status}")
        bd = (data or {}).get("business_discovery") or {}
        src = ctx.out.source(f"https://www.instagram.com/{bd.get('username', u)}/", title="Instagram Graph API — business discovery",
                             request=f"handle “{u}”")
        profile = {"username": bd.get("username", u), "display_name": bd.get("name"), "bio": bd.get("biography"),
                   "website": bd.get("website"), "avatar_url": bd.get("profile_picture_url"), "user_id": bd.get("id"),
                   "followers": bd.get("followers_count"), "profile_url": f"https://www.instagram.com/{bd.get('username', u)}/",
                   "avatar_fp": await avatar_fingerprint(ctx, bd.get("profile_picture_url")),
                   "extra": {"media_count": bd.get("media_count"), "account_kind": "business/creator"}}
        acc = add_account(ctx.out, src, platform="instagram", username=profile["username"], profile=profile,
                          link_from=entity.key)
        ctx.out.check("instagram", "found", url=profile["profile_url"], account=acc, platform_title="Instagram")
