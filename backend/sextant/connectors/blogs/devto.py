"""DEV Community — public user API."""

from __future__ import annotations

from .._platform_api import Parsed, PlatformSpec, jbody, make_platform_connector


def parse_devto(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    links = []
    if u.get("github_username"):
        links.append(f"https://github.com/{u['github_username']}")
    if u.get("twitter_username"):
        links.append(f"https://x.com/{u['twitter_username']}")
    return "found", {
        "username": u.get("username"), "display_name": u.get("name"), "bio": u.get("summary"),
        "location": u.get("location"), "website": u.get("website_url"), "created_at": u.get("joined_at"),
        "avatar_url": u.get("profile_image"), "profile_url": f"https://dev.to/{u.get('username') or username}",
        "links": links, "user_id": str(u.get("id") or ""),
    }, ""


DEVTO = make_platform_connector(PlatformSpec(
    "devto", "devto", "DEV Community", "blogs", "dev.to", "https://dev.to/api/users/by_username?url={u}", parse_devto,
    "https://dev.to", "https://developers.forem.com/api", r"^[A-Za-z0-9_]{2,30}$",
    capabilities=("account existence", "name", "summary", "location", "website", "linked GitHub/X", "join date")))
