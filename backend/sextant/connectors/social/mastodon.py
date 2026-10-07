"""Mastodon and the Fediverse — public account lookup on large servers, WebFinger for full handles.

A bare username is checked on a short list of large Mastodon servers (the Fediverse has thousands of
servers; the UI says which were checked). A full handle (user@server) is resolved on its own server
with WebFinger. Verified profile links (rel="me", shown as ``verified_at``) are strong evidence.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped
from ...sdk.http import FetchError
from ...util.normalize import normalize_domain
from .._common import add_account, avatar_fingerprint, html_bio

SERVERS = ("mastodon.social", "mastodon.online", "mstdn.social", "fosstodon.org", "hachyderm.io", "infosec.exchange",
           "mas.to", "techhub.social")


def parse_account(u: dict, server: str) -> dict:
    bio, links = html_bio(u.get("note"))
    verified = []
    for fld in u.get("fields") or []:
        _, flinks = html_bio(fld.get("value"))
        links += flinks
        if fld.get("verified_at"):
            verified += flinks
    acct = u.get("acct") or u.get("username")
    handle = acct if "@" in str(acct) else f"{acct}@{server}"
    return {"username": handle, "display_name": u.get("display_name"), "bio": bio, "links": links,
            "verified_links": verified, "created_at": u.get("created_at"), "avatar_url": u.get("avatar"),
            "profile_url": u.get("url"), "followers": u.get("followers_count"), "user_id": u.get("id"),
            "extra": {"server": server, "bot": u.get("bot"), "statuses": u.get("statuses_count")}}


@register
class Mastodon(Connector):
    manifest = Manifest(
        name="mastodon", title="Mastodon / Fediverse", category="social",
        description="Looks up a username on large Mastodon servers, and resolves full fediverse handles (user@server) "
                    "with WebFinger on their own server.",
        access="free", rate_limit=RateLimit(2.0, 4, "300 requests / 5 min per server"),
        website="https://joinmastodon.org", documentation="https://docs.joinmastodon.org/methods/accounts/#lookup",
        reliability="high", coverage=f"{len(SERVERS)} large servers for bare usernames; any server for full handles",
        capabilities=("accounts by username across large servers", "any account by full handle (WebFinger)",
                      "display name, bio, rel=me verified links, creation date, avatar"),
        disclosure=Disclosure(SERVERS + ("the server named in a full handle",), "the username being checked"),
        max_concurrency=2,
    )

    async def _lookup(self, ctx, server: str, user: str):
        try:
            resp = await ctx.get(f"https://{server}/api/v1/accounts/lookup", params={"acct": user}, retries=1)
        except FetchError:
            return None, None
        if resp.status != 200:
            return resp, None
        data = ctx.json(resp)
        return resp, data if isinstance(data, dict) else None

    @transform(inputs=("username",), outputs=("account", "domain", "image", "name"),
               title="Mastodon: find account on large servers / by full handle", priority=0.6, estimated_requests=8)
    async def username_lookup(self, entity, ctx):
        handle = str(entity.get("handle") or "")
        if "@" in handle:
            user, _, server = handle.partition("@")
            servers = (normalize_domain(server),) if normalize_domain(server) else ()
        else:
            user, servers = entity.display, SERVERS
        if not re.fullmatch(r"[A-Za-z0-9_]{1,30}", user) or not servers:
            raise Skipped("Not a valid Mastodon username")
        found = 0
        for server in servers:
            resp, data = await self._lookup(ctx, server, user)
            plat_title = f"Mastodon ({server})"
            if data is None:
                ctx.out.check(f"mastodon:{server}", "not_found" if resp is not None and resp.status == 404 else "unknown",
                              platform_title=plat_title, category="social")
                continue
            src = ctx.out.source(resp.url, title=f"{server} — account lookup", request=f"username “{user}”")
            p = parse_account(data, server)
            p["avatar_fp"] = await avatar_fingerprint(ctx, p.get("avatar_url"))
            acc = add_account(ctx.out, src, platform="mastodon", username=p["username"], profile=p, link_from=entity.key,
                              platform_title=plat_title)
            ctx.out.check(f"mastodon:{server}", "found", url=p.get("profile_url"), account=acc, platform_title=plat_title,
                          category="social")
            found += 1
        if not found:
            raise NotFound(f"Not found on {len(servers)} server(s): {', '.join(servers)}")

    @transform(inputs=("account",), outputs=("account", "domain", "image", "name"),
               title="Mastodon: fetch a linked fediverse profile", priority=0.6,
               accepts=lambda self, e, info: e.get("platform") == "mastodon" and e.get("found_by") in ("link", "search_result"))
    async def account_profile(self, entity, ctx):
        url = str(entity.get("profile_url") or "")
        m = re.match(r"https://([^/]+)/@([A-Za-z0-9_]{1,30})", url)
        if not m:
            raise Skipped("Profile URL is not a Mastodon-style URL")
        server, user = m.group(1), m.group(2)
        resp, data = await self._lookup(ctx, server, user)
        if data is None:
            raise NotFound(f"@{user}@{server} not found")
        src = ctx.out.source(resp.url, title=f"{server} — account lookup", request=f"handle “{user}@{server}”")
        add_account(ctx.out, src, platform="mastodon", username=f"{user}@{server}", profile=parse_account(data, server),
                    found_by="profile", platform_title=f"Mastodon ({server})")

    @transform(inputs=("email",), outputs=("account",), title="Fediverse: WebFinger for an email-like handle",
               priority=0.3, accepts=lambda self, e, info: e.value.split("@", 1)[1] in SERVERS,
               sends="the address as a fediverse handle to its own server")
    async def email_as_handle(self, entity, ctx):
        """Addresses at a Mastodon server's domain (user@mastodon.social) are often fediverse handles."""
        user, _, server = entity.value.partition("@")
        resp = await ctx.get(f"https://{server}/.well-known/webfinger",
                             params={"resource": f"acct:{quote(user)}@{server}"})
        if resp.status != 200:
            raise NotFound("No fediverse account with this handle")
        resp2, data = await self._lookup(ctx, server, user)
        if data is None:
            raise NotFound("No fediverse account with this handle")
        src = ctx.out.source(resp2.url, title=f"{server} — account lookup", request=f"handle “{user}@{server}”")
        add_account(ctx.out, src, platform="mastodon", username=f"{user}@{server}", profile=parse_account(data, server),
                    link_from=entity.key, found_by="WebFinger", platform_title=f"Mastodon ({server})")
