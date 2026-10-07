"""DNS — passive lookups only (no zone transfers, no brute forcing)."""

from __future__ import annotations

import re

import dns.asyncresolver
import dns.exception
import dns.name
import dns.resolver
import dns.reversename

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import is_public_ip, registered_domain

RECORD_TYPES = ("A", "AAAA", "MX", "NS", "TXT", "CAA")
SERVICE_TOKENS = (
    ("google-site-verification", "Google Search Console / Workspace"), ("MS=", "Microsoft 365"),
    ("facebook-domain-verification", "Facebook / Meta"), ("apple-domain-verification", "Apple"),
    ("atlassian-domain-verification", "Atlassian"), ("docusign", "DocuSign"), ("zoom-domain-verification", "Zoom"),
    ("stripe-verification", "Stripe"), ("adobe-idp-site-verification", "Adobe"), ("keybase-site-verification", "Keybase"),
    ("protonmail-verification", "Proton Mail"), ("_github-challenge", "GitHub"), ("github-verification", "GitHub"),
    ("hubspot", "HubSpot"), ("mailchimp", "Mailchimp"), ("brevo-code", "Brevo"), ("openai-domain-verification", "OpenAI"),
    ("atproto", "Bluesky (AT Protocol)"), ("have-i-been-pwned-verification", "Have I Been Pwned"),
)


async def resolve(name: str, rtype: str, lifetime: float = 6.0) -> list[str]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = lifetime
    try:
        answer = await resolver.resolve(name, rtype, raise_on_no_answer=False)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.resolver.NoAnswer, dns.exception.Timeout,
            dns.name.EmptyLabel, dns.name.LabelTooLong):
        return []
    out = []
    for rr in answer:
        text = rr.to_text()
        if rtype == "TXT":
            text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text)) or text
        out.append(text)
    return out


def is_apex(domain: str) -> bool:
    return registered_domain(domain) == domain


@register
class Dns(Connector):
    manifest = Manifest(
        name="dns", title="DNS records", category="dns",
        description="A/AAAA/MX/NS/TXT/CAA records, SPF/DMARC policy, services revealed by verification tokens, and "
                    "reverse DNS of IP addresses.",
        access="free", rate_limit=RateLimit(5, 10), website="https://www.dnspython.org",
        documentation="https://www.rfc-editor.org/rfc/rfc1035", reliability="high", access_method="api",
        capabilities=("IP addresses", "mail servers", "name servers", "SPF/DMARC", "services in TXT tokens",
                      "Bluesky handle verification", "reverse DNS"),
        disclosure=Disclosure(("your DNS resolver",), "the domain or IP being looked up", "public"),
    )

    @transform(inputs=("domain",), outputs=("ip", "domain"), title="DNS: records and IP addresses", priority=0.7,
               sends="domain “{value}” to your DNS resolver")
    async def records(self, entity, ctx):
        name = entity.value
        recs = {rt: await resolve(name, rt) for rt in RECORD_TYPES}
        if is_apex(name):
            recs["DMARC"] = await resolve(f"_dmarc.{name}", "TXT")
            recs["ATPROTO"] = await resolve(f"_atproto.{name}", "TXT")
        if not any(recs.values()):
            raise NotFound("No public DNS records (the domain may not exist)")
        src = ctx.out.source(f"dns:{name}", title=f"DNS records of {name}", request=f"domain “{name}”")
        txt = recs.get("TXT") or []
        spf = next((t for t in txt if t.lower().startswith("v=spf1")), None)
        dmarc = next((t for t in recs.get("DMARC") or [] if t.lower().startswith("v=dmarc1")), None)
        services = sorted({svc for t in txt + (recs.get("ATPROTO") or []) for tok, svc in SERVICE_TOKENS if tok.lower() in t.lower()})
        ctx.out.props(entity.key, {
            "dns": {"a": recs["A"], "aaaa": recs["AAAA"], "mx": [m.split()[-1].rstrip(".") for m in recs["MX"]],
                    "ns": [n.rstrip(".") for n in recs["NS"]], "caa": recs["CAA"], "spf": spf, "dmarc": dmarc,
                    "txt_count": len(txt)},
            "nameservers": [n.rstrip(".") for n in recs["NS"]], "services": services}, source=src)
        for ip in (recs["A"] + recs["AAAA"])[:8]:
            if is_public_ip(ip):
                ik = ctx.out.entity("ip", ip, source=src)
                ctx.out.link(entity.key, ik, "RESOLVES_TO", source=src, evidence="A/AAAA record")
        for t in recs.get("ATPROTO") or []:
            if t.startswith("did="):
                ctx.out.prop(entity.key, "atproto_did", t[4:], source=src, evidence="_atproto TXT record (Bluesky handle)")

    @transform(inputs=("ip",), outputs=("domain",), title="DNS: reverse lookup (PTR)", priority=0.4,
               accepts=lambda self, e, info: is_public_ip(e.value), sends="IP “{value}” to your DNS resolver")
    async def reverse(self, entity, ctx):
        names = await resolve(dns.reversename.from_address(entity.value).to_text(), "PTR")
        if not names:
            raise NotFound("No reverse DNS name")
        src = ctx.out.source(f"dns:{entity.value}/PTR", title=f"Reverse DNS of {entity.value}")
        for n in names[:5]:
            d = ctx.out.entity("domain", n.rstrip("."), source=src, props={"subdomain": not is_apex(n.rstrip("."))})
            ctx.out.link(d, entity.key, "RESOLVES_TO", source=src, confidence=0.8, evidence="PTR record (reverse DNS)")
