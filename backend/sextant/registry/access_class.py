"""Access classes: one vocabulary for "what does it take to use this source", across built-in connectors, catalog-only
providers and your own sources. Searchable and filterable in Data Sources.

A source can carry several classes (a provider with a free tier and enterprise plans is both); the first is its primary
class. ``NOT_VERIFIED`` is added whenever nobody has checked the metadata against the provider.
"""

from __future__ import annotations

ACCESS_CLASSES = {
    "FREE_PUBLIC": "Free public",
    "FREE_API": "Free API",
    "FREE_TIER": "Free tier",
    "PAID_API": "Paid API (per query)",
    "SUBSCRIPTION": "Subscription",
    "ENTERPRISE": "Enterprise",
    "MANUAL": "Manual",
    "REFERENCE_ONLY": "Reference only",
    "UNAVAILABLE": "Unavailable",
    "NOT_VERIFIED": "Not verified",
}
ACCESS_MODELS = {"free": "Free", "free_tier": "Free tier", "trial": "Trial", "per_query": "Per query / credits",
                 "subscription": "Subscription", "enterprise": "Enterprise contract", "manual": "Manual (web application)"}
_MODEL_CLASS = {"free": "FREE_PUBLIC", "free_tier": "FREE_TIER", "per_query": "PAID_API", "subscription": "SUBSCRIPTION",
                "enterprise": "ENTERPRISE", "manual": "MANUAL"}
_MANIFEST_CLASS = {"free_key": "FREE_API", "restricted": "FREE_API", "metered": "PAID_API", "paid": "SUBSCRIPTION",
                   "enterprise": "ENTERPRISE", "manual": "MANUAL", "local": "FREE_PUBLIC"}
_USER_CLASS = {"free_web": "FREE_PUBLIC", "free_api": "FREE_API", "free_download": "FREE_PUBLIC", "registration": "FREE_API",
               "on_request": "MANUAL", "restricted": "MANUAL", "unknown": "NOT_VERIFIED"}


def _uniq(items: list[str]) -> list[str]:
    out = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


def for_connector(manifest, status: str, commercial: dict | None = None) -> list[str]:
    m = manifest
    out: list[str] = []
    if status in ("retired", "excluded"):
        out.append("UNAVAILABLE")
    if status == "manual" or m.access == "manual":
        out.append("MANUAL")
    for model in (commercial or {}).get("access_models") or []:
        out.append(_MODEL_CLASS.get(model, ""))
    if m.access == "free":
        out.append("FREE_API" if m.access_method == "api" and m.official_api else "FREE_PUBLIC")
    else:
        out.append(_MANIFEST_CLASS.get(m.access, "NOT_VERIFIED"))
    if commercial is not None and not commercial.get("last_verified"):
        out.append("NOT_VERIFIED")
    return _uniq(out) or ["NOT_VERIFIED"]


def for_user_source(s: dict) -> list[str]:
    out: list[str] = []
    if s.get("review") in ("unavailable", "excluded"):
        out.append("UNAVAILABLE")
    if s.get("source_type") == "reference":
        out.append("REFERENCE_ONLY")
    for model in s.get("access_models") or []:
        out.append(_MODEL_CLASS.get(model, ""))
    access = s.get("access") or "unknown"
    if access == "paid":
        out.append("PAID_API" if s.get("source_type") in ("official_api", "public_api") else "SUBSCRIPTION")
    else:
        out.append(_USER_CLASS.get(access, "NOT_VERIFIED"))
    if s.get("source_type") in ("manual", "dataset", "local"):
        out.append("MANUAL")
    if s.get("review", "not_verified") == "not_verified":
        out.append("NOT_VERIFIED")
    return _uniq(out) or ["NOT_VERIFIED"]


def payload(classes: list[str]) -> dict:
    return {"access_class": classes[0], "access_class_title": ACCESS_CLASSES[classes[0]], "access_classes": classes,
            "access_class_titles": [ACCESS_CLASSES[c] for c in classes]}


__all__ = ["ACCESS_CLASSES", "ACCESS_MODELS", "for_connector", "for_user_source", "payload"]
