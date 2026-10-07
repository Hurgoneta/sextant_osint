"""Factory for platforms with a documented public JSON endpoint for "user by handle".

Each platform is one ``PlatformSpec`` (endpoint, accepted handle format, parser) and becomes a full
connector with its own manifest, so it appears in the catalog and coverage matrix like any other.
Parsers are pure functions ``(response, username) -> (status, profile, detail)``, unit-tested with
recorded fixtures. Only endpoints with an unambiguous "not found" answer qualify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from ..sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ..sdk.errors import Blocked, NotFound, Skipped, SourceUnavailable
from ..sdk.http import HttpResponse
from ._common import add_account, avatar_fingerprint

Parsed = tuple[str, dict[str, Any] | None, str]          # status (found|not_found|unknown), profile, detail


def ts(value) -> str | None:
    """Unix seconds/milliseconds → ISO date."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 10_000_000_000:
        v /= 1000
    return datetime.fromtimestamp(v, timezone.utc).replace(microsecond=0).isoformat()


def jbody(resp: HttpResponse) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


@dataclass(frozen=True)
class PlatformSpec:
    name: str                       # connector name
    platform: str                   # platform registry id
    title: str
    category: str
    host: str
    endpoint: str                   # {u} = url-quoted handle
    parse: Callable[[HttpResponse, str], Parsed]
    website: str
    documentation: str = ""
    username_re: str = r"^[A-Za-z0-9_.-]{1,64}$"
    lowercase: bool = False
    rate: RateLimit = RateLimit(1.0, 2)
    capabilities: tuple[str, ...] = ("account existence", "display name", "bio", "links", "profile photo")
    description: str = ""
    legal: str = ""
    headers: dict = field(default_factory=dict)


def make_platform_connector(spec: PlatformSpec) -> Connector:
    class _PlatformConnector(Connector):
        manifest = Manifest(
            name=spec.name, title=spec.title, category=spec.category,
            description=spec.description or f"Checks whether a username exists on {spec.title} through its public API "
                                             f"and extracts the public profile.",
            access="free", cost="Free", rate_limit=spec.rate, website=spec.website, documentation=spec.documentation,
            reliability="high", official_api=True, coverage=f"Public {spec.title} profiles",
            capabilities=spec.capabilities, legal=spec.legal,
            disclosure=Disclosure((spec.host,), "the username being checked"),
            input_entities=("username",), output_entities=("account", "email", "domain", "location", "image"),
        )

        @transform(inputs=("username",), outputs=("account", "email", "domain", "location", "organization", "image", "name"),
                   title=f"{spec.title}: find account by username", priority=0.7)
        async def username_lookup(self, entity, ctx):
            u = entity.display.lower() if spec.lowercase else entity.display
            if not re.fullmatch(spec.username_re, u):
                ctx.out.check(spec.platform, "skipped", detail=f"Not a valid {spec.title} username format",
                              platform_title=spec.title)
                raise Skipped(f"“{u}” is not a valid {spec.title} username")
            resp = await ctx.get(spec.endpoint.format(u=quote(u, safe="")),
                                 headers={"Accept": "application/json", **spec.headers})
            src = ctx.out.source(resp.url, title=f"{spec.title} API", request=f"username “{u}”")
            if resp.status in (401, 403):
                ctx.out.check(spec.platform, "blocked", detail=f"HTTP {resp.status}", platform_title=spec.title)
                raise Blocked(f"{spec.title} refused automated access (HTTP {resp.status})")
            status, profile, detail = spec.parse(resp, u)
            if status == "not_found":
                ctx.out.check(spec.platform, "not_found", detail=detail, platform_title=spec.title)
                raise NotFound(f"No {spec.title} account “{u}”" + (f" — {detail}" if detail else ""))
            if status != "found" or not profile:
                ctx.out.check(spec.platform, "unknown", detail=detail, platform_title=spec.title)
                raise SourceUnavailable(detail or f"Unexpected {spec.title} response (HTTP {resp.status})")
            profile["avatar_fp"] = await avatar_fingerprint(ctx, profile.get("avatar_url"))
            handle = str(profile.get("username") or u)
            acc = add_account(ctx.out, src, platform=spec.platform, username=handle, profile=profile,
                              link_from=entity.key, found_by="username", platform_title=spec.title)
            ctx.out.check(spec.platform, "found", detail=f"Public {spec.title} profile", url=profile.get("profile_url"),
                          account=acc, platform_title=spec.title)

        @transform(inputs=("account",), outputs=("account", "email", "domain", "location", "organization", "image", "name"),
                   title=f"{spec.title}: fetch full profile of a linked account", priority=0.6,
                   accepts=lambda self, e, info: e.get("platform") == spec.platform
                   and e.get("found_by") in ("link", "search_result", "adapter"))
        async def account_profile(self, entity, ctx):
            """A profile URL found elsewhere (bio link, search result, adapter): fetch it through the API."""
            u = entity.display.lower() if spec.lowercase else entity.display
            if not re.fullmatch(spec.username_re, u):
                raise Skipped(f"“{u}” is not a valid {spec.title} username")
            resp = await ctx.get(spec.endpoint.format(u=quote(u, safe="")),
                                 headers={"Accept": "application/json", **spec.headers})
            src = ctx.out.source(resp.url, title=f"{spec.title} API", request=f"username “{u}”")
            status, profile, detail = spec.parse(resp, u)
            if status == "not_found":
                raise NotFound(f"The linked {spec.title} account “{u}” does not exist (any more)")
            if status != "found" or not profile:
                raise SourceUnavailable(detail or f"Unexpected {spec.title} response (HTTP {resp.status})")
            profile["avatar_fp"] = await avatar_fingerprint(ctx, profile.get("avatar_url"))
            add_account(ctx.out, src, platform=spec.platform, username=str(profile.get("username") or u), profile=profile,
                        found_by="profile", platform_title=spec.title)

    _PlatformConnector.__name__ = f"{spec.name.title().replace('_', '')}Connector"
    inst = _PlatformConnector()
    register(inst)
    return inst
