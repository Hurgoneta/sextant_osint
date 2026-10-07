"""TinEye — reverse image search through the official API (paid key).

Finds copies of the *same image* (resized, cropped, edited) and the pages that show them; it does not
recognise faces. Only the public URL of a profile photo is sent. Face-recognition engines (PimEyes,
FaceCheck.ID) are deliberately not integrated: they process biometric data and return look-alikes.
"""

from __future__ import annotations

from ...sdk import Connector, Credential, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import NotFound
from ...util.normalize import canonical_url, url_host

API = "https://api.tineye.com/rest/search/"


@register
class TinEye(Connector):
    manifest = Manifest(
        name="tineye", title="TinEye reverse image search", category="images",
        description="Web pages showing copies of a profile photo (official TinEye API).", access="paid",
        credentials=(Credential("TINEYE_API_KEY", "TinEye API key", obtain_url="https://services.tineye.com/TinEyeAPI"),),
        cost="Prepaid search bundles", cost_per_call_usd=0.04, max_calls_per_investigation=15,
        rate_limit=RateLimit(1.0, 1), website="https://tineye.com", documentation="https://services.tineye.com/developers/tineyeapi/",
        reliability="high", capabilities=("pages showing copies of the image", "first crawl date", "backlinks"),
        legal="Copies of the same image — not face recognition.",
        disclosure=Disclosure(("api.tineye.com",), "the public URL of the photo"), auto_run=False,
    )

    @transform(inputs=("image",), outputs=("website",), title="TinEye: pages showing this photo", priority=0.4,
               accepts=lambda self, e, info: bool(e.get("example_url")) and not e.get("flat"))
    async def search(self, entity, ctx):
        (key,) = ctx.require("TINEYE_API_KEY")
        url = str(entity.get("example_url"))
        resp = await ctx.get(API, params={"image_url": url, "limit": 30, "backlink_limit": 3},
                             headers={"X-API-KEY": key}, cache_ttl=7 * 86_400)
        matches = ((ctx.json(ctx.expect(resp)) or {}).get("results") or {}).get("matches") or []
        if not matches:
            raise NotFound("No copies of this image on the web known to TinEye")
        for m in matches:
            for bl in (m.get("backlinks") or [])[:2]:
                page = canonical_url(str(bl.get("backlink") or ""))
                if not page:
                    continue
                src = ctx.out.source(page, title="TinEye match", request="public photo URL")
                w = ctx.out.entity("website", page, label=url_host(page), source=src,
                                   props={"url": page, "host": url_host(page), "category": "image",
                                          "published": bl.get("crawl_date")})
                ctx.out.link(entity.key, w, "APPEARS_ON", source=src, confidence=0.9, evidence="Copy of this image on the page")
