"""Declarative public-API profile checks — platforms defined as data, not code.

A curated platform with ``check = "probe"`` declares a documented public endpoint in its registry entry::

    [platform.probe]
    url = "https://api.example.org/users/{username}"   # {username} is URL-quoted
    method = "GET"                                     # or POST with `body` (JSON text, {username} replaced)
    found_status = [200]
    missing_status = [404]
    require = "id"                 # dotted path that must be non-empty for "found"
    list_path = "data"             # endpoints that answer with a list: an empty list means "not found"
    match = "name"                 # (within the list item) value that must equal the username, case-insensitive
    username_case = "upper"        # some registries use upper-case ids (CPAN)
    format = "json"                # or "text" (status code only)
    accept = "application/json"    # Accept header (JSON:API endpoints want application/vnd.api+json)
    fields = { display_name = "name", bio = "about", avatar_url = "avatar" }
    docs = "https://api.example.org/docs"

This connector runs every declared probe for a username. Adding platform #1,001 with a public API is a data
change: no code, same guarded HTTP client, same rate limits, same honest statuses (a 403/429 is *blocked*,
never *not found*). Only documented public endpoints with an unambiguous "not found" answer qualify.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

from ...registry.platforms import curated
from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.http import FetchError
from .._common import add_account

PROFILE_KEYS = ("display_name", "bio", "location", "website", "company", "created_at", "avatar_url", "followers",
                "user_id")
CONCURRENCY = 6


def dig(doc: Any, path: str) -> Any:
    """'data.0.name' → doc['data'][0]['name'] (None when missing)."""
    cur = doc
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def probe_platforms() -> list:
    return [p for p in curated() if p.check == "probe" and p.status != "defunct" and p.probe.get("url")]


def evaluate(spec: dict, status: int, body: bytes, username: str) -> tuple[str, dict | None, str]:
    """(found|not_found|blocked|unknown, profile, detail) for one probe response."""
    if status in spec.get("missing_status", [404]):
        return "not_found", None, ""
    if status in (401, 403, 429, 503):
        return "blocked", None, f"The API refused the request (HTTP {status}) — not retried around"
    if status not in spec.get("found_status", [200]):
        return "unknown", None, f"Unexpected answer (HTTP {status}) — the probe may be outdated"
    if spec.get("format", "json") == "text":
        return "found", {}, "The API answered for this username"
    try:
        doc = json.loads(body or b"null")
    except ValueError:
        return "unknown", None, "The API returned something other than JSON — the probe may be outdated"
    item = doc
    if spec.get("list_path") is not None:
        items = dig(doc, spec["list_path"]) if spec["list_path"] else doc
        if not isinstance(items, list):
            return "unknown", None, "Unexpected response shape — the probe may be outdated"
        if not items:
            return ("found", {}, "The API lists this account (no public items)") if spec.get("accept_empty_list") \
                else ("not_found", None, "")
        item = items[0]
        if spec.get("match"):
            hits = [it for it in items if str(dig(it, spec["match"]) or "").casefold() == username.casefold()]
            if not hits:
                return "not_found", None, "Only similar names exist"
            item = hits[0]
    if spec.get("require") and dig(item, spec["require"]) in (None, "", [], {}):
        return "not_found", None, ""
    profile = {}
    for key, path in (spec.get("fields") or {}).items():
        v = dig(item, path)
        if v in (None, "", [], {}):
            continue
        if key in PROFILE_KEYS:
            profile[key] = v if not isinstance(v, (dict, list)) else json.dumps(v)[:300]
        elif key == "emails":
            profile["emails"] = v if isinstance(v, list) else [v]
        elif key == "links":
            profile["links"] = [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).startswith("http")]
    return "found", profile, "The platform's public API returned this account"


@register
class ProfileProbes(Connector):
    manifest = Manifest(
        name="profile_probes", title="Public profile APIs (declarative probes)", category="usernames",
        description="Checks a username on every platform whose registry entry declares a documented public API "
                    "endpoint (Minecraft, Roblox, Scratch, MetaCPAN, Hex, Launchpad, Qiita, Modrinth, Liberapay…).",
        access="free", rate_limit=RateLimit(6, 6), website="", tags=("meta",), reliability="high",
        official_api=True, access_method="api", timeout=300, max_concurrency=1,
        coverage="Every curated platform with check = \"probe\"",
        capabilities=("account existence on platforms with public user APIs", "display name, bio, avatar, links when "
                      "the API publishes them"),
        disclosure=Disclosure(("the API host of each probed platform (listed in Platforms)",),
                              "the username — one request per platform"),
    )

    @transform(inputs=("username",), outputs=("account", "email", "domain", "name"),
               title="Username on platforms with public user APIs (probes)", priority=0.65, estimated_requests=25)
    async def sweep(self, entity, ctx):
        username = entity.display
        wanted = {p.lower() for p in ctx.info.option("platforms") or []}
        platforms = [p for p in probe_platforms() if not wanted or p.id in wanted or p.category in wanted]
        sem = asyncio.Semaphore(CONCURRENCY)
        counts: dict[str, int] = {}

        async def one(p) -> None:
            spec = p.probe
            u = username.upper() if spec.get("username_case") == "upper" else \
                username.lower() if spec.get("username_case") == "lower" else username
            if not p.valid_username(u) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", u):
                ctx.out.check(p.id, "skipped", detail="Username format not valid on this platform", platform_title=p.name,
                              category=p.category)
                return
            url = spec["url"].replace("{username}", quote(u, safe=""))
            body = spec.get("body")
            async with sem:
                try:
                    if (spec.get("method") or "GET").upper() == "POST":
                        resp = await ctx.post(url, content=str(body or "").replace("{username}", u).encode(),
                                              headers={"Content-Type": "application/json"}, retries=0)
                    else:
                        resp = await ctx.get(url, retries=0, cache_ttl=86_400,
                                             headers={"Accept": spec.get("accept") or "application/json"})
                except FetchError as exc:
                    if exc.kind == "budget":
                        raise
                    counts["error"] = counts.get("error", 0) + 1
                    ctx.out.check(p.id, "blocked" if exc.kind == "rate_limited" else "error", platform_title=p.name,
                                  category=p.category, detail=f"Request failed ({exc.kind})", url=p.profile(u))
                    return
            status, profile, detail = evaluate(spec, resp.status, resp.content, u)
            counts[status] = counts.get(status, 0) + 1
            page = p.profile(u)
            if status != "found":
                ctx.out.check(p.id, status, detail=detail, url=page, platform_title=p.name, category=p.category)
                return
            src = ctx.out.source(page or url, title=f"{p.name} public API", request=f"username “{u}”")
            acc = add_account(ctx.out, src, platform=p.id, username=u, link_from=entity.key,
                              profile={**profile, "profile_url": page})
            ctx.out.check(p.id, "found", detail=detail, url=page, account=acc, platform_title=p.name, category=p.category)

        await asyncio.gather(*(one(p) for p in platforms))
        ctx.out.set_status("found" if counts.get("found") else "empty",
                           f"{len(platforms)} public APIs checked: {counts.get('found', 0)} found, "
                           f"{counts.get('not_found', 0)} not found, {counts.get('blocked', 0)} blocked, "
                           f"{counts.get('unknown', 0) + counts.get('error', 0)} inconclusive")


def probe_hosts() -> list[str]:
    return sorted({urlsplit(p.probe["url"].replace("{username}", "x")).hostname or "" for p in probe_platforms()})
