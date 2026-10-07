"""Reputation and threat intelligence (all keyed): VirusTotal, urlscan.io, AbuseIPDB, GreyNoise, SecurityTrails."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import is_public_ip
from ..dns.dns import is_apex


@register
class VirusTotal(Connector):
    manifest = Manifest(
        name="virustotal", title="VirusTotal", category="reputation",
        description="Reputation, categories, registrar and passive-DNS context of a domain or IP.", access="free_key",
        credentials=(Credential("VIRUSTOTAL_API_KEY", "VirusTotal API key", "Public API: 4 requests/minute, 500/day; "
                                "commercial use requires a premium licence.", obtain_url="https://www.virustotal.com/gui/my-apikey"),),
        cost="Free public API (non-commercial) / premium", rate_limit=RateLimit(0.066, 1, "4 requests/minute"),
        legal="The public API may not be used in commercial products or services.",
        website="https://www.virustotal.com", documentation="https://docs.virustotal.com/reference/overview",
        reliability="high", capabilities=("malicious/suspicious verdicts", "categories", "registrar", "last DNS records"),
        disclosure=Disclosure(("www.virustotal.com",), "the domain or IP", "public"),
    )

    @transform(inputs=("domain", "ip"), outputs=(), title="VirusTotal: reputation", priority=0.3,
               accepts=lambda self, e, info: e.type == "ip" and is_public_ip(e.value) or e.type == "domain" and is_apex(e.value))
    async def reputation(self, entity, ctx):
        (key,) = ctx.require("VIRUSTOTAL_API_KEY")
        kind = "ip_addresses" if entity.type == "ip" else "domains"
        resp = await ctx.get(f"https://www.virustotal.com/api/v3/{kind}/{quote(entity.value)}", headers={"x-apikey": key})
        a = ((ctx.json(ctx.expect(resp)) or {}).get("data") or {}).get("attributes") or {}
        src = ctx.out.source(f"https://www.virustotal.com/gui/{'ip-address' if entity.type == 'ip' else 'domain'}/{entity.value}",
                             title="VirusTotal", request=f"{entity.type} “{entity.value}”")
        ctx.out.props(entity.key, {"vt_last_analysis": a.get("last_analysis_stats"), "vt_reputation": a.get("reputation"),
                                   "vt_categories": a.get("categories"), "registrar": a.get("registrar")}, source=src)


@register
class UrlScan(Connector):
    manifest = Manifest(
        name="urlscan", title="urlscan.io", category="reputation",
        description="Public scans that involved a domain: pages, IPs, ASNs, first/last seen.", access="free_key",
        credentials=(Credential("URLSCAN_API_KEY", "urlscan.io API key", obtain_url="https://urlscan.io/user/profile/"),),
        rate_limit=RateLimit(0.5, 1), website="https://urlscan.io", documentation="https://urlscan.io/docs/api/",
        reliability="medium", capabilities=("public scans of the domain", "hosting IPs and ASNs", "page titles"),
        disclosure=Disclosure(("urlscan.io",), "the domain as a search query", "public"),
    )

    @transform(inputs=("domain",), outputs=("ip", "website"), title="urlscan.io: public scans of this domain", priority=0.3)
    async def search(self, entity, ctx):
        (key,) = ctx.require("URLSCAN_API_KEY")
        resp = await ctx.get("https://urlscan.io/api/v1/search/", params={"q": f"domain:{entity.value}", "size": 20},
                             headers={"API-Key": key})
        results = (ctx.json(ctx.expect(resp)) or {}).get("results") or []
        if not results:
            raise NotFound("No public scans")
        src = ctx.out.source(f"https://urlscan.io/search/#domain:{entity.value}", title="urlscan.io search",
                             request=f"domain:{entity.value}")
        for r in results[:20]:
            page = r.get("page") or {}
            if page.get("ip") and is_public_ip(page["ip"]):
                ik = ctx.out.entity("ip", page["ip"], source=src)
                ctx.out.link(entity.key, ik, "RESOLVES_TO", source=src, confidence=0.6,
                             evidence=f"Served the site in a scan on {(r.get('task') or {}).get('time', '')[:10]}")


@register
class AbuseIpdb(Connector):
    manifest = Manifest(
        name="abuseipdb", title="AbuseIPDB", category="reputation",
        description="Abuse reports and confidence score for an IP.", access="free_key",
        credentials=(Credential("ABUSEIPDB_API_KEY", "AbuseIPDB API key", obtain_url="https://www.abuseipdb.com/account/api"),),
        rate_limit=RateLimit(1.0, 2, "1,000 checks/day on the free plan"), website="https://www.abuseipdb.com",
        documentation="https://docs.abuseipdb.com/", reliability="medium",
        capabilities=("abuse confidence score", "number of reports", "usage type", "ISP"),
        disclosure=Disclosure(("api.abuseipdb.com",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=(), title="AbuseIPDB: abuse reports", priority=0.3,
               accepts=lambda self, e, info: is_public_ip(e.value))
    async def check(self, entity, ctx):
        (key,) = ctx.require("ABUSEIPDB_API_KEY")
        resp = await ctx.get("https://api.abuseipdb.com/api/v2/check", params={"ipAddress": entity.value, "maxAgeInDays": 365},
                             headers={"Key": key, "Accept": "application/json"})
        d = (ctx.json(ctx.expect(resp)) or {}).get("data") or {}
        src = ctx.out.source(f"https://www.abuseipdb.com/check/{entity.value}", title="AbuseIPDB", request=f"IP “{entity.value}”")
        ctx.out.props(entity.key, {"abuse_confidence": d.get("abuseConfidenceScore"), "abuse_reports": d.get("totalReports"),
                                   "usage_type": d.get("usageType"), "isp": d.get("isp")}, source=src)


@register
class GreyNoise(Connector):
    manifest = Manifest(
        name="greynoise", title="GreyNoise Community", category="reputation",
        description="Whether an IP is internet background noise (mass scanner) or a known benign service.", access="free_key",
        credentials=(Credential("GREYNOISE_API_KEY", "GreyNoise API key", obtain_url="https://viz.greynoise.io/account/api-key"),),
        rate_limit=RateLimit(0.5, 1), website="https://www.greynoise.io", documentation="https://docs.greynoise.io/",
        reliability="medium", capabilities=("noise / RIOT classification", "actor name", "last seen"),
        disclosure=Disclosure(("api.greynoise.io",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=(), title="GreyNoise: scanner / benign service?", priority=0.2,
               accepts=lambda self, e, info: is_public_ip(e.value))
    async def community(self, entity, ctx):
        (key,) = ctx.require("GREYNOISE_API_KEY")
        resp = await ctx.get(f"https://api.greynoise.io/v3/community/{quote(entity.value)}", headers={"key": key})
        if resp.status == 404:
            raise NotFound("Not observed by GreyNoise")
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source(f"https://viz.greynoise.io/ip/{entity.value}", title="GreyNoise community",
                             request=f"IP “{entity.value}”")
        ctx.out.props(entity.key, {"greynoise": {k: d.get(k) for k in ("noise", "riot", "classification", "name", "last_seen")}},
                      source=src)


@register
class SecurityTrails(Connector):
    manifest = Manifest(
        name="securitytrails", title="SecurityTrails", category="reputation",
        description="Subdomains and historical DNS of a domain.", access="paid",
        credentials=(Credential("SECURITYTRAILS_API_KEY", "SecurityTrails API key", obtain_url="https://securitytrails.com/app/account/credentials"),),
        cost="Free tier (limited) / paid plans", rate_limit=RateLimit(0.5, 1), website="https://securitytrails.com",
        documentation="https://docs.securitytrails.com/reference", reliability="high",
        capabilities=("subdomains", "current DNS", "Alexa rank", "historical records (paid)"),
        disclosure=Disclosure(("api.securitytrails.com",), "the domain", "public"),
    )

    @transform(inputs=("domain",), outputs=("domain",), title="SecurityTrails: subdomains", priority=0.3,
               accepts=lambda self, e, info: is_apex(e.value))
    async def subdomains(self, entity, ctx):
        (key,) = ctx.require("SECURITYTRAILS_API_KEY")
        resp = await ctx.get(f"https://api.securitytrails.com/v1/domain/{quote(entity.value)}/subdomains",
                             params={"children_only": "false"}, headers={"APIKEY": key})
        subs = (ctx.json(ctx.expect(resp)) or {}).get("subdomains") or []
        src = ctx.out.source(f"https://securitytrails.com/list/apex_domain/{entity.value}", title="SecurityTrails subdomains",
                             request=f"domain “{entity.value}”")
        for s in subs[:40]:
            sk = ctx.out.entity("domain", f"{s}.{entity.value}", source=src, props={"subdomain": True})
            ctx.out.link(entity.key, sk, "HAS_SUBDOMAIN", source=src, evidence="Subdomain per SecurityTrails")
