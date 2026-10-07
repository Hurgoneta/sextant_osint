"""Certificate Transparency — passive subdomain discovery and certificate history (crt.sh, Certspotter)."""

from __future__ import annotations

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import SourceUnavailable
from ...util.normalize import normalize_domain
from ..dns.dns import is_apex

MAX_SUBDOMAINS = 40


def _emit(ctx, entity, names: set[str], first: str | None, count: int, src: str) -> None:
    ctx.out.props(entity.key, {"certificate_count": count, "first_certificate": first,
                               "subdomain_count": len(names)}, source=src)
    for sub in sorted(names)[:MAX_SUBDOMAINS]:
        s = ctx.out.entity("domain", sub, source=src, props={"subdomain": True})
        ctx.out.link(entity.key, s, "HAS_SUBDOMAIN", source=src, evidence="Name seen in a public certificate")


@register
class CrtSh(Connector):
    manifest = Manifest(
        name="crtsh", title="crt.sh (Certificate Transparency)", category="certificates",
        description="Subdomains and certificate history from public CT logs (passive; nothing is scanned). crt.sh is often "
                    "overloaded — Certspotter is the fallback.",
        access="free", rate_limit=RateLimit(0.2, 1), website="https://crt.sh", documentation="https://crt.sh/",
        reliability="high", timeout=90,
        capabilities=("subdomains from certificates", "first certificate date", "certificate count"),
        disclosure=Disclosure(("crt.sh",), "the domain name", "public"),
    )

    @transform(inputs=("domain",), outputs=("domain",), title="CT logs (crt.sh): subdomains and certificates",
               priority=0.5, accepts=lambda self, e, info: is_apex(e.value))
    async def subdomains(self, entity, ctx):
        resp = await ctx.get("https://crt.sh/", params={"q": f"%.{entity.value}", "output": "json"}, retries=1)
        if resp.truncated:
            raise SourceUnavailable("Too many certificates to process (very large domain)")
        rows = ctx.json(ctx.expect(resp, what="crt.sh")) or []
        names, first = set(), None
        for row in rows if isinstance(rows, list) else []:
            nb = row.get("not_before")
            if nb and (first is None or nb < first):
                first = nb
            for n in str(row.get("name_value") or "").splitlines():
                n = normalize_domain(n.strip().lstrip("*."))
                if n and n != entity.value and n.endswith("." + entity.value):
                    names.add(n)
        src = ctx.out.source(f"https://crt.sh/?q=%25.{entity.value}", title="crt.sh certificate search",
                             request=f"domain “{entity.value}”")
        _emit(ctx, entity, names, first, len(rows), src)


@register
class Certspotter(Connector):
    manifest = Manifest(
        name="certspotter", title="Certspotter (SSLMate)", category="certificates",
        description="Certificate issuances for a domain and its subdomains from CT logs.", access="free",
        credentials=(Credential("CERTSPOTTER_API_KEY", "Certspotter API key", "Optional; raises the unauthenticated quota.",
                                required=False, obtain_url="https://sslmate.com/certspotter/api"),),
        rate_limit=RateLimit(0.2, 1, "limited hourly quota without a key"), website="https://sslmate.com/certspotter/",
        documentation="https://sslmate.com/help/reference/ct_search_api_v1", reliability="high",
        capabilities=("subdomains from certificates", "issuance history"),
        disclosure=Disclosure(("api.certspotter.com",), "the domain name", "public"),
    )

    @transform(inputs=("domain",), outputs=("domain",), title="CT logs (Certspotter): subdomains", priority=0.5,
               accepts=lambda self, e, info: is_apex(e.value))
    async def subdomains(self, entity, ctx):
        headers = {}
        key = ctx.credential("CERTSPOTTER_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = await ctx.get("https://api.certspotter.com/v1/issuances", headers=headers,
                             params={"domain": entity.value, "include_subdomains": "true", "expand": "dns_names"})
        rows = ctx.json(ctx.expect(resp, what="Certspotter")) or []
        names = set()
        for row in rows if isinstance(rows, list) else []:
            for n in row.get("dns_names") or []:
                n = normalize_domain(str(n).lstrip("*."))
                if n and n != entity.value and n.endswith("." + entity.value):
                    names.add(n)
        first = min((r.get("not_before") for r in rows if isinstance(r, dict) and r.get("not_before")), default=None)
        src = ctx.out.source(f"https://sslmate.com/certspotter/?q={entity.value}", title="Certspotter issuances",
                             request=f"domain “{entity.value}”")
        _emit(ctx, entity, names, first, len(rows), src)
