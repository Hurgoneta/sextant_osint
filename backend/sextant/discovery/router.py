"""Which configured web-search provider runs an investigation's searches.

Every search provider (Brave, SearXNG, Serper, Tavily, Google CSE, SerpApi, …) is a connector with the same
transforms. Running them all would pay several times for the same results, so the engine routes automatic search
runs to one *primary* provider and fails over to the next when it errors, refuses or lacks credentials. The order is:

1. the investigator's preference (setting ``search_provider_order``), then
2. price per query (free / self-hosted first; prices from the pricing table, ``costs/pricing.py``), then
3. the provider's reliability rating.

Disabled providers, providers without credentials and providers the investigator switched off never appear. With
``search_provider_mode = all`` every available provider runs (more coverage, more cost).
"""

from __future__ import annotations

from ..registry import connectors as REG

RELIABILITY_RANK = {"official": 0, "high": 1, "medium": 2, "low": 3}
FAILOVER_STATUSES = frozenset({"error", "blocked", "rate_limited", "needs_credentials", "needs_subscription", "timeout"})


def is_search_provider(conn) -> bool:
    from ..connectors.search.engines import SearchConnector
    return isinstance(conn, SearchConnector)


def providers() -> list[str]:
    REG.load_all()
    return [n for n, c in REG.REGISTRY.items() if is_search_provider(c)]


def ordered(cfg) -> list[dict]:
    """Every search provider with its status, price and rank (available first, in routing order)."""
    from ..costs.pricing import price_for_connector
    pref = [p.strip() for p in str((cfg.get("search_provider_order") if cfg else "") or "").split(",") if p.strip()]
    rows = []
    for name in providers():
        conn = REG.REGISTRY[name]
        status, note = REG.connector_status(conn, cfg)
        price = price_for_connector(name, cfg)
        per_query = price.get("per_request") if price else None
        rows.append({"name": name, "title": conn.manifest.title, "status": status, "note": note,
                     "per_query_usd": per_query, "price_known": per_query is not None,
                     "reliability": conn.manifest.reliability,
                     "preference": pref.index(name) if name in pref else len(pref) + 1})
    rows.sort(key=lambda r: (r["status"] != "available", r["preference"],
                             r["per_query_usd"] if r["per_query_usd"] is not None else 1.0,
                             RELIABILITY_RANK.get(r["reliability"], 9), r["name"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def available(cfg) -> list[str]:
    return [r["name"] for r in ordered(cfg) if r["status"] == "available"]


def primary(available_list: list[str], failed: list[str] | tuple = ()) -> str | None:
    return next((p for p in available_list if p not in failed), None)


def unavailable_message(cfg) -> str:
    rows = ordered(cfg)
    if any(r["status"] == "available" for r in rows):
        return ""
    return ("Web discovery unavailable — API-only search was used. No web-search provider is configured (Data Sources → "
            "Search engines: SearXNG is free and self-hosted; Brave, Serper and Tavily need a key), so profiles that "
            "only a search engine can reveal were not looked for. This does not mean the person has no accounts.")


__all__ = ["FAILOVER_STATUSES", "is_search_provider", "providers", "ordered", "available", "primary",
           "unavailable_message"]
