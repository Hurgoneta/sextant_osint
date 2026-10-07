"""Public pages of a website: home page, /about, /contact, robots.txt, sitemap.xml, security.txt.

Pages are parsed as text with the standard-library tokenizer — nothing is rendered or executed, no
linked resources are fetched, documents are never downloaded. Only links that point at known profile
platforms, rel="me" links, emails and phone numbers become entities.
"""

from __future__ import annotations

import re

from ...registry.platforms import parse_profile_url
from ...sdk import Connector, Disclosure, Manifest, RateLimit, register, transform
from ...sdk.errors import SourceUnavailable
from ...sdk.http import FetchError
from ...util.extract import extract_emails, extract_phones, parse_html
from ...util.normalize import normalize_email, parse_phone, registered_domain, url_host
from .._common import add_link_target
from ..dns.dns import is_apex

MAX_PAGE = 1_500_000
EXTRA_PAGES = ("/about", "/contact", "/impressum", "/kontakt", "/about-us", "/contact-us")
TECH_HINTS = (("wp-content", "WordPress"), ("cdn.shopify.com", "Shopify"), ("squarespace", "Squarespace"),
              ("static.wixstatic.com", "Wix"), ("ghost.io", "Ghost"), ("___gatsby", "Gatsby"), ("/_next/", "Next.js"),
              ("data-hugo", "Hugo"), ("jekyll", "Jekyll"), ("drupal", "Drupal"), ("joomla", "Joomla"),
              ("googletagmanager.com", "Google Tag Manager"), ("google-analytics.com", "Google Analytics"),
              ("plausible.io", "Plausible Analytics"), ("cloudflareinsights", "Cloudflare Web Analytics"),
              ("hs-scripts.com", "HubSpot"), ("cdn.jsdelivr.net", "jsDelivr CDN"), ("webflow", "Webflow"),
              ("netlify", "Netlify"), ("vercel", "Vercel"), ("github.io", "GitHub Pages"))
HEADER_HINTS = (("server", None), ("x-powered-by", None), ("x-generator", None), ("cf-ray", "Cloudflare"),
                ("x-vercel-id", "Vercel"), ("x-nf-request-id", "Netlify"), ("x-github-request-id", "GitHub Pages"),
                ("x-amz-cf-id", "Amazon CloudFront"), ("x-served-by", None))


async def fetch_page(ctx, url: str):
    try:
        resp = await ctx.get(url, max_bytes=MAX_PAGE, retries=1, headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
    except FetchError:
        return None
    ctype = resp.headers.get("content-type", "")
    if resp.status != 200 or not any(t in ctype for t in ("text/", "xml", "html")):
        return None
    return resp


@register
class Website(Connector):
    manifest = Manifest(
        name="website", title="Website (public pages)", category="websites",
        description="Reads a site's home/about/contact pages, robots.txt, sitemap.xml and security.txt for public "
                    "contact details, rel=me links and links to profiles on known platforms.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://www.rfc-editor.org/rfc/rfc9116",
        reliability="medium", official_api=False, access_method="web_page",
        capabilities=("emails and phone numbers shown on the site", "rel=me and social profile links", "site title/author",
                      "robots.txt / sitemap / security.txt"),
        disclosure=Disclosure(("the website itself",), "ordinary page requests to the site", "public"),
    )

    @transform(inputs=("domain",), outputs=("email", "phone", "account", "domain", "name"),
               title="Website: contacts and profile links on public pages", priority=0.7, estimated_requests=6,
               accepts=lambda self, e, info: is_apex(e.value) or bool(e.get("site_title")))
    async def pages(self, entity, ctx):
        home = await fetch_page(ctx, f"https://{entity.value}/") or await fetch_page(ctx, f"http://{entity.value}/")
        if home is None:
            raise SourceUnavailable("The website did not answer with a readable page")
        pages = [home]
        for path in EXTRA_PAGES[:4]:
            p = await fetch_page(ctx, f"https://{entity.value}{path}")
            if p is not None and p.url.rstrip("/") != home.url.rstrip("/"):
                pages.append(p)
        region = ctx.info.default_region or (ctx.info.countries[0] if ctx.info.countries else None)
        own = entity.is_seed or entity.status == "confirmed"
        usernames = {u.casefold() for u in ctx.info.seeds.get("username", ())}
        seen_links: set[str] = set()
        for resp in pages:
            page = parse_html(resp.text, resp.url)
            src = ctx.out.source(resp.url, title=page.title or resp.url, kind="web_page", request="page request")
            if resp is home:
                ctx.out.props(entity.key, {"site_title": page.title or None, "author_meta": page.meta.get("author"),
                                           "description": page.meta.get("description") or page.meta.get("og:description")},
                              source=src)
                author = page.meta.get("author")
                if author and len(author.split()) >= 2 and len(author) < 60:
                    nk = ctx.out.entity("name", author, source=src, evidence="<meta name=author>")
                    ctx.out.link(entity.key, nk, "ASSOCIATED_WITH", source=src, confidence=0.7,
                                 evidence="Author named in the site's metadata")
            text = f"{page.title} {page.text}"
            emails = set(extract_emails(text)) | {e for e in (normalize_email(m) for m in page.mailto) if e}
            phones = set(extract_phones(text, region)) | {p.e164 for p in (parse_phone(t, region) for t in page.tel) if p}
            for email in sorted(emails)[:15]:
                ek = ctx.out.entity("email", email, source=src, evidence="Shown on the website")
                ctx.out.link(entity.key, ek, "USES", source=src, evidence=f"Email shown on {resp.url}")
            for phone in sorted(phones)[:8]:
                pk = ctx.out.entity("phone", phone, source=src, evidence="Shown on the website")
                ctx.out.link(entity.key, pk, "USES", source=src, evidence=f"Phone number shown on {resp.url}")
            me = set(page.me_links)
            for link in page.links:
                if link in seen_links or url_host(link) == entity.value or len(seen_links) >= 40:
                    continue
                prof = parse_profile_url(link)
                # On a target-owned site every profile link counts; elsewhere only rel=me or the target's handles,
                # otherwise a company blog would flood the graph with the company's social accounts.
                if prof and (own or link in me or prof[1].casefold() in usernames):
                    seen_links.add(link)
                    add_link_target(ctx.out, entity.key, link, src, verified=link in me)
                elif link in me:
                    seen_links.add(link)
                    add_link_target(ctx.out, entity.key, link, src, verified=True)
        for key, path in (("robots", "/robots.txt"), ("security_txt", "/.well-known/security.txt")):
            resp = await fetch_page(ctx, f"https://{entity.value}{path}")
            if resp is None or resp.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                continue
            src = ctx.out.source(resp.url, title=path, kind="web_page")
            if key == "robots":
                ctx.out.prop(entity.key, "robots_disallow", re.findall(r"(?im)^disallow:\s*(\S+)", resp.text)[:30], source=src)
            else:
                for line in resp.text.splitlines():
                    if line.lower().startswith("contact:"):
                        for e in extract_emails(line):
                            ek = ctx.out.entity("email", e, source=src)
                            ctx.out.link(entity.key, ek, "USES", source=src, evidence="security.txt contact")


@register
class TechFingerprint(Connector):
    manifest = Manifest(
        name="tech_fingerprint", title="Technology fingerprint", category="technology_detection",
        description="Identifies site technologies (CMS, hosting/CDN, analytics) from the home page markup and response "
                    "headers. Shared analytics/verification IDs link sites run by the same owner.",
        access="free", rate_limit=RateLimit(1.0, 2), website="https://developer.mozilla.org/docs/Web/HTTP/Headers",
        reliability="medium", official_api=False, access_method="web_page",
        capabilities=("CMS / framework", "hosting / CDN", "analytics", "Google Analytics / Tag Manager IDs"),
        disclosure=Disclosure(("the website itself",), "one home-page request", "public"),
    )

    @transform(inputs=("domain",), outputs=(), title="Technologies used by the website", priority=0.3,
               accepts=lambda self, e, info: is_apex(e.value))
    async def fingerprint(self, entity, ctx):
        resp = await fetch_page(ctx, f"https://{entity.value}/")
        if resp is None:
            raise SourceUnavailable("The website did not answer with a readable page")
        html = resp.text[:400_000].lower()
        techs = sorted({name for hint, name in TECH_HINTS if hint in html})
        page = parse_html(resp.text, resp.url)
        if page.meta.get("generator"):
            techs.insert(0, page.meta["generator"][:80])
        for h, name in HEADER_HINTS:
            v = resp.headers.get(h)
            if v:
                techs.append(name or f"{h}: {v[:60]}")
        ids = sorted(set(re.findall(r"\b(UA-\d{4,10}-\d{1,4}|G-[A-Z0-9]{6,12}|GTM-[A-Z0-9]{4,10})\b", resp.text[:400_000])))
        src = ctx.out.source(resp.url, title="Home page (markup and headers)", kind="web_page")
        ctx.out.props(entity.key, {"technologies": sorted(set(techs)), "tracking_ids": ids}, source=src)


@register
class LocalDerivations(Connector):
    manifest = Manifest(
        name="derive", title="Local derivations", category="domains",
        description="Offline derivations: a web page's domain, a subdomain's registered domain. Nothing is sent anywhere.",
        access="local", rate_limit=RateLimit(100, 100), website="https://publicsuffix.org", reliability="high",
        official_api=False, access_method="local", capabilities=("domain of a URL", "registered domain of a subdomain"),
        disclosure=Disclosure((), "nothing — runs locally", "none"),
    )

    @transform(inputs=("website", "document"), outputs=("domain",), title="Domain of this page", priority=0.4,
               sends="nothing (local)")
    async def url_domain(self, entity, ctx):
        host = url_host(str(entity.get("url") or entity.value))
        rd = registered_domain(host)
        if not rd:
            return
        src = ctx.out.source(None, title="Local URL parsing", kind="local")
        d = ctx.out.entity("domain", rd, source=src)
        ctx.out.link(entity.key, d, "REFERENCED_BY", source=src, confidence=0.5, evidence="Page hosted on this domain")

    @transform(inputs=("domain",), outputs=("domain",), title="Registered domain of a subdomain", priority=0.3,
               accepts=lambda self, e, info: not is_apex(e.value), sends="nothing (local)")
    async def apex(self, entity, ctx):
        rd = registered_domain(entity.value)
        if rd and rd != entity.value:
            src = ctx.out.source(None, title="Public Suffix List (offline)", kind="local")
            d = ctx.out.entity("domain", rd, source=src)
            ctx.out.link(d, entity.key, "HAS_SUBDOMAIN", source=src, evidence="DNS hierarchy")

