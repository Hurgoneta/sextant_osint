"""Commercial people-data providers — available only with the investigator's own subscription.

These are lawful data businesses with their own compliance requirements (KYC, permissible purpose).
The connectors never work without your credentials; they also respect each provider's terms by
sending only the identifiers already in the investigation.
"""

from __future__ import annotations

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from .._common import add_link_target


def _emit_person(ctx, src, key: str, full: str, props: dict, link_from: str, links: list[str], emails: list[str],
                 phones: list[str], employer: str | None) -> None:
    pk = ctx.out.entity("person", key, label=full, source=src, props={"full_name": full, **props},
                        evidence="Record returned by the provider for this identifier", confidence=0.8)
    ctx.out.link(pk, link_from, "USES", source=src, confidence=0.8, evidence="The provider's record contains this identifier")
    for e in emails[:5]:
        ek = ctx.out.entity("email", e, source=src)
        ctx.out.link(pk, ek, "USES", source=src, confidence=0.7, evidence="Email in the provider's record")
    for p in phones[:5]:
        phk = ctx.out.entity("phone", p, source=src)
        ctx.out.link(pk, phk, "USES", source=src, confidence=0.7, evidence="Phone in the provider's record")
    for url in links[:10]:
        add_link_target(ctx.out, pk, url if "://" in url else f"https://{url}", src)
    if employer:
        org = ctx.out.entity("organization", f"name:{employer.casefold()}", label=employer, source=src, props={"name": employer})
        ctx.out.link(pk, org, "WORKS_FOR", source=src, confidence=0.7, evidence="Employer in the provider's record")
    nk = ctx.out.entity("name", full, source=src)
    ctx.out.link(pk, nk, "HAS_NAME", source=src, evidence="Name in the provider's record")


@register
class PeopleDataLabs(Connector):
    manifest = Manifest(
        name="peopledatalabs", title="People Data Labs", category="people_search",
        description="Person enrichment from an email or phone: name, employment, education, social profiles.",
        access="paid",
        credentials=(Credential("PDL_API_KEY", "People Data Labs API key", "Free tier with limited credits; paid plans.",
                                obtain_url="https://dashboard.peopledatalabs.com/"),),
        cost="Credits per match (free tier available)", cost_per_call_usd=0.10, max_calls_per_investigation=10,
        rate_limit=RateLimit(1.0, 1), website="https://www.peopledatalabs.com",
        documentation="https://docs.peopledatalabs.com/docs/enrichment-api", reliability="medium",
        legal="PDL terms and applicable privacy law (GDPR/CCPA); use only with a lawful basis.",
        capabilities=("full name", "job title and employer", "social profile URLs", "locations"),
        disclosure=Disclosure(("api.peopledatalabs.com",), "the email address or phone number"), auto_run=False,
    )

    @transform(inputs=("email", "phone"), outputs=("person", "account", "organization"),
               title="People Data Labs: enrich this identifier", priority=0.5)
    async def enrich(self, entity, ctx):
        (key,) = ctx.require("PDL_API_KEY")
        param = "email" if entity.type == "email" else "phone"
        resp = await ctx.get("https://api.peopledatalabs.com/v5/person/enrich", params={param: entity.value, "min_likelihood": 6},
                             headers={"X-Api-Key": key})
        if resp.status == 404:
            raise NotFound("No matching person record")
        d = (ctx.json(ctx.expect(resp)) or {}).get("data") or {}
        src = ctx.out.source("https://www.peopledatalabs.com", title="People Data Labs enrichment", request=f"{param} “{entity.value}”")
        links = [p.get("url") for p in d.get("profiles") or [] if p.get("url")]
        _emit_person(ctx, src, f"pdl:{d.get('id')}", d.get("full_name") or "", {
            "occupation": d.get("job_title"), "location": d.get("location_name")}, entity.key, links,
            [e.get("address") for e in d.get("emails") or [] if isinstance(e, dict) and e.get("address")],
            [p for p in d.get("phone_numbers") or [] if isinstance(p, str)], d.get("job_company_name"))


@register
class Pipl(Connector):
    manifest = Manifest(
        name="pipl", title="Pipl", category="people_search",
        description="Identity resolution search by email, phone or username (business subscription with compliance review).",
        access="enterprise",
        credentials=(Credential("PIPL_API_KEY", "Pipl API key", obtain_url="https://pipl.com/api"),),
        cost="Subscription (contact provider)", cost_per_call_usd=0.25, max_calls_per_investigation=10,
        rate_limit=RateLimit(1.0, 1), website="https://pipl.com", documentation="https://docs.pipl.com/reference/",
        reliability="medium", legal="Pipl requires an approved account and a permissible purpose.",
        capabilities=("names, usernames, emails, phones linked to the identifier", "social profile URLs", "jobs"),
        disclosure=Disclosure(("api.pipl.com",), "the email, phone or username"), auto_run=False,
    )

    @transform(inputs=("email", "phone", "username"), outputs=("person", "account"), title="Pipl: person search",
               priority=0.5)
    async def search(self, entity, ctx):
        (key,) = ctx.require("PIPL_API_KEY")
        param = {"email": "email", "phone": "phone", "username": "username"}[entity.type]
        resp = await ctx.get("https://api.pipl.com/search/", params={param: entity.display, "key": key})
        d = ctx.json(ctx.expect(resp)) or {}
        p = d.get("person")
        if not p:
            raise NotFound("No person match")
        src = ctx.out.source("https://pipl.com", title="Pipl search", request=f"{param} “{entity.display}”")
        names = [n.get("display") for n in p.get("names") or [] if n.get("display")]
        _emit_person(ctx, src, f"pipl:{p.get('@id') or entity.value}", names[0] if names else "",
                     {"aliases": names[1:5]}, entity.key, [u.get("url") for u in p.get("urls") or [] if u.get("url")],
                     [e.get("address") for e in p.get("emails") or [] if e.get("address")],
                     [ph.get("display_international") for ph in p.get("phones") or [] if ph.get("display_international")],
                     ((p.get("jobs") or [{}])[0]).get("organization"))
