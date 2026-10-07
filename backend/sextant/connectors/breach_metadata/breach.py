"""Breach-exposure metadata only: *which* breaches and *what kinds* of data — never passwords, hashes or leaked
records. The connectors below do not request them, and nothing resembling a credential is ever stored."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.extract import html_to_text

PASSWORD_NOTE = "Credentials: NOT retrieved and NOT stored — metadata only"


def add_breach(ctx, src, entity_key: str, *, name: str, title: str, date: str | None, domain: str | None,
               classes: list[str], description: str, verified: bool | None, records: int | None, confidence: float = 1.0,
               evidence: str = "Included in this breach") -> None:
    b = ctx.out.entity("breach", name, label=title, source=src,
                       props={"title": title, "breach_date": date, "domain": domain, "data_classes": classes,
                              "description": description[:800], "verified": verified, "records": records,
                              "note": PASSWORD_NOTE})
    ctx.out.link(entity_key, b, "EXPOSED_IN", source=src, confidence=confidence, evidence=evidence)


@register
class Hibp(Connector):
    manifest = Manifest(
        name="hibp", title="Have I Been Pwned", category="breach_metadata",
        description="Known breaches that included an email address (metadata only).", access="paid",
        credentials=(Credential("HIBP_API_KEY", "HIBP API key", "From $3.95/month (Pwned 1, 10 requests/minute).",
                                obtain_url="https://haveibeenpwned.com/API/Key"),),
        cost="From $3.95/month", rate_limit=RateLimit(0.15, 1, "10 requests/minute on the lowest tier"),
        website="https://haveibeenpwned.com", documentation="https://haveibeenpwned.com/API/v3", reliability="high",
        legal="HIBP terms: attribution required; searching addresses you are not authorised to investigate may be restricted.",
        capabilities=("breaches containing the address", "breach date", "exposed data classes", "verified/sensitive flags"),
        disclosure=Disclosure(("haveibeenpwned.com",), "the email address"), auto_run=True,
    )

    @transform(inputs=("email",), outputs=("breach",), title="HIBP: breaches containing this address", priority=0.6)
    async def breaches(self, entity, ctx):
        (key,) = ctx.require("HIBP_API_KEY")
        resp = await ctx.get(f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote(entity.value, safe='')}",
                             params={"truncateResponse": "false"}, headers={"hibp-api-key": key}, cache_ttl=6 * 3600)
        if resp.status == 404:
            raise NotFound("Not in any breach known to HIBP")
        items = ctx.json(ctx.expect(resp)) or []
        src = ctx.out.source(f"https://haveibeenpwned.com/account/{quote(entity.value)}", title="Have I Been Pwned",
                             request=f"email “{entity.value}”")
        for b in items:
            add_breach(ctx, src, entity.key, name=b.get("Name", "unknown"), title=b.get("Title") or b.get("Name", "Breach"),
                       date=b.get("BreachDate"), domain=b.get("Domain"), classes=list(b.get("DataClasses") or []),
                       description=html_to_text(b.get("Description") or ""), verified=b.get("IsVerified"),
                       records=b.get("PwnCount"))


@register
class XposedOrNot(Connector):
    manifest = Manifest(
        name="xposedornot", title="XposedOrNot", category="breach_metadata",
        description="Free breach-exposure lookup (metadata only) by a third-party aggregator.", access="free",
        rate_limit=RateLimit(0.5, 1), website="https://xposedornot.com", documentation="https://xposedornot.com/api_doc",
        reliability="medium", default_enabled=False,
        opt_in_reason="Third-party aggregator — enable it only if you accept sharing the address with it.",
        capabilities=("breaches containing the address", "exposed data classes", "breach year"),
        disclosure=Disclosure(("api.xposedornot.com",), "the email address"),
    )

    @transform(inputs=("email",), outputs=("breach",), title="XposedOrNot: breaches containing this address", priority=0.5)
    async def breaches(self, entity, ctx):
        resp = await ctx.get("https://api.xposedornot.com/v1/breach-analytics", params={"email": entity.value},
                             cache_ttl=6 * 3600)
        if resp.status == 404:
            raise NotFound("Not in XposedOrNot's breach data")
        data = ctx.json(ctx.expect(resp)) or {}
        items = ((data.get("ExposedBreaches") or {}).get("breaches_details")) or []
        if not items:
            raise NotFound("Not in XposedOrNot's breach data")
        src = ctx.out.source("https://xposedornot.com/", title="XposedOrNot breach analytics", request=f"email “{entity.value}”")
        for b in items:
            year = str(b.get("xposed_date") or "")
            add_breach(ctx, src, entity.key, name=b.get("breach", "unknown"), title=b.get("breach", "Breach"),
                       date=f"{year}-01-01" if year.isdigit() else None, domain=b.get("domain"),
                       classes=[c.strip() for c in str(b.get("xposed_data") or "").split(";") if c.strip()],
                       description=str(b.get("details") or ""), verified=str(b.get("verified")).lower() == "yes",
                       records=b.get("xposed_records"))


@register
class HudsonRock(Connector):
    manifest = Manifest(
        name="hudsonrock", title="Hudson Rock Cavalier (info-stealer exposure)", category="breach_metadata",
        description="Whether an email or username appears in info-stealer malware logs — date and malware family only. "
                    "Passwords and logins in the response are discarded unread.", access="free",
        rate_limit=RateLimit(0.3, 1), website="https://www.hudsonrock.com",
        documentation="https://cavalier.hudsonrock.com/docs", reliability="medium", default_enabled=False,
        opt_in_reason="Third-party threat-intelligence service — enable it if you accept sharing identifiers with it.",
        capabilities=("compromise date", "info-stealer family", "operating system of the infected machine"),
        disclosure=Disclosure(("cavalier.hudsonrock.com",), "the email address or username"),
    )

    async def _lookup(self, ctx, entity, path: str, param: str):
        resp = await ctx.get(f"https://cavalier.hudsonrock.com/api/json/v2/osint-tools/{path}",
                             params={param: entity.display if entity.type == "username" else entity.value})
        d = ctx.json(ctx.expect(resp)) or {}
        stealers = d.get("stealers") or []
        if not stealers:
            raise NotFound("Not found in Hudson Rock's info-stealer data")
        src = ctx.out.source("https://www.hudsonrock.com/threat-intelligence-cybercrime-tools", title="Hudson Rock Cavalier",
                             request=f"{entity.type} “{entity.display}”")
        for i, s in enumerate(stealers[:10]):
            date = str(s.get("date_compromised") or "")[:10]
            add_breach(ctx, src, entity.key, name=f"infostealer:{entity.value}:{date or i}",
                       title=f"Info-stealer infection {date}".strip(), date=date or None, domain=None,
                       classes=["Credentials captured by info-stealer malware (not retrieved)"],
                       description=f"Malware family: {s.get('stealer_family') or 'unknown'}; OS: {s.get('operating_system') or 'unknown'}. "
                                   f"Matched by {entity.type}; for usernames this may concern someone else.",
                       verified=None, records=None, confidence=1.0 if entity.type == "email" else 0.6,
                       evidence=f"{entity.type.capitalize()} found in info-stealer logs")

    @transform(inputs=("email",), outputs=("breach",), title="Hudson Rock: info-stealer exposure (email)", priority=0.4)
    async def by_email(self, entity, ctx):
        await self._lookup(ctx, entity, "search-by-email", "email")

    @transform(inputs=("username",), outputs=("breach",), title="Hudson Rock: info-stealer exposure (username)", priority=0.3)
    async def by_username(self, entity, ctx):
        await self._lookup(ctx, entity, "search-by-username", "username")
