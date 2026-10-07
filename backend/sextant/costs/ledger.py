"""What an investigation cost, what it will cost, and whether the next step fits the budget.

Sources of truth, never mixed up:

* **Actual**: a provider reported the billed amount for a request (OpenRouter returns the cost of each call).
* **Estimated**: requests or tokens counted by Sextant × the published price in the pricing table. Not an invoice.
* **Pending**: work queued but not run yet, estimated the same way.
* **Unknown**: something billable ran but no price is known (shown as such, never as 0 and never as a guess).

Costs are added up per normalized provider (one API account), so an OpenAI key used by the assistant and by
summarisation is one provider line with several services.
"""

from __future__ import annotations

from collections import defaultdict

from ..registry import connectors as REG
from . import pricing as PR

CATEGORY_TITLES = {"ai": "AI", "search": "Search", "api": "APIs", "commercial": "Commercial providers"}
CURRENCIES = ("EUR", "USD")


def money(cfg) -> dict:
    """Display currency and the USD→EUR rate the investigator set (prices are stored in USD)."""
    cur = str((cfg.get("cost_display_currency") if cfg else None) or "EUR").upper()
    if cur not in CURRENCIES:
        cur = "USD"
    try:
        rate = float(cfg.get("cost_usd_to_eur")) if cfg and cfg.get("cost_usd_to_eur") not in (None, "") else 0.86
    except (TypeError, ValueError):
        rate = 0.86
    return {"currency": cur, "symbol": "€" if cur == "EUR" else "$", "rate": rate if cur == "EUR" else 1.0,
            "note": f"Converted from USD at {rate:g} (Settings → Costs); not a live exchange rate." if cur == "EUR"
            else "Amounts in USD as published by the providers."}


def category_of(conn) -> str:
    m = conn.manifest
    if m.category == "search":
        return "search"
    if m.category == "people_search" or m.access in ("enterprise",) or m.requires_subscription and m.access == "paid":
        return "commercial"
    return "api"


def connector_unit_cost(name: str, cfg) -> tuple[float | None, dict | None]:
    """USD per request (or per run for manifest estimates) and the price entry; (None, None) when unknown."""
    p = PR.price_for_connector(name, cfg)
    if p is None:
        return None, None
    return float(p["per_request"]), p


def _is_billable(conn) -> bool:
    m = conn.manifest
    return bool(m.cost_per_call_usd) or m.access in ("metered", "paid", "enterprise") or m.category == "search"


def run_cost(conn, requests: int, cfg) -> tuple[float | None, dict | None]:
    unit, p = connector_unit_cost(conn.manifest.name, cfg)
    if unit is None:
        return None, None
    n = 1 if p and p.get("per_run") else max(requests, 0)
    return round(unit * n, 6), p


def investigation_costs(rt, db, cfg, engine=None) -> dict:
    """The cost dashboard of one investigation: per provider, per category, the chain of steps, totals and budget."""
    lines: dict[str, dict] = {}

    def line(provider: str, category: str) -> dict:
        d = lines.get(provider)
        if d is None:
            d = lines[provider] = {"provider": provider, "title": PR.PROVIDER_TITLES.get(provider, _title(provider)),
                                   "category": category, "services": [], "models": [], "requests": 0, "tokens": 0,
                                   "estimated": 0.0, "actual": None, "pending": 0.0, "unknown": 0,
                                   "price_verified": None, "first_at": None}
        return d

    chain: list[dict] = []
    # connector runs (search, APIs, commercial)
    for r in sorted(rt.runs.values(), key=lambda r: r.started_at):
        conn = REG.REGISTRY.get(r.connector)
        if conn is None or not _is_billable(conn):
            continue
        provider = PR.provider_for_connector(r.connector)
        cat = category_of(conn)
        d = line(provider, cat)
        d["requests"] += r.requests
        if conn.manifest.category not in d["services"]:
            d["services"].append(conn.manifest.category)
        cost, p = run_cost(conn, r.requests, cfg)
        if cost is None:
            if r.requests:
                d["unknown"] += 1
        else:
            d["estimated"] += cost
            d["price_verified"] = bool(p.get("verified")) if d["price_verified"] is None else d["price_verified"] and bool(p.get("verified"))
        d["first_at"] = d["first_at"] or r.started_at
        if r.requests or cost:
            chain.append({"at": r.started_at, "step": cat, "provider": provider, "title": d["title"],
                          "what": conn.manifest.title, "requests": r.requests, "estimated": cost, "status": r.status})
    # queued work (pending)
    for name, n in rt.coverage.queued.items():
        conn = REG.REGISTRY.get(name)
        if not n or conn is None or not _is_billable(conn):
            continue
        unit, p = connector_unit_cost(name, cfg)
        d = line(PR.provider_for_connector(name), category_of(conn))
        per_run = 1 if (p and p.get("per_run")) else _avg_requests(rt, name)
        if unit is not None:
            d["pending"] += unit * per_run * n
    # AI and other recorded usage
    for u in db.usage_list(rt.id):
        d = line(u["provider"], "ai" if u["service"].startswith("ai") else "api")
        d["requests"] += u["requests"]
        d["tokens"] += u["total_tokens"]
        if u["service"] not in d["services"]:
            d["services"].append(u["service"])
        if u["model"] and u["model"] not in d["models"]:
            d["models"].append(u["model"])
        if u["actual_cost"] is not None:
            d["actual"] = (d["actual"] or 0.0) + u["actual_cost"]
        if u["estimated_cost"] is not None:
            d["estimated"] += u["estimated_cost"]
        elif u["actual_cost"] is None:
            d["unknown"] += 1
        chain.append({"at": u["at"], "step": "ai" if u["service"].startswith("ai") else "api", "provider": u["provider"],
                      "title": d["title"], "what": f"{u['service']} · {u['model']}".strip(" ·"), "requests": u["requests"],
                      "tokens": u["total_tokens"], "estimated": u["estimated_cost"], "actual": u["actual_cost"],
                      "status": u["cost_status"]})
    out_lines = sorted(lines.values(), key=lambda d: (list(CATEGORY_TITLES).index(d["category"]), -d["estimated"]))
    for d in out_lines:
        d["estimated"] = round(d["estimated"], 6)
        d["pending"] = round(d["pending"], 6)
        d["status"] = ("actual" if d["actual"] is not None else "unknown" if d["unknown"] and not d["estimated"]
                       else "estimated")
    by_cat = {c: {"title": t, "estimated": 0.0, "actual": None, "pending": 0.0, "unknown": 0}
              for c, t in CATEGORY_TITLES.items()}
    for d in out_lines:
        b = by_cat[d["category"]]
        b["estimated"] += d["estimated"]
        b["pending"] += d["pending"]
        b["unknown"] += d["unknown"]
        if d["actual"] is not None:
            b["actual"] = (b["actual"] or 0.0) + d["actual"]
    est = round(sum(d["estimated"] for d in out_lines), 6)
    actual_known = [d for d in out_lines if d["actual"] is not None]
    pending = round(sum(d["pending"] for d in out_lines), 6)
    budget = budget_of(rt)
    spent_for_budget = spent(rt, db, cfg)
    return {
        "lines": out_lines, "by_category": by_cat, "chain": chain[-300:], "money": money(cfg),
        "totals": {"estimated": est, "actual": round(sum(d["actual"] for d in actual_known), 6) if actual_known else None,
                   "actual_covers": f"{len(actual_known)} of {len(out_lines)} provider(s) report billed amounts",
                   "pending": pending, "unknown_items": sum(d["unknown"] for d in out_lines),
                   "free_requests": sum(r.requests for r in rt.runs.values()
                                        if r.connector in REG.REGISTRY and not _is_billable(REG.REGISTRY[r.connector]))},
        "budget": {"limit_usd": budget, "spent_usd": spent_for_budget,
                   "remaining_usd": None if budget is None else round(max(0.0, budget - spent_for_budget), 6),
                   "confirm_above_usd": rt.options.get("confirm_above_usd"), "hold": rt.data.get("budget_hold")},
        "explanation": "Estimated = counted requests/tokens × published prices (pricing table). Actual = billed amount "
                       "reported by the provider for each request. Pending = queued work. Unknown = billable work "
                       "without a known price. Your provider's invoice is authoritative.",
    }


def _title(provider: str) -> str:
    conn = REG.REGISTRY.get(provider)
    return conn.manifest.title if conn else provider


def _avg_requests(rt, connector: str) -> float:
    runs = [r for r in rt.runs.values() if r.connector == connector and r.requests]
    if runs:
        return sum(r.requests for r in runs) / len(runs)
    ts = [t for t in REG.TRANSFORMS.values() if t.connector == connector]
    return max((getattr(t, "estimated_requests", 1) or 1) for t in ts) if ts else 1


def budget_of(rt) -> float | None:
    b = rt.options.get("budget_usd")
    try:
        return float(b) if b not in (None, "", "unlimited") else None
    except (TypeError, ValueError):
        return None


def spent(rt, db, cfg) -> float:
    """Estimated (or actual, when reported) spend so far plus queued work — what a budget is checked against."""
    total = 0.0
    for r in rt.runs.values():
        conn = REG.REGISTRY.get(r.connector)
        if conn is not None and _is_billable(conn):
            cost, _ = run_cost(conn, r.requests, cfg)
            total += cost or 0.0
    for name, n in rt.coverage.queued.items():
        conn = REG.REGISTRY.get(name)
        if n and conn is not None and _is_billable(conn):
            unit, p = connector_unit_cost(name, cfg)
            total += (unit or 0.0) * n * (1 if p and p.get("per_run") else _avg_requests(rt, name))
    for u in db.usage_list(rt.id):
        total += u["actual_cost"] if u["actual_cost"] is not None else (u["estimated_cost"] or 0.0)
    return round(total, 6)


def task_estimate(t, conn, cfg) -> float:
    """Estimated USD of running one transform once."""
    unit, p = connector_unit_cost(conn.manifest.name, cfg)
    if not unit:
        return 0.0
    return unit if p and p.get("per_run") else unit * max(1, int(getattr(t, "estimated_requests", 1) or 1))


# --------------------------------------------------------------------------- forecast before an investigation starts

AI_TURN_ASSUMPTION = {"prompt_tokens": 12_000, "completion_tokens": 1_200, "turns": 3}


def forecast(plan: dict, cfg, ai_config: dict | None = None) -> dict:
    """Forecast of one investigation from the plan (assistant/planner.py): every source the plan will run, its
    estimated requests and price, grouped by category, plus the AI chat if a provider is ready."""
    rows: dict[str, dict] = {}
    unknown = []
    for step in plan.get("steps") or []:
        for src in step.get("sources") or []:
            name = src.get("name")
            conn = REG.REGISTRY.get(name)
            if conn is None or not src.get("auto") or src.get("status") != "available" or not _is_billable(conn):
                continue
            ts = [t for t in REG.TRANSFORMS.values() if t.connector == name and t.auto]
            reqs = sum(max(1, int(getattr(t, "estimated_requests", 1) or 1)) for t in ts) or 1
            unit, p = connector_unit_cost(name, cfg)
            cat = category_of(conn)
            if unit is None:
                unknown.append(conn.manifest.title)
                continue
            cost = unit if p and p.get("per_run") else unit * reqs
            d = rows.setdefault(PR.provider_for_connector(name), {
                "provider": PR.provider_for_connector(name), "title": PR.PROVIDER_TITLES.get(PR.provider_for_connector(name),
                                                                                             conn.manifest.title),
                "category": cat, "requests": 0, "estimated": 0.0, "verified": True})
            d["requests"] += reqs
            d["estimated"] += cost
            d["verified"] = d["verified"] and bool(p.get("verified"))
    ai = None
    if ai_config and ai_config.get("ready"):
        a = AI_TURN_ASSUMPTION
        price = PR.price_for_model(ai_config.get("provider_id") or ai_config.get("provider"), ai_config.get("model"), cfg)
        per = PR.token_cost(price, {"prompt_tokens": a["prompt_tokens"] * a["turns"],
                                    "completion_tokens": a["completion_tokens"] * a["turns"]})
        ai = {"provider": ai_config.get("provider_id") or ai_config.get("provider"), "model": ai_config.get("model"),
              "per_message": per, "assumption": f"≈{a['turns']} model calls of ~{a['prompt_tokens']:,} input / "
              f"{a['completion_tokens']:,} output tokens per chat message",
              "price_known": per is not None}
    total = round(sum(d["estimated"] for d in rows.values()), 6)
    return {"lines": sorted(rows.values(), key=lambda d: -d["estimated"]), "total_estimated": total, "ai": ai,
            "unknown": sorted(set(unknown)), "money": money(cfg),
            "note": "Upper-bound estimate of the automatic sources at this depth; discovered pivots may add queries "
                    "within the per-investigation caps. Recursion stops at the budget you set."}


def check(rt, db, cfg, add_usd: float) -> dict | None:
    """None when adding ``add_usd`` stays within the budget (and below the ask-before threshold); else a hold."""
    if add_usd <= 0:
        return None
    current = spent(rt, db, cfg)
    after = round(current + add_usd, 6)
    budget = budget_of(rt)
    ask = rt.options.get("confirm_above_usd")
    try:
        ask = float(ask) if ask not in (None, "") else None
    except (TypeError, ValueError):
        ask = None
    if budget is not None and after > budget:
        return {"reason": "budget", "current_usd": current, "after_usd": after, "limit_usd": budget}
    if ask is not None and current <= ask < after and not rt.options.get("confirmed_above"):
        return {"reason": "ask_before", "current_usd": current, "after_usd": after, "limit_usd": ask}
    return None


__all__ = ["investigation_costs", "forecast", "check", "spent", "budget_of", "money", "task_estimate", "run_cost",
           "category_of", "CATEGORY_TITLES"]
