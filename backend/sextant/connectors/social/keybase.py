"""Keybase — public lookup API including cryptographic identity proofs (strong cross-platform evidence)."""

from __future__ import annotations

from ...sdk import RateLimit
from .._platform_api import Parsed, PlatformSpec, jbody, make_platform_connector, ts


def parse_keybase(resp, username) -> Parsed:
    data = jbody(resp)
    if resp.status != 200 or not isinstance(data, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    them = data.get("them") or []
    if (data.get("status") or {}).get("code") == 100 or not them or them[0] is None:
        return "not_found", None, ""
    u = them[0]
    prof = u.get("profile") or {}
    proofs = (u.get("proofs_summary") or {}).get("all") or []
    verified = [p["service_url"] for p in proofs if p.get("service_url")]
    pic = ((u.get("pictures") or {}).get("primary") or {}).get("url")
    basics = u.get("basics") or {}
    return "found", {
        "username": basics.get("username_cased") or username, "display_name": prof.get("full_name"), "bio": prof.get("bio"),
        "location": prof.get("location"), "created_at": ts(basics.get("ctime")), "avatar_url": pic,
        "profile_url": f"https://keybase.io/{username}", "links": verified, "verified_links": verified,
        "extra": {"proofs": [f"{p.get('proof_type')}: {p.get('nametag')}" for p in proofs]},
    }, ""


KEYBASE = make_platform_connector(PlatformSpec(
    "keybase", "keybase", "Keybase", "social", "keybase.io",
    "https://keybase.io/_/api/1.0/user/lookup.json?usernames={u}&fields=basics,profile,proofs_summary,pictures",
    parse_keybase, "https://keybase.io", "https://keybase.io/docs/api/1.0/call/user/lookup", r"^[A-Za-z0-9_]{2,16}$",
    lowercase=True, rate=RateLimit(0.5, 2),
    capabilities=("account existence", "full name", "bio", "location", "cryptographic proofs of other accounts/domains")))
