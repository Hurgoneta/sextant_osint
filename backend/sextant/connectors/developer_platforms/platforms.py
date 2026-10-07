"""Developer platforms with public "user by handle" APIs: GitLab, Codeberg, Docker Hub, crates.io, Hugging Face."""

from __future__ import annotations

from ...sdk import RateLimit
from .._platform_api import Parsed, PlatformSpec, jbody, make_platform_connector


def parse_gitlab(resp, username) -> Parsed:
    data = jbody(resp)
    if resp.status != 200 or not isinstance(data, list):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    if not data:
        return "not_found", None, ""
    u = data[0]
    return "found", {
        "username": u.get("username"), "display_name": u.get("name"), "profile_url": u.get("web_url"),
        "avatar_url": u.get("avatar_url"), "emails": [u["public_email"]] if u.get("public_email") else [],
        "user_id": str(u.get("id") or ""), "extra": {"state": u.get("state")},
    }, ""


def parse_codeberg(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    return "found", {
        "username": u.get("login"), "display_name": u.get("full_name"), "profile_url": u.get("html_url"),
        "avatar_url": u.get("avatar_url"), "location": u.get("location"), "website": u.get("website"),
        "bio": u.get("description"), "created_at": u.get("created"), "user_id": str(u.get("id") or ""),
        "emails": [u["email"]] if u.get("email") else [],
    }, ""


def parse_dockerhub(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    return "found", {
        "username": u.get("username") or u.get("orgname") or username, "display_name": u.get("full_name"),
        "location": u.get("location"), "company": u.get("company"), "website": u.get("profile_url"),
        "created_at": u.get("date_joined"), "profile_url": f"https://hub.docker.com/u/{username}",
        "extra": {"type": u.get("type")},
    }, ""


def parse_crates(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    data = jbody(resp)
    u = (data or {}).get("user") if isinstance(data, dict) else None
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    return "found", {
        "username": u.get("login"), "display_name": u.get("name"), "avatar_url": u.get("avatar"),
        "profile_url": f"https://crates.io/users/{u.get('login')}", "links": [u["url"]] if u.get("url") else [],
        "verified_links": [u["url"]] if u.get("url") and u.get("github_username_matches") else [],
        "user_id": str(u.get("id") or ""), "created_at": u.get("created_at"),
    }, ""


def parse_huggingface(resp, username) -> Parsed:
    if resp.status == 404:
        return "not_found", None, ""
    u = jbody(resp)
    if resp.status != 200 or not isinstance(u, dict):
        return "unknown", None, f"Unexpected response (HTTP {resp.status})"
    orgs = [o.get("fullname") or o.get("name") for o in u.get("orgs") or [] if isinstance(o, dict)][:10]
    return "found", {
        "username": u.get("user") or username, "display_name": u.get("fullname"), "bio": u.get("details"),
        "avatar_url": u.get("avatarUrl"), "profile_url": f"https://huggingface.co/{u.get('user') or username}",
        "created_at": u.get("createdAt"), "followers": u.get("numFollowers"), "user_id": u.get("_id"),
        "extra": {"models": u.get("numModels"), "datasets": u.get("numDatasets"), "spaces": u.get("numSpaces"),
                  "organizations": orgs},
    }, ""


GITLAB = make_platform_connector(PlatformSpec(
    "gitlab", "gitlab", "GitLab", "developer_platforms", "gitlab.com", "https://gitlab.com/api/v4/users?username={u}",
    parse_gitlab, "https://gitlab.com", "https://docs.gitlab.com/api/users/", r"^[A-Za-z0-9_.-]{2,255}$",
    capabilities=("account existence", "display name", "public email", "profile photo")))
CODEBERG = make_platform_connector(PlatformSpec(
    "codeberg", "codeberg", "Codeberg", "developer_platforms", "codeberg.org", "https://codeberg.org/api/v1/users/{u}",
    parse_codeberg, "https://codeberg.org", "https://codeberg.org/api/swagger", r"^[A-Za-z0-9_.-]{1,40}$",
    capabilities=("account existence", "display name", "email (if public)", "location", "website", "creation date")))
DOCKERHUB = make_platform_connector(PlatformSpec(
    "dockerhub", "dockerhub", "Docker Hub", "developer_platforms", "hub.docker.com", "https://hub.docker.com/v2/users/{u}/",
    parse_dockerhub, "https://hub.docker.com", "https://docs.docker.com/reference/api/hub/latest/", r"^[a-z0-9]{4,30}$",
    lowercase=True, capabilities=("account existence", "full name", "company", "location", "website", "join date")))
CRATES = make_platform_connector(PlatformSpec(
    "crates", "crates", "crates.io", "developer_platforms", "crates.io", "https://crates.io/api/v1/users/{u}",
    parse_crates, "https://crates.io", "https://crates.io/data-access", r"^[A-Za-z0-9_-]{1,39}$",
    rate=RateLimit(1.0, 1, "1 request/second per crates.io crawler policy"),
    capabilities=("account existence", "display name", "linked GitHub profile (verified)", "profile photo")))
HUGGINGFACE = make_platform_connector(PlatformSpec(
    "huggingface", "huggingface", "Hugging Face", "developer_platforms", "huggingface.co",
    "https://huggingface.co/api/users/{u}/overview", parse_huggingface, "https://huggingface.co",
    "https://huggingface.co/docs/hub/api", r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,41}$",
    capabilities=("account existence", "full name", "bio", "organizations", "profile photo", "creation date")))
