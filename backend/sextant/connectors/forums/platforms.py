"""Forums & communities with public user APIs: Hacker News, Lobsters, Lemmy (lemmy.world)."""

from __future__ import annotations

from urllib.parse import quote

from ...sdk import RateLimit
from .._common import html_bio
from .._platform_api import Parsed, PlatformSpec, jbody, make_platform_connector, ts


def parse_hackernews(resp, username) -> Parsed:
    u = jbody(resp)
    if resp.status == 200 and u is None:
        return "not_found", None, "Hacker News usernames are case-sensitive"
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    bio, links = html_bio(u.get("about"))
    return "found", {
        "username": u.get("id"), "bio": bio, "links": links, "created_at": ts(u.get("created")),
        "profile_url": f"https://news.ycombinator.com/user?id={quote(str(u.get('id') or username))}",
        "extra": {"karma": u.get("karma")},
    }, ""


def parse_lobsters(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    bio, links = html_bio(u.get("about"))
    if u.get("github_username"):
        links.append(f"https://github.com/{u['github_username']}")
    if u.get("mastodon_username") and u.get("mastodon_instance"):
        links.append(f"https://{u['mastodon_instance']}/@{u['mastodon_username']}")
    avatar = u.get("avatar_url")
    return "found", {
        "username": u.get("username"), "bio": bio, "links": links, "created_at": u.get("created_at"),
        "avatar_url": f"https://lobste.rs{avatar}" if avatar and avatar.startswith("/") else avatar,
        "profile_url": f"https://lobste.rs/~{u.get('username') or username}",
        "extra": {"invited_by": u.get("invited_by_user"), "karma": u.get("karma")},
    }, ""


def parse_lemmy(resp, username) -> Parsed:
    data = jbody(resp)
    if resp.status in (400, 404) and isinstance(data, dict) and "couldnt_find" in str(data.get("error", "")):
        return "not_found", None, "Checked on lemmy.world (federated users from other instances are not covered)"
    pv = (data or {}).get("person_view") if isinstance(data, dict) else None
    if resp.status != 200 or not isinstance(pv, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    p = pv.get("person") or {}
    bio, links = html_bio(p.get("bio"))
    return "found", {
        "username": p.get("name"), "display_name": p.get("display_name"), "bio": bio or p.get("bio"), "links": links,
        "created_at": p.get("published"), "avatar_url": p.get("avatar"), "profile_url": p.get("actor_id"),
        "extra": {"posts": (pv.get("counts") or {}).get("post_count"),
                  "comments": (pv.get("counts") or {}).get("comment_count"), "bot": p.get("bot_account")},
    }, ""


HACKERNEWS = make_platform_connector(PlatformSpec(
    "hackernews", "hackernews", "Hacker News", "forums", "hacker-news.firebaseio.com",
    "https://hacker-news.firebaseio.com/v0/user/{u}.json", parse_hackernews, "https://news.ycombinator.com",
    "https://github.com/HackerNews/API", r"^[A-Za-z0-9_-]{2,15}$",
    capabilities=("account existence", "about text and links", "creation date", "karma")))
LOBSTERS = make_platform_connector(PlatformSpec(
    "lobsters", "lobsters", "Lobsters", "forums", "lobste.rs", "https://lobste.rs/~{u}.json", parse_lobsters,
    "https://lobste.rs", "https://lobste.rs/about", r"^[A-Za-z0-9_-]{1,50}$", rate=RateLimit(0.5, 1),
    capabilities=("account existence", "about text", "linked GitHub / Mastodon", "creation date")))
LEMMY = make_platform_connector(PlatformSpec(
    "lemmy", "lemmy", "Lemmy (lemmy.world)", "forums", "lemmy.world", "https://lemmy.world/api/v3/user?username={u}&limit=1",
    parse_lemmy, "https://lemmy.world", "https://join-lemmy.org/api/", r"^[A-Za-z0-9_]{3,30}$",
    capabilities=("account existence", "display name", "bio", "creation date")))
