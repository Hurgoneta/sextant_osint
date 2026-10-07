"""Phone intelligence: offline analysis (libphonenumber) and keyed carrier/line-type lookups.

No subscriber databases, HLR lookups, caller-ID apps (Truecaller etc.) or messenger contact-discovery
abuse. Where a number is publicly *published* is found by the search connectors, using the format
variants generated here.
"""

from __future__ import annotations

import base64
from urllib.parse import quote

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...util.normalize import location_tokens, parse_phone


@register
class PhoneOffline(Connector):
    manifest = Manifest(
        name="phone_offline", title="Phone analysis (offline)", category="phone",
        description="Validity, country/region, original carrier, line type, time zones and the formats people write "
                    "the number in. Offline metadata only — nothing is sent.",
        access="local", rate_limit=RateLimit(100, 100), website="https://github.com/google/libphonenumber",
        reliability="high", official_api=False, access_method="local", license="Apache-2.0 (metadata)",
        capabilities=("valid / possible", "country and allocated region", "original carrier", "line type", "format variants"),
        disclosure=Disclosure((), "nothing — runs locally", "none"),
    )

    @transform(inputs=("phone",), outputs=("location",), title="Phone: offline analysis", priority=0.9, sends="nothing (local)")
    async def analyse(self, entity, ctx):
        info = parse_phone(entity.value)
        if info is None:
            return
        src = ctx.out.source(None, title="libphonenumber offline metadata", kind="local")
        d = info.to_dict()
        ctx.out.props(entity.key, {k: d[k] for k in ("international", "national", "region", "region_name", "carrier",
                                                     "number_type", "valid", "timezones", "search_variants")}, source=src,
                      evidence="Original carrier of the number range — ported numbers may have moved")
        if info.region_name and location_tokens(info.region_name):
            le = ctx.out.entity("location", info.region_name, source=src,
                                props={"text": info.region_name, "country_code": info.region, "kind": "number allocation"})
            ctx.out.link(entity.key, le, "LOCATED_IN", source=src, confidence=0.4,
                         evidence="Region the number range is allocated to (not necessarily where the owner lives)")


@register
class NumVerify(Connector):
    manifest = Manifest(
        name="numverify", title="numverify (apilayer)", category="phone",
        description="Validation with current carrier and line type for many countries.", access="free_key",
        credentials=(Credential("NUMVERIFY_API_KEY", "apilayer API key (Number Verification)",
                                obtain_url="https://apilayer.com/marketplace/number_verification-api"),),
        cost="Free tier (100 requests/month), then paid", rate_limit=RateLimit(0.5, 1), website="https://apilayer.com",
        documentation="https://apilayer.com/marketplace/number_verification-api", reliability="medium",
        capabilities=("valid", "carrier", "line type", "location"),
        disclosure=Disclosure(("api.apilayer.com",), "the phone number"),
    )

    @transform(inputs=("phone",), outputs=(), title="numverify: carrier and line type", priority=0.4)
    async def validate(self, entity, ctx):
        (key,) = ctx.require("NUMVERIFY_API_KEY")
        resp = await ctx.get("https://api.apilayer.com/number_verification/validate", params={"number": entity.value.lstrip("+")},
                             headers={"apikey": key})
        d = ctx.json(ctx.expect(resp)) or {}
        src = ctx.out.source("https://apilayer.com/marketplace/number_verification-api", title="numverify",
                             request=f"number “{entity.value}”")
        ctx.out.props(entity.key, {"valid": d.get("valid"), "current_carrier": d.get("carrier"), "line_type": d.get("line_type"),
                                   "location": d.get("location")}, source=src, confidence=0.8)


@register
class TwilioLookup(Connector):
    manifest = Manifest(
        name="twilio_lookup", title="Twilio Lookup v2", category="phone",
        description="Line-type intelligence and (US numbers) caller name (CNAM) from your Twilio account.", access="paid",
        credentials=(Credential("TWILIO_ACCOUNT_SID", "Twilio account SID", secret=False, obtain_url="https://console.twilio.com/"),
                     Credential("TWILIO_AUTH_TOKEN", "Twilio auth token")),
        cost="≈ $0.008–0.01 per lookup", cost_per_call_usd=0.01, max_calls_per_investigation=20,
        rate_limit=RateLimit(1.0, 2), website="https://www.twilio.com/lookup",
        documentation="https://www.twilio.com/docs/lookup/v2-api", reliability="high",
        legal="Caller-name (CNAM) data is US-only and subject to Twilio's acceptable-use policy.",
        capabilities=("line type (mobile/landline/VoIP)", "current carrier", "caller name (US CNAM)"),
        disclosure=Disclosure(("lookups.twilio.com",), "the phone number"), auto_run=False,
    )

    @transform(inputs=("phone",), outputs=("name",), title="Twilio: line type and caller name", priority=0.4)
    async def lookup(self, entity, ctx):
        sid, token = ctx.require("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        resp = await ctx.get(f"https://lookups.twilio.com/v2/PhoneNumbers/{quote(entity.value)}",
                             params={"Fields": "line_type_intelligence,caller_name"}, headers={"Authorization": f"Basic {auth}"})
        d = ctx.json(ctx.expect(resp)) or {}
        lti = d.get("line_type_intelligence") or {}
        cn = d.get("caller_name") or {}
        src = ctx.out.source("https://www.twilio.com/docs/lookup/v2-api", title="Twilio Lookup v2", request=f"number “{entity.value}”")
        ctx.out.props(entity.key, {"line_type": lti.get("type"), "current_carrier": lti.get("carrier_name"),
                                   "caller_name": cn.get("caller_name"), "caller_type": cn.get("caller_type")}, source=src)
        if cn.get("caller_name") and cn.get("caller_type") == "CONSUMER" and len(str(cn["caller_name"]).split()) >= 2:
            nk = ctx.out.entity("name", cn["caller_name"], source=src)
            ctx.out.link(entity.key, nk, "ASSOCIATED_WITH", source=src, confidence=0.6,
                         evidence="Caller name (CNAM) registered for the number")
