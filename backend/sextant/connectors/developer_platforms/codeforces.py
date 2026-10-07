"""Codeforces — competitive-programming profiles through the official public API.

This connector is the worked example in docs/CONNECTOR_SDK.md. It deliberately uses only the public SDK
(no shared helpers) so every step is visible; keep the two in sync.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ...sdk import Connector, Disclosure, Manifest, NotFound, RateLimit, SourceUnavailable, register, transform

API = "https://codeforces.com/api/user.info"
HANDLE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")


@register
class Codeforces(Connector):
    manifest = Manifest(
        name="codeforces", title="Codeforces", category="developer_platforms",
        description="Competitive-programming profiles: published name, country, city, organization, rating and "
                    "registration date.",
        access="free", cost="Free", rate_limit=RateLimit(0.5, 1, "one request per 2 seconds (API terms)"),
        website="https://codeforces.com", documentation="https://codeforces.com/apiHelp", reliability="high",
        license="Codeforces API terms", official_api=True,
        capabilities=("account existence", "first/last name (if the user published it)", "country and city",
                      "organization", "registration date", "rating"),
        disclosure=Disclosure(("codeforces.com",), "the username"),
    )

    @transform(inputs=("username",), outputs=("account", "name", "location", "organization"),
               title="Codeforces: account by username", priority=0.4,
               accepts=lambda self, e, info: bool(HANDLE.match(e.display)))
    async def username_lookup(self, entity, ctx):
        """Looks the handle up with user.info; a missing handle is an honest "not found"."""
        resp = await ctx.get(API, params={"handles": entity.display})
        data = ctx.json(resp) or {}
        if resp.status == 400 and "not found" in str(data.get("comment", "")):
            ctx.out.check("codeforces", "not_found", platform_title="Codeforces")    # shown in the platform sweep
            raise NotFound(f"No Codeforces account “{entity.display}”")
        ctx.expect(resp)                                   # 403 → blocked, 429 → rate limited, 5xx → unavailable
        users = data.get("result") or []
        if data.get("status") != "OK" or not users:
            raise SourceUnavailable(f"Codeforces answered: {str(data.get('comment') or 'no result')[:120]}")
        u = users[0]
        handle = u["handle"]

        # 1. provenance: every fact below cites this source
        src = ctx.out.source(f"https://codeforces.com/profile/{handle}", title="Codeforces API (user.info)",
                             request=f"handle “{entity.display}”")

        # 2. the account and its facts
        name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x).strip()
        place = ", ".join(x for x in (u.get("city"), u.get("country")) if x)
        created = u.get("registrationTimeSeconds")
        acc = ctx.out.entity("account", f"codeforces:{handle}", label=f"Codeforces: {handle}", source=src, props={
            "platform": "codeforces", "platform_title": "Codeforces", "category": "developer_platforms",
            "username": handle, "display_name": name or None, "location": place or None,
            "employer": u.get("organization") or None, "profile_url": f"https://codeforces.com/profile/{handle}",
            "avatar_url": u.get("titlePhoto"), "rating": u.get("rating"), "rank": u.get("rank"),
            "created_at": datetime.fromtimestamp(created, timezone.utc).date().isoformat() if created else None,
            "found_by": "username"}, evidence="Profile returned by the official API")
        ctx.out.link(acc, entity.key, "USES", source=src, evidence="Handle of the account")
        ctx.out.check("codeforces", "found", url=f"https://codeforces.com/profile/{handle}", account=acc,
                      platform_title="Codeforces")

        # 3. new selectors / objects the engine can pivot on — linked to the ACCOUNT, never to a person
        if len(name.split()) >= 2:
            ne = ctx.out.entity("name", name, source=src)
            ctx.out.link(acc, ne, "HAS_NAME", source=src, evidence="Name published on the profile")
        if place:
            le = ctx.out.entity("location", place, source=src, props={"text": place, "city": u.get("city"),
                                                                      "country": u.get("country")})
            ctx.out.link(acc, le, "LOCATED_IN", source=src, evidence="City/country on the profile")
        if u.get("organization"):
            org = u["organization"]
            oe = ctx.out.entity("organization", f"name:{org.casefold()}", label=org, source=src, props={"name": org})
            ctx.out.link(acc, oe, "WORKS_FOR", source=src, evidence="Organization stated on the profile")
