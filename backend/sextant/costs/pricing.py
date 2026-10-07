"""The pricing table: published prices for cost estimates, editable without code changes.

Two layers, like the catalog: the shipped table (``data/pricing/pricing.toml``) and the investigator's overrides
(stored in the settings table under ``pricing_overrides``). An override with the same key (provider, service,
connector, model) replaces the shipped entry. A connector without a table entry falls back to the per-call estimate in
its manifest, labelled as such. A price nobody knows stays unknown: costs computed from it show "Unknown".

Provider identity: every price belongs to a normalized ``provider`` (one API account). Several services and
connectors can share one provider (the OpenAI key used by the assistant, by summarisation, by planning…), and cost
reports add them up per provider instead of counting them as separate providers.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "pricing" / "pricing.toml"
OVERRIDES_KEY = "pricing_overrides"
NUMERIC = ("per_request", "input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "reasoning_per_mtok")
TEXT = ("provider", "service", "connector", "model", "currency", "effective_date", "source_url", "verified_date", "note")

# connectors whose provider identity is not their own name (shared accounts)
CONNECTOR_PROVIDER = {"brave_search": "brave", "google_cse": "google", "ai_assistant": "ai"}

# AI provider ids (assistant/providers.py) and their titles
PROVIDER_TITLES = {"openai": "OpenAI", "anthropic": "Anthropic", "openrouter": "OpenRouter", "local": "Local model",
                   "openai_compatible": "OpenAI-compatible endpoint", "brave": "Brave Search", "google": "Google",
                   "serpapi": "SerpApi", "searxng": "SearXNG (self-hosted)", "serper": "Serper", "tavily": "Tavily"}


def key_of(e: dict) -> str:
    return "|".join(str(e.get(k) or "") for k in ("provider", "service", "connector", "model"))


def clean(entry: dict) -> dict:
    """Validates one price entry (from the file, an override or the API)."""
    out: dict = {}
    for k in TEXT:
        v = entry.get(k)
        if v not in (None, ""):
            s = str(v).strip()
            if len(s) > 500:
                raise ValueError(f"{k} is too long")
            out[k] = s
    if not out.get("provider"):
        raise ValueError("A price needs a provider")
    out.setdefault("service", "api")
    out.setdefault("currency", "USD")
    if out["currency"] != "USD":
        raise ValueError("Prices are stored in USD (the display currency is a separate setting)")
    for k in NUMERIC:
        v = entry.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number") from None
        if f < 0 or f > 100_000:
            raise ValueError(f"{k} is out of range")
        out[k] = f
    for d in ("effective_date", "verified_date"):
        if out.get(d) and not _is_date(out[d]):
            raise ValueError(f"{d} must be YYYY-MM-DD")
    return out


def _is_date(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


@lru_cache(maxsize=1)
def shipped() -> tuple[dict, ...]:
    with open(DATA, "rb") as fh:
        doc = tomllib.load(fh)
    return tuple({**clean(e), "origin": "system"} for e in doc.get("price", []))


def _db(cfg):
    return getattr(cfg, "db", None)


def overrides(cfg) -> list[dict]:
    db = _db(cfg)
    if db is None:
        return []
    raw = db.get_setting(OVERRIDES_KEY) or []
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            out.append({**clean(e), "origin": "user"})
        except ValueError:
            continue
    return out


PROVIDER_PRICES_KEY = "provider_prices"


def provider_prices(cfg) -> list[dict]:
    """Prices read from a provider's own API (e.g. OpenRouter's model list), with the date they were fetched."""
    db = _db(cfg)
    raw = (db.get_setting(PROVIDER_PRICES_KEY) if db is not None else None) or []
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            out.append({**clean(e), "origin": "provider_api"})
        except ValueError:
            continue
    return out


def store_provider_prices(cfg, provider: str, models: list[dict], fetched: str, source_url: str) -> int:
    """Replaces the stored API prices of one provider with the models it lists now."""
    keep = [e for e in provider_prices(cfg) if e["provider"] != provider]
    new = []
    for m in models:
        if m.get("input_per_mtok") is None and m.get("output_per_mtok") is None:
            continue
        new.append(clean({"provider": provider, "service": "ai_chat", "model": m["id"],
                          **{k: m.get(k) for k in NUMERIC if m.get(k) is not None},
                          "source_url": source_url, "verified_date": fetched[:10], "effective_date": fetched[:10],
                          "note": "Read from the provider's model list (its published prices at that date)."}))
    _db(cfg).set_setting(PROVIDER_PRICES_KEY, [{k: v for k, v in e.items() if k != "origin"} for e in keep + new] or None)
    return len(new)


def table(cfg=None) -> list[dict]:
    by_key = {key_of(e): e for e in shipped()}
    for e in provider_prices(cfg):
        by_key[key_of(e)] = e
    for e in overrides(cfg):
        by_key[key_of(e)] = e
    out = []
    for e in by_key.values():
        e = dict(e)
        e["verified"] = bool(e.get("verified_date"))
        e["provider_title"] = PROVIDER_TITLES.get(e["provider"], e["provider"])
        e["key"] = key_of(e)
        out.append(e)
    return sorted(out, key=lambda e: (e["provider"], e.get("service", ""), e.get("model", ""), e.get("connector", "")))


def put_override(cfg, entry: dict) -> dict:
    e = clean(entry)
    cur = [x for x in overrides(cfg) if key_of(x) != key_of(e)]
    cur.append(e)
    _db(cfg).set_setting(OVERRIDES_KEY, [{k: v for k, v in x.items() if k != "origin"} for x in cur])
    return {**e, "origin": "user", "key": key_of(e)}


def delete_override(cfg, key: str) -> bool:
    cur = overrides(cfg)
    keep = [x for x in cur if key_of(x) != key]
    if len(keep) == len(cur):
        return False
    _db(cfg).set_setting(OVERRIDES_KEY, [{k: v for k, v in x.items() if k != "origin"} for x in keep] or None)
    return True


def provider_for_connector(name: str) -> str:
    for e in shipped():
        if e.get("connector") == name:
            return e["provider"]
    return CONNECTOR_PROVIDER.get(name, name)


def price_for_connector(name: str, cfg=None) -> dict | None:
    """The per-request price of a connector: the table's entry, else its manifest estimate, else None (unknown)."""
    for e in table(cfg):
        if e.get("connector") == name and e.get("per_request") is not None:
            return e
    from ..registry import connectors as REG
    conn = REG.REGISTRY.get(name)
    if conn is None:
        return None
    m = conn.manifest
    if m.cost_per_call_usd:
        return {"provider": provider_for_connector(name), "service": "api", "connector": name,
                "per_request": float(m.cost_per_call_usd), "per_run": True, "origin": "manifest",
                "verified_date": "", "verified": False, "source_url": m.website,
                "note": f"Estimate from the source's description ({m.cost}); not a verified price."}
    if m.access in ("free", "free_key", "local", "manual") or m.access == "registration":
        return {"provider": provider_for_connector(name), "service": "api", "connector": name, "per_request": 0.0,
                "origin": "free", "verified": True, "verified_date": "", "note": "Free source"}
    return None


def price_for_model(provider: str, model: str, cfg=None) -> dict | None:
    best = None
    for e in table(cfg):
        if e["provider"] != provider or e.get("service") not in ("ai_chat", None):
            continue
        if e.get("model") == model:
            return e
        if e.get("model") in ("*", None, "") and best is None:
            best = e
    return best


def token_cost(price: dict | None, usage: dict) -> float | None:
    """USD for one AI request from published per-token prices; None when any needed price is unknown."""
    if not price or price.get("input_per_mtok") is None or price.get("output_per_mtok") is None:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    cached_rate = price.get("cached_input_per_mtok")
    uncached = max(0, prompt - cached) if cached_rate is not None else prompt
    cost = uncached * price["input_per_mtok"] + completion * price["output_per_mtok"]
    if cached_rate is not None:
        cost += cached * cached_rate
    if reasoning and price.get("reasoning_per_mtok") is not None:
        cost += reasoning * price["reasoning_per_mtok"]       # only when the provider bills reasoning separately
    return round(cost / 1_000_000, 6)


__all__ = ["table", "shipped", "provider_prices", "store_provider_prices", "overrides", "put_override", "delete_override", "price_for_connector", "price_for_model",
           "provider_for_connector", "token_cost", "clean", "key_of", "PROVIDER_TITLES"]
