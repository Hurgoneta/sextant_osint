"""OpenStreetMap Nominatim — geocoding of location texts (city, region, country, coordinates)."""

from __future__ import annotations

from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound


@register
class Nominatim(Connector):
    manifest = Manifest(
        name="nominatim", title="OpenStreetMap Nominatim", category="geolocation",
        description="Geocodes location texts found on profiles and records (city, region, country, coordinates).",
        access="free", rate_limit=RateLimit(1.0, 1, "usage policy: max 1 request/second, identifying User-Agent"),
        website="https://nominatim.openstreetmap.org", documentation="https://nominatim.org/release-docs/latest/api/Search/",
        reliability="high", license="ODbL (OpenStreetMap contributors)",
        legal="OSMF Nominatim usage policy: no heavy use, attribution required.",
        capabilities=("normalised city/region/country", "country code", "coordinates"),
        disclosure=Disclosure(("nominatim.openstreetmap.org",), "the location text", "public"),
    )

    @transform(inputs=("location",), outputs=(), title="Geocode this location", priority=0.3,
               accepts=lambda self, e, info: not e.get("lat") and len(str(e.get("text") or e.label)) >= 3)
    async def geocode(self, entity, ctx):
        text = str(entity.get("text") or entity.label)
        params = {"q": text, "format": "jsonv2", "limit": 1, "addressdetails": 1}
        if ctx.info.option("contact_email"):
            params["email"] = ctx.info.option("contact_email")
        resp = await ctx.get("https://nominatim.openstreetmap.org/search", params=params, cache_ttl=30 * 86_400)
        rows = ctx.json(ctx.expect(resp)) or []
        if not rows:
            raise NotFound("Location not found in OpenStreetMap")
        r = rows[0]
        a = r.get("address") or {}
        src = ctx.out.source(f"https://www.openstreetmap.org/{r.get('osm_type')}/{r.get('osm_id')}", title="OpenStreetMap",
                             request=f"“{text}”")
        ctx.out.props(entity.key, {"city": a.get("city") or a.get("town") or a.get("village"), "region": a.get("state"),
                                   "country": a.get("country"), "country_code": str(a.get("country_code") or "").upper() or None,
                                   "lat": float(r["lat"]) if r.get("lat") else None, "lon": float(r["lon"]) if r.get("lon") else None,
                                   "display_name": r.get("display_name")}, source=src, confidence=0.8)
