"""npm registry — packages maintained by a username, including the maintainer email published in package metadata."""

from __future__ import annotations

import re
from urllib.parse import quote

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, Skipped
from ...util.normalize import normalize_email
from .._common import add_link_target


@register
class Npm(Connector):
    manifest = Manifest(
        name="npm", title="npm registry", category="developer_platforms",
        description="Packages maintained by an npm username, with the maintainer email from public package metadata.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://www.npmjs.com",
        documentation="https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md", reliability="high",
        capabilities=("account existence (has published packages)", "maintainer email", "package homepages/repositories"),
        disclosure=Disclosure(("registry.npmjs.org",), "the username being checked"),
    )

    @transform(inputs=("username",), outputs=("account", "email", "website", "domain"),
               title="npm: packages and maintainer email for a username", priority=0.6)
    async def maintainer_lookup(self, entity, ctx):
        u = entity.display.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,213}", u):
            raise Skipped("Not a valid npm username")
        resp = await ctx.get("https://registry.npmjs.org/-/v1/search", params={"text": f"maintainer:{u}", "size": 20})
        data = ctx.json(ctx.expect(resp)) or {}
        objs = data.get("objects") or []
        if not objs:
            ctx.out.check("npm", "not_found", detail="No packages maintained by this username (an account without packages "
                          "cannot be detected)", platform_title="npm")
            raise NotFound(f"No npm packages maintained by “{u}”")
        src = ctx.out.source(f"https://www.npmjs.com/~{quote(u)}", title="npm registry search", request=f"maintainer:{u}")
        emails = set()
        pkgs = []
        for o in objs:
            pkg = o.get("package") or {}
            pkgs.append(pkg.get("name"))
            for m in [*(pkg.get("maintainers") or []), pkg.get("publisher") or {}]:
                if isinstance(m, dict) and str(m.get("username", "")).lower() == u and m.get("email"):
                    e = normalize_email(m["email"])
                    if e:
                        emails.add(e)
        acc = ctx.out.entity("account", f"npm:{u}", label=f"npm: {u}", source=src,
                             props={"platform": "npm", "platform_title": "npm", "username": u, "found_by": "username",
                                    "profile_url": f"https://www.npmjs.com/~{u}", "emails": sorted(emails),
                                    "extra": {"packages": pkgs[:20], "package_count": data.get("total")}})
        ctx.out.link(acc, entity.key, "USES", source=src, evidence="npm maintainer username")
        for e in sorted(emails):
            ek = ctx.out.entity("email", e, source=src, evidence="Maintainer email in public package metadata")
            ctx.out.link(acc, ek, "USES", source=src, evidence="Maintainer email published with the packages")
        for o in objs[:10]:
            links = (o.get("package") or {}).get("links") or {}
            for k in ("homepage", "repository"):
                if links.get(k):
                    add_link_target(ctx.out, acc, links[k], src)
        ctx.out.check("npm", "found", url=f"https://www.npmjs.com/~{u}", account=acc, platform_title="npm")
