"""Operational health of every source — so a connector never looks fine after the website changed.

Two kinds of evidence feed it, and both carry dates:

* **Probes** (``python -m sextant.tools.health_check``): each connector with an entry in
  ``data/health/probes.toml`` is run against a well-known public test identifier (GitHub's mascot, IANA's
  example domain, a large listed company…). Results go to ``data/health/_health.json`` (shipped snapshot)
  and to the local database.
* **Real runs**: after every transform run the engine records the outcome per connector (status and time
  only — never what was searched). ``Last successful query`` comes from here.

Statuses:

    operational              last outcomes succeeded (an answer, including "not found", is a success)
    degraded                 ≥30 % of the recent outcomes failed
    auth_required            needs an API key / account / approval that is not configured
    subscription_required    needs a paid subscription that is not configured
    temporarily_unavailable  the last 3+ outcomes were timeouts, rate limits or refusals
    deprecated               the provider retired or is retiring the service
    broken                   the last 3+ outcomes were errors (unexpected answers — the site probably changed)
    not_publicly_searchable  the platform forbids automated access or has no public lookup (manual links only)
    country_restricted       the source only answers clients from certain countries
    excluded                 excluded by policy (not a health state, shown for completeness)
    unknown                  never verified on this installation and no probe result
"""

from __future__ import annotations

import json
from pathlib import Path

from .connectors import REGISTRY, connector_status

DATA = Path(__file__).resolve().parents[1] / "data" / "health"

HEALTH_TITLES = {
    "operational": "Operational",
    "degraded": "Degraded",
    "auth_required": "Authentication required",
    "subscription_required": "Subscription required",
    "temporarily_unavailable": "Temporarily unavailable",
    "deprecated": "Deprecated",
    "broken": "Broken",
    "not_publicly_searchable": "Not publicly searchable",
    "country_restricted": "Country restricted",
    "excluded": "Excluded by policy",
    "unknown": "Unknown (not yet verified)",
}
SUCCESS = ("found", "empty", "not_found")
TRANSIENT = ("timeout", "rate_limited", "blocked")
ERRORS = ("error",)
RECENT = 20


def snapshot() -> dict:
    """Probe results shipped with the code (``tools/health_check``)."""
    try:
        return json.loads((DATA / "_health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def merge_record(record: dict | None, status: str, at: str) -> dict:
    """Adds one outcome to a connector's health record (kept in the database)."""
    r = dict(record or {})
    recent = list(r.get("recent") or [])
    recent.append([at, status])
    r["recent"] = recent[-RECENT:]
    r["runs"] = int(r.get("runs") or 0) + 1
    r["last_status"], r["last_run"] = status, at
    if status in SUCCESS:
        r["last_success"] = at
    elif status in TRANSIENT or status in ERRORS:
        r["last_failure"] = at
    return r


def _from_outcomes(outcomes: list[str]) -> str | None:
    outcomes = [s for s in outcomes if s in SUCCESS or s in TRANSIENT or s in ERRORS]
    if not outcomes:
        return None
    tail = outcomes[-3:]
    if len(tail) == 3 and all(s in ERRORS for s in tail):
        return "broken"
    if len(tail) == 3 and all(s in TRANSIENT or s in ERRORS for s in tail):
        return "temporarily_unavailable"
    recent = outcomes[-10:]
    failed = sum(1 for s in recent if s not in SUCCESS)
    if failed / len(recent) >= 0.3:
        return "degraded"
    return "operational" if outcomes[-1] in SUCCESS else "degraded"


def connector_health(name: str, cfg=None, record: dict | None = None, snap: dict | None = None) -> dict:
    """{status, title, note, last_verified, last_success, last_failure, runs, evidence} for one connector."""
    conn = REGISTRY[name]
    m = conn.manifest
    cstatus, note = connector_status(conn, cfg)
    snap = snapshot() if snap is None else snap
    probe = (snap.get("connectors") or {}).get(name) or {}
    record = record or {}
    last_success = max(filter(None, [record.get("last_success"), probe.get("checked_at") if probe.get("status") in SUCCESS else None]),
                       default=None)
    last_verified = max(filter(None, [record.get("last_run"), probe.get("checked_at")]), default=None)
    evidence = "probe" if probe else ""
    status = None
    if cstatus == "excluded":
        status = "excluded"
    elif cstatus == "retired" or m.status in ("retired", "sunsetting"):
        status = "deprecated"
        note = m.status_note or note
    elif cstatus == "manual":
        status = "not_publicly_searchable"
    elif cstatus == "catalog_only":
        status = "subscription_required" if m.access in ("paid", "enterprise", "metered") else "auth_required" \
            if m.access in ("restricted", "free_key") else "unknown"
        note = note or "No implementation: documented provider only"
    elif cstatus == "needs_credentials":
        status = "auth_required"
    elif cstatus == "needs_subscription":
        status = "subscription_required"
    if status is None:
        outcomes = [s for _, s in record.get("recent") or []]
        if probe.get("status") and (not record.get("last_run") or probe.get("checked_at", "") > record.get("last_run", "")):
            outcomes = [*outcomes, probe["status"]]
        status = _from_outcomes(outcomes)
        if record.get("recent"):
            evidence = "runs" + ("+probe" if probe else "")
        if status is None:
            status = "unknown"
            note = note or ("Not installed: " + note if cstatus == "not_installed" else "Never verified on this installation")
        if status in ("temporarily_unavailable", "broken") and "country_restricted" in m.tags:
            status = "country_restricted"
    return {"status": status, "title": HEALTH_TITLES[status], "note": note, "config_status": cstatus,
            "last_verified": last_verified, "last_success": last_success, "last_failure": record.get("last_failure"),
            "runs": int(record.get("runs") or 0), "probe": probe or None, "evidence": evidence}


def link_health(verification: dict | None) -> dict:
    """Health of a link-only source from the URL check (verify_catalog)."""
    if not verification:
        return {"status": "unknown", "title": HEALTH_TITLES["unknown"], "last_verified": None}
    result = verification.get("result")
    status = {"ok": "operational", "reachable_refuses_bots": "operational", "tls_problem": "degraded",
              "server_error": "temporarily_unavailable", "unreachable": "temporarily_unavailable",
              "dead": "broken"}.get(result, "unknown")
    return {"status": status, "title": HEALTH_TITLES[status], "last_verified": verification.get("checked_at"),
            "check": result}
