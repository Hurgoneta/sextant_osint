"""Source coverage accounting — what was searched, what was not, and why.

The platform never claims to have searched "the internet". For every investigation it reports, per
connector, whether it was queried and with what outcome, or why it was not: needs credentials,
needs a subscription, disabled, external tool not installed, not applicable to any entity in the
graph, manual only, catalog only, or skipped by budget/policy.
"""

from __future__ import annotations

from collections import defaultdict

from ..registry import connectors as REG
from ..sdk.manifest import CATEGORIES

OK_STATUSES = ("found", "empty", "not_found")
FAIL_STATUSES = ("blocked", "rate_limited", "error", "timeout")
DISCLAIMER = ("Coverage is based on the connectors currently configured and on legally accessible sources. "
              "A missing result never proves that information does not exist.")


class CoverageTracker:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))     # connector -> status -> count
        self.reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # connector -> skip reason -> count
        self.queued: dict[str, int] = defaultdict(int)
        self.running: dict[str, int] = defaultdict(int)
        self.notes: dict[str, str] = {}                  # connector -> why the engine stopped using it (back-off)

    # ------------------------------------------------------------------ recording
    def skip(self, connector: str, reason: str) -> None:
        self.reasons[connector][reason] += 1

    def enqueue(self, connector: str) -> None:
        self.queued[connector] += 1

    def start(self, connector: str) -> None:
        self.queued[connector] = max(0, self.queued[connector] - 1)
        self.running[connector] += 1

    def finish(self, connector: str, status: str) -> None:
        self.running[connector] = max(0, self.running[connector] - 1)
        self.runs[connector][status] += 1

    def drop_queued(self, connector: str) -> None:
        self.queued[connector] = max(0, self.queued[connector] - 1)

    def note(self, connector: str, text: str) -> None:
        self.notes[connector] = text

    # ------------------------------------------------------------------ views
    def connector_state(self, name: str, cfg, applicable: bool) -> dict:
        conn = REG.REGISTRY[name]
        status, note = REG.connector_status(conn, cfg)
        runs = dict(self.runs.get(name, {}))
        reasons = dict(self.reasons.get(name, {}))
        done = sum(runs.values())
        ok = sum(runs.get(s, 0) for s in OK_STATUSES)
        failed = sum(runs.get(s, 0) for s in FAIL_STATUSES)
        if done and ok:
            state = "queried"
        elif done and failed and not ok:
            state = "failed"
        elif self.queued.get(name) or self.running.get(name):
            state = "pending"
        elif status in ("needs_credentials", "needs_subscription", "disabled", "not_installed", "manual",
                        "catalog_only", "retired", "excluded"):
            state = status if applicable else "not_applicable"
        elif done:
            state = "skipped"
        elif reasons:
            state = "skipped"
        elif not applicable:
            state = "not_applicable"
        else:
            state = "not_run"
        m = conn.manifest
        return {"connector": name, "title": m.title, "category": m.category, "category_title": m.category_title,
                "group": m.progress_group, "state": state, "connector_status": status,
                "note": self.notes.get(name) or note, "runs": runs,
                "skips": reasons, "queued": self.queued.get(name, 0), "running": self.running.get(name, 0),
                "access": m.access, "public_record": m.public_record}

    def report(self, cfg, entity_types: set[str]) -> dict:
        rows = []
        for name, conn in REG.REGISTRY.items():
            inputs = set(conn.input_types())
            applicable = bool(inputs & entity_types)
            rows.append(self.connector_state(name, cfg, applicable))
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[r["state"]] += 1
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault(r["group"], {"group": r["group"], "done": 0, "pending": 0, "total": 0, "found": 0,
                                               "queried_sources": 0, "unavailable_sources": 0})
            done = sum(r["runs"].values())
            pending = r["queued"] + r["running"]
            g["done"] += done
            g["pending"] += pending
            g["total"] += done + pending
            g["found"] += r["runs"].get("found", 0)
            if r["state"] == "queried":
                g["queried_sources"] += 1
            elif r["state"] in ("failed", "needs_credentials", "needs_subscription", "not_installed"):
                g["unavailable_sources"] += 1
        for g in groups.values():
            g["progress"] = 1.0 if not g["total"] else round(g["done"] / g["total"], 3)
        order = list(dict.fromkeys(v[1] for v in CATEGORIES.values()))
        return {
            "summary": {
                "queried": counts["queried"],
                "failed_or_blocked": counts["failed"],
                "pending": counts["pending"],
                "requires_credentials": counts["needs_credentials"],
                "requires_subscription": counts["needs_subscription"],
                "disabled": counts["disabled"],
                "not_installed": counts["not_installed"],
                "manual_only": counts["manual"],
                "catalog_only": counts["catalog_only"] + counts["retired"],
                "excluded_by_policy": counts["excluded"],
                "skipped": counts["skipped"] + counts["not_run"],
                "not_applicable": counts["not_applicable"],
                "total_connectors": len(rows),
            },
            "groups": sorted(groups.values(), key=lambda g: order.index(g["group"]) if g["group"] in order else 99),
            "connectors": sorted(rows, key=lambda r: (r["state"] != "queried", r["category"], r["title"])),
            "disclaimer": DISCLAIMER,
        }

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {"runs": {k: dict(v) for k, v in self.runs.items()}, "reasons": {k: dict(v) for k, v in self.reasons.items()},
                "notes": dict(self.notes)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "CoverageTracker":
        c = cls()
        for k, v in ((d or {}).get("runs") or {}).items():
            for s, n in v.items():
                c.runs[k][s] = n
        for k, v in ((d or {}).get("reasons") or {}).items():
            for s, n in v.items():
                c.reasons[k][s] = n
        c.notes.update((d or {}).get("notes") or {})
        return c


# --------------------------------------------------------------------------- investigation-level global coverage

PLATFORM_SEARCHED = ("found", "not_found")
PLATFORM_UNAVAILABLE = ("blocked", "error", "unknown")


def global_coverage(rt, cfg, connector_rows: list[dict], health_records: dict | None = None, catalog=None) -> dict:
    """What this investigation actually covered, across countries, platforms, public databases and commercial
    providers. Every count comes from the registries and from this investigation's runs and checks. ``catalog``: the
    user layer of the catalog (the investigator's own sources count as links for their jurisdictions)."""
    from ..registry import countries as C
    from ..registry import coverage_model as CM
    from ..registry import jurisdictions as J
    from ..registry import platforms as P

    ctx = CM.CoverageContext(cfg, health_records or {}, catalog)
    rows = CM.cached_rows(ctx)
    sov = [r for r in rows if r["sovereign"]]
    applicable = []
    by_name = {r["connector"]: r for r in connector_rows}
    for code in rt.info.countries:
        r = next((x for x in rows if x["code"] == code), None)
        if r is None:
            continue
        conns = []
        for name in ctx.country_connectors(code):
            row = by_name.get(name, {})
            conns.append({"connector": name, "title": REG.REGISTRY[name].manifest.title, "state": row.get("state", "not_run"),
                          "runs": row.get("runs", {})})
        j = J.jurisdictions().get(code)
        applicable.append({"code": code, "name": r["name"], "level": r["level"], "level_title": r["level_title"],
                           "connectors": conns, "official_links": len(j.sources) if j else 0,
                           "your_sources": r["custom_active"], "potential_sources": r["discovered_sources"]})
    # platforms: per platform, the best outcome of all checks in this investigation
    outcome: dict[str, str] = {}
    rank = {"found": 5, "not_found": 4, "blocked": 3, "error": 2, "unknown": 2, "skipped": 1}
    for chk in rt.graph.checks.values():
        p = P.get(chk.platform)
        pid = p.id if p else chk.platform
        if rank.get(chk.status, 0) > rank.get(outcome.get(pid, ""), 0):
            outcome[pid] = chk.status
    registered = len(P.curated()) + len(P.imported())
    searched = sum(1 for s in outcome.values() if s in PLATFORM_SEARCHED)
    unavailable = sum(1 for s in outcome.values() if s in PLATFORM_UNAVAILABLE)
    skipped = sum(1 for s in outcome.values() if s == "skipped")
    has_username = bool(rt.info.seeds.get("username")) or any(e.type == "username" for e in rt.graph.entities.values())
    # public databases and commercial providers (connectors), from the per-connector coverage rows
    pub = {"queried": 0, "failed": 0, "unavailable": 0, "not_applicable": 0, "other": 0}
    com = {"configured": 0, "queried": 0, "not_configured": 0, "catalog_only": 0}
    for r in connector_rows:
        m = REG.REGISTRY[r["connector"]].manifest
        if m.public_record:
            st = r["state"]
            key = "queried" if st == "queried" else "failed" if st == "failed" else "not_applicable" \
                if st == "not_applicable" else "unavailable" if st in ("needs_credentials", "needs_subscription",
                                                                       "not_installed", "disabled", "catalog_only") else "other"
            pub[key] += 1
        if m.access in CM.COMMERCIAL_ACCESS:
            cs = r["connector_status"]
            if cs == "available":
                com["configured"] += 1
                com["queried"] += 1 if r["state"] == "queried" else 0
            elif cs == "catalog_only":
                com["catalog_only"] += 1
            elif cs in ("needs_subscription", "needs_credentials", "disabled"):
                com["not_configured"] += 1
    links = sum(a["official_links"] + a["your_sources"] for a in applicable)
    return {
        "countries": {"evaluated": len(sov), "applicable": applicable,
                      "with_sources": sum(1 for r in sov if r["sources"] or r["connectors"]),
                      "with_country_connectors": sum(1 for r in sov if r["connectors"]),
                      "no_connector": sum(1 for r in sov if not r["connectors"]),
                      "note": ("Countries were inferred from the input (phone numbers, domains, locations) or selected by you; "
                               "country-specific sources ran only for those. Global sources cover every country.")
                      if applicable else "No country could be inferred from the input; only global sources applied."},
        "platforms": {"registered": registered, "evaluated": registered, "searched": searched,
                      "found": sum(1 for s in outcome.values() if s == "found"), "unavailable": unavailable,
                      "skipped": skipped, "not_searched": max(0, registered - searched - unavailable - skipped),
                      "username_available": has_username,
                      "note": "Platforms are searched by username; without a username only name-based sources apply."
                      if not has_username else "Unsearched platforms had no automated check on this installation "
                                               "(manual-only, no maintained rule, optional adapter not installed, or not "
                                               "publicly searchable)."},
        "public_databases": {**pub, "official_links_for_applicable_countries": links},
        "commercial": com,
        "disclaimer": CM.SEARCH_DISCLAIMER,
        "countries_total_registry": len(C.countries()),
    }
