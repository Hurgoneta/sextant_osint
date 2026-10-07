"""Email intelligence: local analysis, Gravatar, OpenPGP keyserver, Hunter.io, EmailRep."""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import (
    email_domain, email_variants, is_free_email_provider, normalize_email, normalize_name, normalize_username,
    registered_domain,
)
from .._common import add_account, avatar_fingerprint
from ..dns.dns import resolve

YANDEX = ("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.ua", "narod.ru")
GENERIC_LOCAL = frozenset({
    "info", "contact", "mail", "email", "admin", "office", "hello", "hi", "support", "sales", "team", "me", "test",
    "noreply", "no-reply", "webmaster", "postmaster", "billing", "help", "jobs", "hr", "press", "privacy", "security",
    "abuse", "hostmaster", "marketing", "careers", "legal", "accounts",
})
MX_PROVIDERS = (
    ("google.com", "Google Workspace / Gmail"), ("googlemail.com", "Google Workspace / Gmail"),
    ("outlook.com", "Microsoft 365"), ("protection.outlook.com", "Microsoft 365"), ("protonmail.ch", "Proton Mail"),
    ("zoho", "Zoho Mail"), ("messagingengine.com", "Fastmail"), ("yahoodns.net", "Yahoo"), ("icloud.com", "iCloud Mail"),
    ("mailgun.org", "Mailgun"), ("secureserver.net", "GoDaddy"), ("ovh.net", "OVH"), ("one.com", "one.com"),
)


def local_part_username(email: str) -> tuple[str, float, str] | None:
    local, domain = email.split("@", 1)
    base = local.split("+", 1)[0]
    u = normalize_username(base)
    if not u or len(u) < 4 or u.casefold() in GENERIC_LOCAL:
        return None
    if domain in YANDEX:
        return u, 0.95, "This is the Yandex login (Yandex services use it as the username)."
    return u, 0.75, "The part before @ — people often reuse it as a username elsewhere."


@register
class EmailAnalysis(Connector):
    manifest = Manifest(
        name="email_analysis", title="Email analysis (local + MX)", category="email",
        description="Classifies the provider offline, derives the likely username from the part before @, normalises "
                    "provider aliases, and looks up MX records of custom domains.",
        access="local", cost="Free", rate_limit=RateLimit(5, 5), website="https://datatracker.ietf.org/doc/html/rfc5321",
        reliability="high", official_api=False, access_method="local",
        capabilities=("free vs. custom email domain", "mail provider (from MX)", "likely username", "domain to investigate"),
        disclosure=Disclosure(("your DNS resolver",), "only the domain part of the address (MX lookup)", "public"),
    )

    @transform(inputs=("email",), outputs=("username", "domain"), title="Email: provider, domain and likely username",
               priority=0.9, sends="only the domain “{value}” to your DNS resolver (custom domains)")
    async def analyse(self, entity, ctx):
        email = entity.value
        domain = email_domain(email)
        src = ctx.out.source(None, title="Local email analysis", kind="local", request="nothing sent (local) + MX lookup")
        free = is_free_email_provider(domain)
        variants = email_variants(email)
        ctx.out.props(entity.key, {"free_provider": free, "provider": domain if free else None,
                                   "provider_canonical": variants.get("provider_canonical")}, source=src)
        cand = local_part_username(email)
        if cand:
            uname, conf, note = cand
            uk = ctx.out.entity("username", uname, source=src, props={"username": uname, "derived_from": email},
                                evidence=note, confidence=conf)
            ctx.out.link(entity.key, uk, "ASSOCIATED_WITH", source=src, confidence=conf,
                         evidence=f"Likely username derived from the address ({note})")
        if free:
            return
        mx = [m.split()[-1].rstrip(".") for m in await resolve(domain, "MX")]
        provider = next((name for host, name in MX_PROVIDERS for m in mx if host in m), None)
        dsrc = ctx.out.source(f"dns:{domain}/MX", title=f"DNS MX records of {domain}", kind="api", request=f"domain “{domain}”")
        if mx:
            ctx.out.prop(entity.key, "mx", mx, source=dsrc)
        if provider:
            ctx.out.prop(entity.key, "provider", provider, source=dsrc)
        rd = registered_domain(domain) or domain
        dk = ctx.out.entity("domain", rd, source=src, evidence=f"Domain of {email}")
        # 0.6: a custom email domain is often the person's own, but may be an employer's or a school's
        ctx.out.link(entity.key, dk, "REGISTERED_TO", source=src, confidence=0.6,
                     evidence="The address is hosted on this domain (own, employer's or school's)")


@register
class Gravatar(Connector):
    manifest = Manifest(
        name="gravatar", title="Gravatar", category="email",
        description="Public Gravatar profile registered to an email address (verified accounts, links, name, photo).",
        access="free",
        credentials=(Credential("GRAVATAR_API_KEY", "Gravatar API key", "Optional; raises rate limits.", required=False,
                                obtain_url="https://gravatar.com/developers"),),
        rate_limit=RateLimit(1.0, 2), website="https://gravatar.com", documentation="https://docs.gravatar.com/api/profiles/rest-api/",
        reliability="high", capabilities=("profile tied to the email", "display name, location, company, job title",
                                          "verified accounts on other platforms", "avatar"),
        disclosure=Disclosure(("api.gravatar.com",), "a SHA-256 hash of the email address", "hashed",
                              "Hashes of email addresses can be reversed by guessing — treat it as revealing the address."),
    )

    @transform(inputs=("email",), outputs=("account", "domain", "image", "name"), title="Gravatar: profile for this email",
               priority=0.8, sends="SHA-256 hash of “{value}”")
    async def profile(self, entity, ctx):
        digest = hashlib.sha256(entity.value.strip().lower().encode()).hexdigest()
        headers = {"Accept": "application/json"}
        key = ctx.credential("GRAVATAR_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = await ctx.get(f"https://api.gravatar.com/v3/profiles/{digest}", headers=headers)
        if resp.status == 404:
            ctx.out.check("gravatar", "not_found", platform_title="Gravatar", identifier_type="email")
            raise NotFound("No public Gravatar profile for this address")
        p = ctx.json(ctx.expect(resp)) or {}
        verified = [a.get("url") for a in p.get("verified_accounts") or [] if isinstance(a, dict) and a.get("url")]
        links = verified + [lk.get("url") for lk in p.get("links") or [] if isinstance(lk, dict) and lk.get("url")]
        username = str(p.get("profile_url") or "").rstrip("/").rsplit("/", 1)[-1] or digest[:12]
        src = ctx.out.source(p.get("profile_url") or f"https://gravatar.com/{digest}", title="Gravatar profile API",
                             request="SHA-256 of the address")
        profile = {"username": username, "display_name": p.get("display_name"), "bio": p.get("description"),
                   "location": p.get("location"), "company": p.get("company"), "avatar_url": p.get("avatar_url"),
                   "profile_url": p.get("profile_url"), "links": links, "verified_links": verified,
                   "avatar_fp": await avatar_fingerprint(ctx, p.get("avatar_url")),
                   "extra": {"job_title": p.get("job_title"), "pronouns": p.get("pronouns")}}
        acc = add_account(ctx.out, src, platform="gravatar", username=username, profile=profile, link_from=entity.key,
                          found_by="exact email lookup")
        ctx.out.check("gravatar", "found", url=p.get("profile_url"), account=acc, platform_title="Gravatar",
                      identifier_type="email")


def openpgp_user_ids(armored: str, limit: int = 20) -> list[str]:
    """Extracts User ID packets (tag 13) from an ASCII-armored OpenPGP public key. Pure, defensive."""
    m = re.search(r"-----BEGIN PGP PUBLIC KEY BLOCK-----(.*?)-----END PGP PUBLIC KEY BLOCK-----", armored, re.S)
    if not m:
        return []
    body = [ln for ln in m.group(1).strip().splitlines() if ln and ":" not in ln and not ln.startswith("=")]
    try:
        data = base64.b64decode("".join(body), validate=False)
    except ValueError:
        return []
    out, i = [], 0
    while i < len(data) and len(out) < limit:
        ctb = data[i]
        if not ctb & 0x80:
            break
        if ctb & 0x40:                                   # new format
            tag = ctb & 0x3F
            i += 1
            if i >= len(data):
                break
            l1 = data[i]
            if l1 < 192:
                length, i = l1, i + 1
            elif l1 < 224:
                length, i = ((l1 - 192) << 8) + data[i + 1] + 192, i + 2
            elif l1 == 255:
                length, i = int.from_bytes(data[i + 1:i + 5], "big"), i + 5
            else:
                break                                    # partial lengths: not used for keys
        else:                                            # old format
            tag = (ctb >> 2) & 0x0F
            ltype = ctb & 0x03
            i += 1
            n = {0: 1, 1: 2, 2: 4}.get(ltype)
            if n is None:
                break
            length, i = int.from_bytes(data[i:i + n], "big"), i + n
        if tag == 13:
            out.append(data[i:i + length].decode("utf-8", "replace"))
        i += length
    return out


@register
class OpenPgpKeyserver(Connector):
    manifest = Manifest(
        name="openpgp_keyserver", title="keys.openpgp.org", category="email",
        description="Public OpenPGP key published for an email address. keys.openpgp.org only publishes identities whose "
                    "email was verified, so the User ID (often a full name) is strong evidence.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://keys.openpgp.org",
        documentation="https://keys.openpgp.org/about/api", reliability="high",
        capabilities=("verified OpenPGP key for the email", "name in the key's User ID", "key fingerprint"),
        disclosure=Disclosure(("keys.openpgp.org",), "the email address being checked"),
    )

    @transform(inputs=("email",), outputs=("name",), title="OpenPGP: verified key and name for this email", priority=0.5)
    async def key_lookup(self, entity, ctx):
        resp = await ctx.get(f"https://keys.openpgp.org/vks/v1/by-email/{quote(entity.value, safe='')}")
        if resp.status == 404:
            raise NotFound("No verified OpenPGP key published for this address")
        ctx.expect(resp)
        uids = openpgp_user_ids(resp.text)
        src = ctx.out.source(f"https://keys.openpgp.org/search?q={quote(entity.value)}", title="keys.openpgp.org VKS",
                             request=f"email “{entity.value}”")
        ctx.out.prop(entity.key, "pgp_user_ids", uids, source=src, evidence="User IDs of the published key")
        for uid in uids:
            m = re.match(r"^\s*([^<(]+?)\s*(?:\(.*?\))?\s*<([^>]+)>", uid)
            if m and normalize_email(m.group(2)) == entity.value:
                n = normalize_name(m.group(1))
                if n and len(n.split()) >= 2:
                    nk = ctx.out.entity("name", n, source=src, evidence="Name in a verified OpenPGP User ID")
                    ctx.out.link(entity.key, nk, "ASSOCIATED_WITH", source=src, confidence=0.9,
                                 evidence=f"OpenPGP key for this address names “{n}” (address verified by the keyserver)")


@register
class Hunter(Connector):
    manifest = Manifest(
        name="hunter", title="Hunter.io", category="email",
        description="Email verification and the public email addresses Hunter has found for a domain (with names and "
                    "positions, from public web sources).",
        access="free_key",
        credentials=(Credential("HUNTER_API_KEY", "Hunter.io API key", "Free plan: 25 searches/50 verifications per month.",
                                obtain_url="https://hunter.io/api-keys"),),
        cost="Free tier, then paid plans", rate_limit=RateLimit(1.0, 2, "15 requests/second; monthly plan quota"),
        website="https://hunter.io", documentation="https://hunter.io/api-documentation/v2", reliability="medium",
        capabilities=("email deliverability", "emails published for a domain", "names and positions of people at a domain",
                      "sources where each email was found"),
        disclosure=Disclosure(("api.hunter.io",), "email addresses and domains being checked"),
    )

    @transform(inputs=("domain",), outputs=("email", "person", "website"), title="Hunter: public emails at this domain",
               priority=0.5, accepts=lambda self, e, info: not e.get("subdomain"))
    async def domain_search(self, entity, ctx):
        (key,) = ctx.require("HUNTER_API_KEY")
        resp = await ctx.get("https://api.hunter.io/v2/domain-search", params={"domain": entity.value, "api_key": key,
                                                                              "limit": 25})
        data = (ctx.json(ctx.expect(resp)) or {}).get("data") or {}
        src = ctx.out.source(f"https://hunter.io/search/{entity.value}", title="Hunter domain search",
                             request=f"domain “{entity.value}”")
        if data.get("organization"):
            org = ctx.out.entity("organization", f"name:{data['organization'].casefold()}", label=data["organization"],
                                 source=src, props={"name": data["organization"], "website": entity.value})
            ctx.out.link(entity.key, org, "REGISTERED_TO", source=src, confidence=0.7, evidence="Organization per Hunter")
        for e in data.get("emails") or []:
            ek = ctx.out.entity("email", e.get("value"), source=src, props={"hunter_confidence": e.get("confidence")},
                                evidence="Found on public web sources by Hunter")
            ctx.out.link(ek, entity.key, "REGISTERED_TO", source=src, evidence="Address at this domain")
            full = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x)
            if ek and len(full.split()) >= 2:
                pk = ctx.out.entity("person", f"hunter:{e.get('value')}", label=full, source=src,
                                    props={"full_name": full, "occupation": e.get("position")})
                ctx.out.link(pk, ek, "USES", source=src, evidence="Name attached to the address by Hunter")
            for s in (e.get("sources") or [])[:3]:
                if s.get("uri"):
                    w = ctx.out.entity("website", s["uri"], source=src, props={"url": s["uri"], "host": s.get("domain")})
                    ctx.out.link(ek, w, "MENTIONED_IN", source=src, evidence="Page where Hunter found the address")

    @transform(inputs=("email",), outputs=(), title="Hunter: verify the address", priority=0.3)
    async def verify(self, entity, ctx):
        (key,) = ctx.require("HUNTER_API_KEY")
        resp = await ctx.get("https://api.hunter.io/v2/email-verifier", params={"email": entity.value, "api_key": key})
        data = (ctx.json(ctx.expect(resp)) or {}).get("data") or {}
        src = ctx.out.source("https://hunter.io/verify", title="Hunter email verifier", request=f"email “{entity.value}”")
        ctx.out.props(entity.key, {"deliverability": data.get("status"), "deliverability_score": data.get("score"),
                                   "disposable": data.get("disposable"), "webmail": data.get("webmail")}, source=src)


@register
class EmailRep(Connector):
    manifest = Manifest(
        name="emailrep", title="EmailRep", category="email",
        description="Reputation of an email address and the services where it has public profiles.", access="free_key",
        credentials=(Credential("EMAILREP_API_KEY", "EmailRep API key", obtain_url="https://emailrep.io/key"),),
        cost="Free tier (limited queries/day)", rate_limit=RateLimit(0.5, 1), website="https://emailrep.io",
        documentation="https://docs.sublimesecurity.com/reference/get_-email", reliability="medium",
        capabilities=("reputation & suspicious flags", "first/last seen", "services with public profiles",
                      "breach exposure flag (no details)"),
        disclosure=Disclosure(("emailrep.io",), "the email address being checked"),
    )

    @transform(inputs=("email",), outputs=(), title="EmailRep: reputation and profile services", priority=0.4)
    async def reputation(self, entity, ctx):
        (key,) = ctx.require("EMAILREP_API_KEY")
        resp = await ctx.get(f"https://emailrep.io/{quote(entity.value, safe='')}",
                             headers={"Key": key, "User-Agent": "Sextant", "Accept": "application/json"})
        d = ctx.json(ctx.expect(resp)) or {}
        det = d.get("details") or {}
        src = ctx.out.source(f"https://emailrep.io/{entity.value}", title="EmailRep", request=f"email “{entity.value}”")
        ctx.out.props(entity.key, {"reputation": d.get("reputation"), "suspicious": d.get("suspicious"),
                                   "first_seen": det.get("first_seen"), "last_seen": det.get("last_seen"),
                                   "profile_services": det.get("profiles"), "data_breach": det.get("data_breach")},
                      source=src, confidence=0.8)
