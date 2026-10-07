"""Configuration: data directory, .env loading, global settings, credentials, connector preferences.

Precedence for a credential or setting: environment variable > value stored (encrypted) through the UI
> default. Secrets are never logged and never returned by the API (only "set"/"not set").
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage.db import Database


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    help: str
    kind: str = "text"            # text | int | float | bool | url | choice
    default: Any = None
    choices: tuple[str, ...] = ()


SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("default_depth", "Default investigation depth", "1 direct · 2 related · 3 deep correlation · 4 aggressive.",
                "choice", "2", ("1", "2", "3", "4")),
    SettingSpec("respect_robots_txt", "Respect robots.txt",
                "On (recommended): sites whose robots.txt disallows automated clients are skipped and listed as links for "
                "you. Anti-bot protection is never bypassed either way.", "bool", True),
    SettingSpec("include_adult_sites", "Include adult & dating sites", "Username checks on adult/dating sites (opt-in).",
                "bool", False),
    SettingSpec("generate_username_guesses", "Generate username guesses from names",
                "At depth 4 the engine may try a few handles derived from the target's name (clearly marked as guesses, "
                "never evidence).", "bool", True),
    SettingSpec("default_region", "Default phone country", "2-letter country used for numbers without +country prefix.",
                "text", ""),
    SettingSpec("contact_email", "Contact e-mail for polite APIs",
                "Optional. Sent as the contact address to APIs that ask for one (SEC EDGAR, Crossref/OpenAlex polite pool, "
                "Nominatim). Without it a generic contact string is used.", "text", ""),
    SettingSpec("max_concurrency", "Parallel transforms", "How many transforms may run at the same time.", "int", 6),
    SettingSpec("maigret_database", "Maigret site database",
                "current: Maigret's latest database through its own update channel (version check + SHA-256), refreshed "
                "daily; bundled: the database shipped with the installed Maigret version.", "choice", "current",
                ("current", "bundled")),
    SettingSpec("allow_paid_sources", "Allow paid sources",
                "On: sources that cost money (subscriptions, metered APIs) may run when you have configured them. Off: they "
                "never run, whatever else is set.", "bool", True),
    SettingSpec("max_paid_cost_usd", "Maximum estimated paid cost per investigation (USD)",
                "Paid sources stop once their estimated cost (calls × the provider's published price per call) reaches this "
                "amount in one investigation. 0 = no limit. Estimates only; your provider's bill is authoritative.",
                "float", 0.0),
    SettingSpec("enhanced_automation", "Enhanced anti-bot handling (compliant)",
                "Slower pacing, more patient retries that honour Retry-After, and a switch to another configured source when "
                "one asks for verification. It never solves or bypasses CAPTCHAs, Cloudflare/Turnstile challenges, WAFs, "
                "logins or rate limits: such pages are reported as “Verification required — manual action needed”.",
                "bool", False),
    SettingSpec("ai_provider", "AI assistant provider",
                "The primary provider. none: the assistant works without AI (plans, candidates, evidence, next steps). "
                "anthropic: Claude. openai: OpenAI. openrouter: any model OpenRouter routes to (billed cost reported per "
                "request). local: Ollama / LM Studio / vLLM on this computer. openai_compatible: another compatible "
                "endpoint. Each has its own model, endpoint, key and limits under Settings → AI providers, and a failover "
                "order. The AI receives the investigation's findings when you chat; local-only data never.",
                "choice", "none", ("none", "anthropic", "openai", "openrouter", "local", "openai_compatible")),
    SettingSpec("ai_model", "AI model",
                "Empty = the provider default (claude-opus-5-5 for Anthropic). For a local server, its model name, e.g. "
                "llama3.1.", "text", ""),
    SettingSpec("ai_base_url", "AI endpoint",
                "OpenAI-compatible: the base URL ending in /v1, e.g. http://127.0.0.1:11434/v1 for Ollama or "
                "https://api.openai.com/v1. Anthropic: leave empty for api.anthropic.com, or an Anthropic-compatible "
                "endpoint.", "text", ""),
    SettingSpec("ai_effort", "AI effort", "How hard the model thinks (Anthropic): low is fastest and cheapest.", "choice",
                "medium", ("low", "medium", "high", "xhigh", "max")),
    SettingSpec("search_provider_order", "Search provider preference",
                "Comma-separated connector names, first = preferred (e.g. searxng,brave_search,serper). Providers not "
                "listed follow, cheapest first. Automatic searches go to the first available one and fail over to the "
                "next when it errors.", "text", ""),
    SettingSpec("search_provider_mode", "Search providers per search",
                "primary: one provider per search with failover (no double cost). all: every available provider "
                "(more coverage, more cost).", "choice", "primary", ("primary", "all")),
    SettingSpec("discovery_pivot_cap", "Discovered handles to pivot on automatically",
                "How many handles found in name searches are searched across other platforms automatically per "
                "investigation (more can be run from the assistant).", "int", 5),
    SettingSpec("default_budget_usd", "Default budget per investigation (USD)",
                "Billable steps (paid APIs, search queries, AI) stop and ask once the estimated cost would pass this "
                "amount. 0 = unlimited. Can be changed per investigation on the start screen.", "float", 0.0),
    SettingSpec("cost_display_currency", "Display currency", "Prices are stored in USD; EUR uses the rate below.",
                "choice", "EUR", ("EUR", "USD")),
    SettingSpec("cost_usd_to_eur", "USD → EUR rate",
                "Used only to display amounts in EUR. Set it to the current rate; it is not fetched.", "float", 0.86),
    SettingSpec("ai_auto_apply_catalog", "Allow the AI to apply non-destructive catalog changes automatically",
                "On: adding a source, link, category, platform or jurisdiction link through the assistant is saved "
                "without asking. Deletions and resets always ask, whatever this says.", "bool", False),
    SettingSpec("ai_action_preview_usd", "Preview AI actions above (USD)",
                "Assistant actions estimated to cost more than this (or to make more than 20 calls) show a preview "
                "you confirm first. 0 = preview every billable action.", "float", 0.05),
    SettingSpec("maigret_top_sites", "Maigret sites per normal run",
                "How many of Maigret's most popular sites a normal investigation checks (SEARCH EVERYTHING checks them "
                "all). More sites take longer: about a minute per 500.", "int", 500),
)
SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


def default_data_dir() -> Path:
    env = os.environ.get("SEXTANT_DATA_DIR")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Sextant"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Sextant"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "sextant"


def default_frontend_dir() -> Path:
    env = os.environ.get("SEXTANT_FRONTEND_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def load_dotenv(path: Path) -> list[str]:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes). Never overrides real env vars."""
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ and value:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class Settings:
    """Settings + credentials + connector preferences; implements the registry's ConfigView."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._prefs_cache: dict[str, dict] | None = None

    # ------------------------------------------------------------------ generic settings
    def get(self, key: str) -> Any:
        env = os.environ.get(key) or os.environ.get(f"SEXTANT_{key.upper()}")
        if env not in (None, ""):
            return env
        stored = self.db.get_setting(key)
        if stored not in (None, ""):
            return stored
        cred = self.credential(key)
        if cred:
            return cred
        spec = SETTINGS_BY_KEY.get(key)
        return spec.default if spec else None

    def get_bool(self, key: str) -> bool:
        v = self.get(key)
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")

    def get_float(self, key: str, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(self.get(key))))
        except (TypeError, ValueError):
            return float(SETTINGS_BY_KEY[key].default)

    def get_int(self, key: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(self.get(key))))
        except (TypeError, ValueError):
            return int(SETTINGS_BY_KEY[key].default)

    def set(self, key: str, value: Any) -> None:
        spec = SETTINGS_BY_KEY.get(key)
        if spec is None:
            raise KeyError(key)
        if value in (None, ""):
            self.db.set_setting(key, None)
            return
        if spec.kind == "bool":
            value = str(value).strip().lower() in ("1", "true", "yes", "on")
        elif spec.kind == "int":
            value = int(value)
        elif spec.kind == "float":
            value = float(value)
        elif spec.kind == "choice":
            if str(value) not in spec.choices:
                raise ValueError(f"{spec.label}: choose one of {', '.join(spec.choices)}")
            value = str(value)
        else:
            value = str(value).strip()
            if len(value) > 500:
                raise ValueError("Value too long")
        self.db.set_setting(key, value)

    def describe(self) -> list[dict]:
        out = []
        for s in SETTINGS:
            out.append({"key": s.key, "label": s.label, "help": s.help, "kind": s.kind, "default": s.default,
                        "choices": list(s.choices), "value": self.get(s.key),
                        "source": "environment" if os.environ.get(s.key) else
                                  ("stored" if self.db.get_setting(s.key) not in (None, "") else "default")})
        return out

    # ------------------------------------------------------------------ credentials
    def credential(self, key: str) -> str | None:
        env = os.environ.get(key)
        if env:
            return env
        return self.db.get_credential(key)

    def credential_source(self, key: str) -> str | None:
        if os.environ.get(key):
            return "environment"
        if key in self.db.credential_keys():
            return "stored (encrypted)"
        return None

    def set_credential(self, key: str, value: str | None) -> None:
        if value is not None:
            value = value.strip()
            if len(value) > 4000 or "\n" in value:
                raise ValueError("Invalid credential value")
        self.db.set_credential(key, value or None)

    # ------------------------------------------------------------------ connector preferences
    def connector_prefs(self, name: str) -> dict:
        if self._prefs_cache is None:
            self._prefs_cache = self.db.connector_prefs()
        return self._prefs_cache.get(name, {})

    def set_connector_pref(self, name: str, **kw) -> None:
        self.db.set_connector_pref(name, **kw)
        self._prefs_cache = None

    def invalidate_prefs(self) -> None:
        """After the stored preferences changed outside ``set_connector_pref`` (the catalog reset)."""
        self._prefs_cache = None
