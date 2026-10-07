"""Stack Exchange — public API (Stack Overflow users by display name)."""

from __future__ import annotations

from datetime import datetime, timezone

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from .._common import accepts_named_person, add_account, person_names

API = "https://api.stackexchange.com/2.3/users"


def _profile(u: dict) -> dict:
    created = u.get("creation_date")
    return {"username": f"{u.get('user_id')}", "display_name": u.get("display_name"), "location": u.get("location"),
            "website": u.get("website_url") or None, "avatar_url": u.get("profile_image"), "profile_url": u.get("link"),
            "user_id": str(u.get("user_id")), "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else None,
            "extra": {"reputation": u.get("reputation"), "about": u.get("about_me")}}


@register
class StackExchange(Connector):
    manifest = Manifest(
        name="stackexchange", title="Stack Exchange (Stack Overflow)", category="forums",
        description="Searches Stack Overflow users by display name (exact handle or full name).", access="free",
        credentials=(Credential("STACKEXCHANGE_KEY", "Stack Exchange app key", "Optional; raises the daily quota from 300 to "
                                "10,000 requests.", required=False, secret=False, obtain_url="https://stackapps.com/apps/oauth/register"),),
        cost="Free", rate_limit=RateLimit(0.5, 2, "300 requests/day per IP without key"),
        website="https://stackoverflow.com", documentation="https://api.stackexchange.com/docs/users", reliability="high",
        capabilities=("users by display name", "location", "website", "creation date", "reputation", "avatar"),
        disclosure=Disclosure(("api.stackexchange.com",), "a username or full name as display-name search"),
    )

    async def _search(self, ctx, q: str):
        params = {"inname": q, "site": "stackoverflow", "pagesize": 5, "order": "desc", "sort": "reputation"}
        key = ctx.credential("STACKEXCHANGE_KEY")
        if key:
            params["key"] = key
        resp = await ctx.get(API, params=params)
        return resp, (ctx.json(ctx.expect(resp)) or {}).get("items") or []

    @transform(inputs=("username",), outputs=("account",), title="Stack Overflow: user with this exact display name",
               priority=0.4)
    async def username_lookup(self, entity, ctx):
        resp, items = await self._search(ctx, entity.display)
        src = ctx.out.source(f"https://stackoverflow.com/users?tab=Reputation&search={entity.display}",
                             title="Stack Exchange API — users", request=f"display name “{entity.display}”")
        exact = [u for u in items if str(u.get("display_name", "")).casefold() == entity.display.casefold()]
        for u in exact[:3]:
            add_account(ctx.out, src, platform="stackoverflow", username=str(u.get("user_id")), profile=_profile(u),
                        link_from=entity.key, found_by="display name", link_confidence=0.7)
        ctx.out.check("stackoverflow", "found" if exact else "not_found", platform_title="Stack Overflow",
                      detail="Matched by display name (not a unique handle)")

    @transform(inputs=("person",), outputs=("account",), title="Stack Overflow: users with this full name", priority=0.3,
               accepts=accepts_named_person, sends="full name “{value}”")
    async def name_search(self, entity, ctx):
        name = person_names(entity, ctx.info)[0]
        resp, items = await self._search(ctx, name)
        src = ctx.out.source(f"https://stackoverflow.com/users?tab=Reputation&search={name}", title="Stack Exchange API — users",
                             request=f"name “{name}”")
        for u in items[:5]:
            add_account(ctx.out, src, platform="stackoverflow", username=str(u.get("user_id")), profile=_profile(u),
                        found_by="name search")
