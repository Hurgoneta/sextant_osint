"""Bluesky — public AppView API (no account needed)."""

from __future__ import annotations

import re
from urllib.parse import quote

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped, SourceUnavailable
from .._common import accepts_named_person, add_account, avatar_fingerprint, person_names

API = "https://public.api.bsky.app/xrpc"


def _profile(d: dict) -> dict:
    handle = d.get("handle")
    return {"username": handle, "display_name": d.get("displayName"), "bio": d.get("description"),
            "created_at": d.get("createdAt"), "avatar_url": d.get("avatar"), "followers": d.get("followersCount"),
            "profile_url": f"https://bsky.app/profile/{handle}", "user_id": d.get("did"),
            "extra": {"posts": d.get("postsCount"), "follows": d.get("followsCount")}}


@register
class Bluesky(Connector):
    manifest = Manifest(
        name="bluesky", title="Bluesky", category="social",
        description="Bluesky profiles by handle (username.bsky.social or a custom-domain handle) and people search by name.",
        access="free", rate_limit=RateLimit(1.0, 3, "public AppView; 3,000 requests / 5 min per IP"),
        website="https://bsky.app", documentation="https://docs.bsky.app/docs/api/app-bsky-actor-get-profile",
        reliability="high", coverage="All Bluesky (AT Protocol) accounts served by the public AppView",
        capabilities=("account by handle", "custom-domain handle for a domain (proves domain control)",
                      "people search by name", "display name, bio, creation date, avatar, followers"),
        disclosure=Disclosure(("public.api.bsky.app",), "handles, domains and names being checked"),
    )

    async def _get_profile(self, ctx, actor: str):
        resp = await ctx.get(f"{API}/app.bsky.actor.getProfile", params={"actor": actor})
        data = ctx.json(resp) if resp.content else None
        if resp.status == 400 and isinstance(data, dict) and "not found" in str(data.get("message", "")).lower():
            return resp, None
        if resp.status != 200 or not isinstance(data, dict):
            raise SourceUnavailable(f"Bluesky answered HTTP {resp.status}")
        return resp, data

    @transform(inputs=("username",), outputs=("account", "image", "name"), title="Bluesky: find account by handle",
               priority=0.7)
    async def username_lookup(self, entity, ctx):
        u = entity.display.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,17}", u):
            raise Skipped("Not a valid bsky.social handle")
        resp, data = await self._get_profile(ctx, f"{u}.bsky.social")
        if data is None:
            ctx.out.check("bluesky", "not_found", detail=f"No {u}.bsky.social (custom-domain handles are checked from domains)",
                          platform_title="Bluesky")
            raise NotFound(f"No Bluesky handle {u}.bsky.social")
        src = ctx.out.source(resp.url, title="Bluesky AppView — getProfile", request=f"handle “{u}.bsky.social”")
        p = _profile(data)
        p["avatar_fp"] = await avatar_fingerprint(ctx, p.get("avatar_url"))
        acc = add_account(ctx.out, src, platform="bluesky", username=p["username"], profile=p, link_from=entity.key)
        ctx.out.check("bluesky", "found", url=p["profile_url"], account=acc, platform_title="Bluesky")

    @transform(inputs=("domain",), outputs=("account",), title="Bluesky: is this domain a Bluesky handle?", priority=0.6,
               accepts=lambda self, e, info: not e.get("subdomain"))
    async def domain_handle(self, entity, ctx):
        """A custom-domain handle is only possible for whoever controls the domain's DNS or web server."""
        resp, data = await self._get_profile(ctx, entity.value)
        if data is None:
            raise NotFound(f"{entity.value} is not used as a Bluesky handle")
        src = ctx.out.source(resp.url, title="Bluesky AppView — getProfile", request=f"handle “{entity.value}”")
        p = _profile(data)
        acc = add_account(ctx.out, src, platform="bluesky", username=p["username"], profile=p, found_by="domain handle")
        ctx.out.link(acc, entity.key, "USES", source=src, evidence="The Bluesky handle is this domain (requires control of it)")

    @transform(inputs=("person",), outputs=("account",), title="Bluesky: people search by name", priority=0.4,
               accepts=accepts_named_person, sends="full name “{value}”")
    async def name_search(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        resp = await ctx.get(f"{API}/app.bsky.actor.searchActors", params={"q": name, "limit": 5})
        data = ctx.json(ctx.expect(resp))
        src = ctx.out.source(f"https://bsky.app/search?q={quote(name)}", title="Bluesky people search", request=f"name “{name}”")
        for d in (data or {}).get("actors") or []:
            p = _profile(d)
            add_account(ctx.out, src, platform="bluesky", username=p["username"], profile=p, found_by="name search")
