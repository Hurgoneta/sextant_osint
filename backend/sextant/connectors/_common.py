"""Helpers shared by connectors: turning public profiles and records into graph fragments.

Everything here only *describes* findings through the TransformResult builder; provenance (the
source id) is mandatory on every call.
"""

from __future__ import annotations

from typing import Any

from ..registry.platforms import get as platform_get
from ..registry.platforms import parse_profile_url, platform_for_host
from ..sdk.context import TransformContext
from ..sdk.http import FetchError
from ..sdk.result import TransformResult
from ..sdk.transform import EntityView
from ..util.extract import extract_emails, extract_urls, parse_html
from ..util.imagehash import MAX_IMAGE_BYTES, PIL_AVAILABLE, fingerprint
from ..util.normalize import (
    canonical_url, location_tokens, name_tokens, normalize_email, normalize_name, registered_domain, url_host,
)

PROFILE_FIELDS = ("display_name", "bio", "location", "website", "company", "created_at", "avatar_url", "profile_url",
                  "followers", "user_id", "category")
FP_KEYS = ("dhash", "phash", "dhash_flip", "phash_flip", "flat")

# link shorteners / generic hosts: a link to them says nothing about who owns what
IGNORED_LINK_DOMAINS = frozenset({
    "bit.ly", "t.co", "goo.gl", "tinyurl.com", "lnkd.in", "youtu.be", "google.com", "ow.ly", "buff.ly", "invalid.local",
    "linktr.ee", "gravatar.com", "wikipedia.org", "creativecommons.org", "w3.org", "schema.org",
})


def person_names(entity: EntityView, info=None, min_tokens: int = 2) -> list[str]:
    """Names to search for a person entity (target or a person described by a record)."""
    names = [str(n) for n in [*entity.values("full_name"), *entity.all_list("aliases")] if n]
    if not names and entity.type == "name":
        names = [entity.label]
    if not names and entity.type == "person" and entity.label and not entity.label.startswith("Unknown"):
        names = [entity.label]
    out = []
    for n in names:
        nn = normalize_name(n)
        if nn and len(name_tokens(nn)) >= min_tokens and nn not in out:
            out.append(nn)
    return out


def accepts_named_person(_self: Any, entity: EntityView, info) -> bool:
    return bool(person_names(entity, info))


async def avatar_fingerprint(ctx: TransformContext, url: str | None) -> dict | None:
    """Downloads a public profile photo (size-capped) → perceptual hashes + tiny re-encoded thumbnail.
    The photo itself is not kept."""
    if not url or not PIL_AVAILABLE or not str(url).startswith("https://"):
        return None
    try:
        resp = await ctx.get(str(url), max_bytes=MAX_IMAGE_BYTES, cache_ttl=7 * 86_400, retries=1)
    except FetchError:
        return None
    if resp.status != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return None
    return fingerprint(resp.content)


def html_bio(html: str | None) -> tuple[str, list[str]]:
    if not html:
        return "", []
    page = parse_html(html, "https://invalid.local/")
    return page.text, [lk for lk in page.links if not lk.startswith("https://invalid.local")]


def account_key(platform: str, username: str) -> str:
    return f"{platform}:{username}"


def add_account(out: TransformResult, src: str, *, platform: str, username: str, profile: dict[str, Any],
                link_from: str | None = None, found_by: str = "username", platform_title: str | None = None,
                link_confidence: float = 1.0) -> str | None:
    """Account node + every public detail the profile exposes (emails, links, location, employer, photo).

    ``found_by`` documents *how* the account was found (username lookup, exact email lookup, name search…);
    ``link_from`` is the identifier that led here (linked with ``USES``)."""
    p = platform_get(platform)
    title = platform_title or (p.name if p else platform)
    props: dict[str, Any] = {"platform": platform, "platform_title": title, "username": username,
                             "found_by": found_by}
    if p and p.category:
        props["category"] = p.category
    for k in PROFILE_FIELDS:
        v = profile.get(k)
        if v not in (None, "", [], {}):
            props[k] = v
    bio = str(profile.get("bio") or "")
    emails = [e for e in (normalize_email(str(x)) for x in profile.get("emails") or []) if e and "noreply" not in e]
    emails += [e for e in extract_emails(bio) if e not in emails]
    links = [str(x) for x in profile.get("links") or [] if x]
    links += [u for u in extract_urls(bio) if u not in links]
    verified = [str(x) for x in profile.get("verified_links") or [] if x]
    if emails:
        props["emails"] = emails
    if links:
        props["links"] = links
    if verified:
        props["verified_links"] = verified
    if profile.get("extra"):
        props["extra"] = profile["extra"]
    fp = profile.get("avatar_fp")
    if fp:
        props["avatar_fp"] = {k: fp.get(k) for k in FP_KEYS}
        props["avatar_thumb"] = fp.get("thumb")
    url = profile.get("profile_url")
    acc = out.entity("account", account_key(platform, username), label=f"{title}: {username}", source=src, props=props,
                     evidence=f"Public profile on {title}" + (f" ({url})" if url else ""))
    if acc is None:
        return None
    if link_from:
        out.link(acc, link_from, "USES", source=src, confidence=link_confidence,
                 evidence=f"Found on {title} by {found_by.replace('_', ' ')}")
    for email in emails:
        e = out.entity("email", email, source=src, evidence=f"Shown on {title} profile")
        out.link(acc, e, "USES", source=src, evidence="Email displayed on the public profile")
    for raw in [profile.get("website"), *links]:
        if raw:
            add_link_target(out, acc, str(raw), src, verified=str(raw) in verified)
    loc = profile.get("location")
    if loc and location_tokens(str(loc)):
        le = out.entity("location", str(loc), source=src, props={"text": str(loc)})
        out.link(acc, le, "LOCATED_IN", source=src, evidence="Location on the public profile")
    company = str(profile.get("company") or "").strip().lstrip("@")
    if company and len(company) > 1:
        org = out.entity("organization", f"name:{company.casefold()}", label=company, source=src, props={"name": company})
        out.link(acc, org, "WORKS_FOR", source=src, evidence="Company/employer on the public profile")
    dn = profile.get("display_name")
    if dn and len(name_tokens(str(dn))) >= 2:
        ne = out.entity("name", str(dn), source=src)
        out.link(acc, ne, "HAS_NAME", source=src, evidence="Display name on the public profile")
    if fp and not fp.get("flat") and fp.get("phash"):
        img = out.entity("image", fp["phash"], label="Profile photo", source=src,
                         props={**{k: fp.get(k) for k in FP_KEYS}, "thumb": fp.get("thumb"),
                                "example_url": profile.get("avatar_url")})
        out.link(acc, img, "HAS_IMAGE", source=src, evidence="Profile photo (perceptual hash)")
    return acc


def add_link_target(out: TransformResult, from_key: str, raw: str, src: str, *, verified: bool = False) -> str | None:
    """A link on a profile/page becomes an account (known profile URL) or a domain."""
    url = raw if "://" in raw else f"https://{raw}"
    c = canonical_url(url)
    if not c:
        return None
    why = "Verified link (rel=me / cryptographic proof)" if verified else "Linked from the profile"
    prof = parse_profile_url(c)
    if prof:
        platform, uname = prof
        key = out.entity("account", account_key(platform.id, uname), label=f"{platform.name}: {uname}", source=src,
                         props={"platform": platform.id, "platform_title": platform.name, "username": uname,
                                "profile_url": c, "found_by": "link"}, evidence=why)
        if key and key != from_key:
            out.link(from_key, key, "LINKED_TO", source=src, evidence=why)
            if "/" not in uname:
                u = out.entity("username", uname, source=src, props={"username": uname})
                out.link(key, u, "USES", source=src, evidence=f"Username of the linked {platform.name} profile")
        return key
    host = url_host(c)
    if platform_for_host(host):
        return None            # a non-profile page on a big platform says nothing about owning that domain
    rd = registered_domain(host)
    if not rd or rd in IGNORED_LINK_DOMAINS:
        return None
    dom = out.entity("domain", rd, source=src, evidence=why)
    out.link(from_key, dom, "LINKED_TO", source=src, evidence=why)
    return dom


def add_record(out: TransformResult, src: str, *, key: str, title: str, record_type: str, jurisdiction: str,
               authority: str = "", official_url: str | None = None, props: dict | None = None,
               matched_name: str | None = None, name_match: str | None = None, query_name: str | None = None,
               link_from: str | None = None, link_type: str = "ASSOCIATED_WITH") -> str | None:
    """A public record. A record found by a *name* search links only to the name string it carries
    (HAS_NAME) — never to the person that was searched. Sharing a name with the target is shown as
    exactly that; deciding it is the same person is the job of resolution and the investigator.

    ``link_from`` is for lookups by a hard identifier (company number, email…), where the link is real."""
    p = {"record_type": record_type, "jurisdiction": jurisdiction, "authority": authority, "official_url": official_url,
         "title": title}
    if matched_name:
        p["matched_name"] = matched_name
        p["name_match"] = name_match
    p.update({k: v for k, v in (props or {}).items() if v not in (None, "", [], {})})
    rec = out.entity("public_record", key, label=title[:200], source=src, props=p,
                     evidence=f"{record_type.replace('_', ' ').capitalize()} published by {authority or jurisdiction}")
    if rec is None:
        return None
    if matched_name:
        ne = out.entity("name", matched_name, source=src)
        out.link(rec, ne, "HAS_NAME", source=src, evidence="Name as written on the record")
        if query_name:
            qn = out.entity("name", query_name, source=src)
            if qn and ne and qn != ne:
                out.link(qn, ne, "ASSOCIATED_WITH", source=src, confidence=0.5,
                         evidence=f"Name search for “{query_name}” matched “{matched_name}” ({name_match or 'similar'})")
    if link_from:
        out.link(link_from, rec, link_type, source=src, evidence="Returned by the source for this query")
    return rec
