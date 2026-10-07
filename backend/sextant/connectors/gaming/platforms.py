"""Gaming platforms with public user APIs: Lichess, Chess.com."""

from __future__ import annotations

from ...sdk import RateLimit
from .._platform_api import Parsed, PlatformSpec, jbody, make_platform_connector, ts


def parse_lichess(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    prof = u.get("profile") or {}
    links = [x.strip() for x in str(prof.get("links") or "").splitlines() if x.strip()]
    real = prof.get("realName") or " ".join(x for x in (prof.get("firstName"), prof.get("lastName")) if x) or None
    return "found", {
        "username": u.get("username"), "display_name": real, "bio": prof.get("bio"), "location": prof.get("location"),
        "links": links, "created_at": ts(u.get("createdAt")), "profile_url": u.get("url"),
        "extra": {"disabled": u.get("disabled"), "flag": prof.get("flag") or prof.get("country")},
    }, ""


def parse_chesscom(resp, username) -> Parsed:
    if resp.status in (404, 410):
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    country = str(u.get("country") or "").rsplit("/", 1)[-1] or None
    return "found", {
        "username": u.get("username"), "display_name": u.get("name"), "location": u.get("location"),
        "created_at": ts(u.get("joined")), "avatar_url": u.get("avatar"), "profile_url": u.get("url"),
        "followers": u.get("followers"), "user_id": str(u.get("player_id") or ""), "extra": {"country": country},
    }, ""


LICHESS = make_platform_connector(PlatformSpec(
    "lichess", "lichess", "Lichess", "gaming", "lichess.org", "https://lichess.org/api/user/{u}", parse_lichess,
    "https://lichess.org", "https://lichess.org/api", r"^[A-Za-z0-9_-]{2,30}$", rate=RateLimit(0.5, 1),
    capabilities=("account existence", "real name (if set)", "bio", "location", "links", "creation date")))
CHESSCOM = make_platform_connector(PlatformSpec(
    "chesscom", "chesscom", "Chess.com", "gaming", "api.chess.com", "https://api.chess.com/pub/player/{u}",
    parse_chesscom, "https://www.chess.com", "https://www.chess.com/news/view/published-data-api",
    r"^[A-Za-z0-9_-]{3,25}$", lowercase=True,
    capabilities=("account existence", "name (if set)", "location", "country", "join date", "avatar")))
