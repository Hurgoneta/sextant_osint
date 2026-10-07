"""RDAP (the structured successor of WHOIS) for domains and IP networks, via the rdap.org bootstrap."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound, SourceUnavailable
from ...sdk.http import FetchError
from ...util.normalize import normalize_email, normalize_phone
from ..dns.dns import is_apex

REDACTION_MARKERS = ("redacted", "privacy", "private", "proxy", "protected", "withheld", "not disclosed",
                     "data protected", "contact privacy", "domains by proxy", "whoisguard", "gdpr")
ACCEPT = {"Accept": "application/rdap+json, application/json"}


def vcard(entity: dict) -> dict:
    out: dict = {}
    arr = entity.get("vcardArray")
    if not (isinstance(arr, list) and len(arr) == 2 and isinstance(arr[1], list)):
        return out
    for item in arr[1]:
        if not (isinstance(item, list) and len(item) >= 4):
            continue
        key, value = item[0], item[3]
        if isinstance(value, list):
            value = " ".join(str(v) for v in value if v)
        if key in ("fn", "org", "email", "tel", "adr") and value:
            out.setdefault(key, str(value).strip())
    return out


def walk(entities: list, depth: int = 0):
    for e in entities or []:
        if isinstance(e, dict):
            yield e
            if depth < 2:
                yield from walk(e.get("entities") or [], depth + 1)


def parse_rdap(data: dict) -> dict:
    events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events") or [] if isinstance(e, dict)}
    registrar, registrant, abuse = None, {}, None
    for ent in walk(data.get("entities") or []):
        roles = ent.get("roles") or []
        card = vcard(ent)
        if "registrar" in roles and not registrar:
            registrar = card.get("fn") or card.get("org")
        if "registrant" in roles and not registrant:
            registrant = card
        if "abuse" in roles and not abuse:
            abuse = card.get("email")
    public = {k: v for k, v in registrant.items()
              if v and not any(m in v.lower() for m in REDACTION_MARKERS) and k in ("fn", "org", "email", "tel", "adr")}
    related = next((ln.get("href") for ln in data.get("links") or []
                    if isinstance(ln, dict) and ln.get("rel") == "related" and "rdap" in str(ln.get("href", ""))), None)
    return {"registered": events.get("registration"), "expires": events.get("expiration"),
            "last_changed": events.get("last changed"), "registrar": registrar, "registrant": public, "abuse": abuse,
            "nameservers": [n.get("ldhName", "").lower() for n in data.get("nameservers") or [] if isinstance(n, dict)],
            "status": data.get("status") or [], "related": related, "name": data.get("name"),
            "handle": data.get("handle"), "country": data.get("country"),
            "start": data.get("startAddress"), "end": data.get("endAddress")}


@register
class Rdap(Connector):
    manifest = Manifest(
        name="rdap", title="RDAP / WHOIS", category="rdap",
        description="Registration dates, registrar, publicly visible registrant data of domains; holder and range of IP "
                    "networks.",
        access="free", rate_limit=RateLimit(0.5, 2), website="https://rdap.org", documentation="https://about.rdap.org/",
        reliability="official", public_record=True,
        capabilities=("registration/expiry dates", "registrar", "registrant name/organisation/email where public",
                      "IP network holder and range"),
        disclosure=Disclosure(("rdap.org", "the registry/registrar RDAP server"), "the domain or IP being looked up", "public"),
    )

    @transform(inputs=("domain",), outputs=("organization", "email", "person", "phone"), title="RDAP: domain registration",
               priority=0.7, accepts=lambda self, e, info: is_apex(e.value))
    async def domain(self, entity, ctx):
        resp = await ctx.get(f"https://rdap.org/domain/{quote(entity.value)}", headers=ACCEPT)
        if resp.status == 404:
            raise NotFound("The registry has no RDAP record for this domain")
        if resp.status != 200:
            raise SourceUnavailable(f"RDAP answered HTTP {resp.status} (some ccTLDs have no RDAP service)")
        parsed = parse_rdap(ctx.json(resp) or {})
        url = resp.url
        if parsed["related"] and not parsed["registrant"]:
            try:
                r2 = await ctx.get(parsed["related"], headers=ACCEPT)
                if r2.status == 200:
                    d2 = parse_rdap(ctx.json(r2) or {})
                    parsed["registrant"] = d2["registrant"] or parsed["registrant"]
                    parsed["registrar"] = parsed["registrar"] or d2["registrar"]
                    url = r2.url
            except (FetchError, SourceUnavailable):
                pass
        src = ctx.out.source(url, title="RDAP domain record", request=f"domain “{entity.value}”")
        pub = parsed["registrant"]
        ctx.out.props(entity.key, {"registered": parsed["registered"], "expires": parsed["expires"],
                                   "registrar": parsed["registrar"], "nameservers": parsed["nameservers"],
                                   "registrant": pub or None, "registrant_public": bool(set(pub) & {"fn", "email", "tel", "adr"}),
                                   "rdap_status": parsed["status"]}, source=src)
        if pub.get("email"):
            e = ctx.out.entity("email", normalize_email(pub["email"]) or "", source=src)
            ctx.out.link(entity.key, e, "REGISTERED_TO", source=src, evidence="Registrant email in the public RDAP record")
        if pub.get("tel"):
            p = ctx.out.entity("phone", normalize_phone(pub["tel"]) or "", source=src)
            ctx.out.link(entity.key, p, "REGISTERED_TO", source=src, evidence="Registrant phone in the public RDAP record")
        if pub.get("org"):
            o = ctx.out.entity("organization", f"name:{pub['org'].casefold()}", label=pub["org"], source=src,
                               props={"name": pub["org"]})
            ctx.out.link(entity.key, o, "REGISTERED_TO", source=src, evidence="Registrant organisation (RDAP)")
        if pub.get("fn") and len(pub["fn"].split()) >= 2:
            pk = ctx.out.entity("person", f"rdap:{entity.value}", label=pub["fn"], source=src,
                                props={"full_name": pub["fn"], "role": "domain registrant", "location": pub.get("adr")})
            ctx.out.link(entity.key, pk, "REGISTERED_TO", source=src, evidence="Registrant name in the public RDAP record")

    @transform(inputs=("ip",), outputs=("organization", "asn"), title="RDAP: IP network holder", priority=0.5)
    async def ip(self, entity, ctx):
        resp = await ctx.get(f"https://rdap.org/ip/{quote(entity.value)}", headers=ACCEPT)
        if resp.status == 404:
            raise NotFound("No RDAP record for this address")
        parsed = parse_rdap(ctx.json(ctx.expect(resp)) or {})
        src = ctx.out.source(resp.url, title="RDAP IP network record", request=f"IP “{entity.value}”")
        holder = (parsed["registrant"] or {}).get("org") or (parsed["registrant"] or {}).get("fn") or parsed["name"]
        ctx.out.props(entity.key, {"network": f"{parsed['start']} – {parsed['end']}" if parsed["start"] else None,
                                   "org": holder, "country": parsed["country"]}, source=src)
        if holder:
            o = ctx.out.entity("organization", f"name:{str(holder).casefold()}", label=str(holder), source=src,
                               props={"name": holder})
            ctx.out.link(entity.key, o, "REGISTERED_TO", source=src, evidence="Network holder (RDAP)")
