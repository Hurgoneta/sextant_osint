"""Infrastructure intelligence for IP addresses: Shodan InternetDB (free), RIPEstat (free), Shodan host API and
ipinfo (keys). Passive data only — nothing is scanned."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import is_public_ip, normalize_asn


def public_ip(_self, e, info) -> bool:
    return is_public_ip(e.value)


@register
class ShodanInternetDB(Connector):
    manifest = Manifest(
        name="shodan_internetdb", title="Shodan InternetDB", category="infrastructure",
        description="Free, keyless Shodan summary for an IP: open ports, hostnames, software (CPE) and tags, as last seen "
                    "by Shodan's own scanning (nothing is scanned by this tool).",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://internetdb.shodan.io",
        documentation="https://internetdb.shodan.io/docs", reliability="high", license="Non-commercial use",
        legal="InternetDB is offered for non-commercial use.",
        capabilities=("open ports (passive)", "hostnames", "software CPEs", "tags"),
        disclosure=Disclosure(("internetdb.shodan.io",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=("domain",), title="Shodan InternetDB: ports, hostnames, software", priority=0.5,
               accepts=public_ip)
    async def lookup(self, entity, ctx):
        resp = await ctx.get(f"https://internetdb.shodan.io/{quote(entity.value)}")
        if resp.status == 404:
            raise NotFound("No information in InternetDB")
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source(f"https://internetdb.shodan.io/{entity.value}", title="Shodan InternetDB",
                             request=f"IP “{entity.value}”")
        ctx.out.props(entity.key, {"ports": d.get("ports"), "hostnames": d.get("hostnames"), "cpes": d.get("cpes"),
                                   "tags": d.get("tags"), "known_vulnerabilities": d.get("vulns")}, source=src)
        for h in (d.get("hostnames") or [])[:10]:
            hk = ctx.out.entity("domain", h, source=src, props={"subdomain": True})
            ctx.out.link(hk, entity.key, "RESOLVES_TO", source=src, confidence=0.7, evidence="Hostname seen by Shodan")


@register
class RipeStat(Connector):
    manifest = Manifest(
        name="ripestat", title="RIPEstat", category="infrastructure",
        description="Routing and registration data for an IP: announcing AS, prefix, network holder.", access="free",
        rate_limit=RateLimit(2.0, 4), website="https://stat.ripe.net", documentation="https://stat.ripe.net/docs/data-api/",
        reliability="official", public_record=True,
        capabilities=("announcing AS and prefix", "AS holder", "country of registration"),
        disclosure=Disclosure(("stat.ripe.net",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=("asn", "organization"), title="RIPEstat: AS, prefix and holder", priority=0.5,
               accepts=public_ip, estimated_requests=2)
    async def network(self, entity, ctx):
        resp = await ctx.get("https://stat.ripe.net/data/network-info/data.json", params={"resource": entity.value})
        d = (ctx.json(ctx.expect(resp)) or {}).get("data") or {}
        src = ctx.out.source(f"https://stat.ripe.net/{entity.value}", title="RIPEstat network-info",
                             request=f"IP “{entity.value}”")
        ctx.out.prop(entity.key, "prefix", d.get("prefix"), source=src)
        for asn in (d.get("asns") or [])[:2]:
            a = normalize_asn(str(asn))
            if not a:
                continue
            r2 = await ctx.get("https://stat.ripe.net/data/as-overview/data.json", params={"resource": a})
            ov = (ctx.json(r2) or {}).get("data") or {} if r2.status == 200 else {}
            ak = ctx.out.entity("asn", a, source=src, props={"name": ov.get("holder")})
            ctx.out.link(entity.key, ak, "REGISTERED_TO", source=src, evidence="Announced by this autonomous system")
            ctx.out.prop(entity.key, "asn", a, source=src)
            if ov.get("holder"):
                ctx.out.prop(entity.key, "org", ov["holder"], source=src)


@register
class Shodan(Connector):
    manifest = Manifest(
        name="shodan", title="Shodan (host API)", category="infrastructure",
        description="Full Shodan host record: services, banners, organisation, location history.", access="paid",
        credentials=(Credential("SHODAN_API_KEY", "Shodan API key", obtain_url="https://account.shodan.io/"),),
        cost="Membership / API plan", rate_limit=RateLimit(1.0, 1), website="https://www.shodan.io",
        documentation="https://developer.shodan.io/api", reliability="high",
        capabilities=("services and banners", "organisation / ISP", "geolocation", "hostnames and domains"),
        disclosure=Disclosure(("api.shodan.io",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=("domain", "organization"), title="Shodan: host details", priority=0.5,
               accepts=public_ip)
    async def host(self, entity, ctx):
        (key,) = ctx.require("SHODAN_API_KEY")
        resp = await ctx.get(f"https://api.shodan.io/shodan/host/{quote(entity.value)}", params={"key": key, "minify": "true"})
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source(f"https://www.shodan.io/host/{entity.value}", title="Shodan host", request=f"IP “{entity.value}”")
        ctx.out.props(entity.key, {"org": d.get("org"), "isp": d.get("isp"), "country": d.get("country_name"),
                                   "ports": d.get("ports"), "hostnames": d.get("hostnames"), "os": d.get("os")}, source=src)
        for h in (d.get("domains") or [])[:10]:
            hk = ctx.out.entity("domain", h, source=src)
            ctx.out.link(hk, entity.key, "RESOLVES_TO", source=src, confidence=0.6, evidence="Domain associated by Shodan")


@register
class IpInfo(Connector):
    manifest = Manifest(
        name="ipinfo", title="ipinfo.io", category="infrastructure",
        description="Geolocation, ASN and hosting/privacy flags of an IP.", access="free_key",
        credentials=(Credential("IPINFO_TOKEN", "ipinfo token", "Free Lite token covers country and ASN.",
                                obtain_url="https://ipinfo.io/signup"),),
        rate_limit=RateLimit(2.0, 4), website="https://ipinfo.io", documentation="https://ipinfo.io/developers",
        reliability="high", capabilities=("country / city", "ASN and network owner", "hostname"),
        disclosure=Disclosure(("api.ipinfo.io",), "the IP address", "public"),
    )

    @transform(inputs=("ip",), outputs=("asn",), title="ipinfo: location and ASN", priority=0.4, accepts=public_ip)
    async def lookup(self, entity, ctx):
        (token,) = ctx.require("IPINFO_TOKEN")
        resp = await ctx.get(f"https://api.ipinfo.io/lite/{quote(entity.value)}", params={"token": token})
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source(f"https://ipinfo.io/{entity.value}", title="ipinfo Lite", request=f"IP “{entity.value}”")
        ctx.out.props(entity.key, {"country": d.get("country"), "org": d.get("as_name"), "asn": d.get("asn")}, source=src)
        a = normalize_asn(str(d.get("asn") or ""))
        if a:
            ak = ctx.out.entity("asn", a, source=src, props={"name": d.get("as_name")})
            ctx.out.link(entity.key, ak, "REGISTERED_TO", source=src, evidence="ASN per ipinfo")
