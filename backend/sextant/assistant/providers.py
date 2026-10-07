"""AI providers behind one interface, with per-provider settings, credentials, failover and usage accounting.

The rest of the application (the copilot, the engine, the API) only sees ``AIProvider``:

    complete(system, transcript, tools) -> Reply      chat + tool calling, with token usage and cost metadata
    user_message / assistant_text / tool_results       the provider's native transcript format
    capabilities, provider_id, model, destination      what it can do and where the data goes

Implementations:

* ``AnthropicProvider``: Claude through the official ``anthropic`` SDK (``pip install anthropic``). Default model
  ``claude-opus-5-5``; effort is explicit (``output_config.effort``), tool choice is ``auto``, refusals are reported, and on
  the first-party API a declined request falls back server-side (``fallbacks: "default"``). Sampling parameters are not
  sent (current Claude models reject them).
* ``OpenAICompatibleProvider``: the ``/chat/completions`` protocol, used by four provider profiles that differ only in
  configuration: ``openai`` (api.openai.com), ``openrouter`` (openrouter.ai — any model it routes to, with the billed
  cost reported per request), ``local`` (Ollama / LM Studio / vLLM on this computer) and ``openai_compatible`` (any
  other endpoint). Requests go through Sextant's guarded HTTP client; only the configured host may be private.

Configuration (Settings → AI providers): the primary provider is the ``ai_provider`` setting; each provider has a
profile in the ``ai_providers`` setting (enabled, model, endpoint, temperature, maximum output, reasoning, timeout) and
its own key (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``OPENROUTER_API_KEY``…). ``ai_failover_order`` lists the
providers to try, in order, when the primary is unavailable; a provider that is disabled or not configured is never
used. The legacy single key ``AI_API_KEY`` and the ``ai_model`` / ``ai_base_url`` / ``ai_effort`` settings still apply
to the primary provider when its profile does not set them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

AI_KEY = "AI_API_KEY"                         # legacy single key: used by the primary provider when it has none
DEFAULT_MODEL = {"anthropic": "claude-opus-5-5"}
ANTHROPIC_HOST = "api.anthropic.com"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5")
MAX_TOKENS = 16000
PROFILES_KEY = "ai_providers"
FAILOVER_KEY = "ai_failover_order"

# provider id -> fixed facts. "kind" picks the implementation; everything else is configurable.
PROVIDERS: dict[str, dict] = {
    "anthropic": {"title": "Anthropic (Claude)", "kind": "anthropic", "base_url": "", "key": "ANTHROPIC_API_KEY",
                  "needs_key": True, "default_model": "claude-opus-5-5", "reasoning": ("low", "medium", "high", "xhigh", "max"),
                  "docs": "https://docs.anthropic.com"},
    "openai": {"title": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "key": "OPENAI_API_KEY",
               "needs_key": True, "default_model": "", "reasoning": ("minimal", "low", "medium", "high"),
               "docs": "https://platform.openai.com/docs/api-reference/chat"},
    "openrouter": {"title": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "key": "OPENROUTER_API_KEY", "needs_key": True, "default_model": "",
                   "reasoning": ("minimal", "low", "medium", "high"), "docs": "https://openrouter.ai/docs"},
    "local": {"title": "Local model (Ollama, LM Studio, vLLM)", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1",
              "key": "LOCAL_AI_API_KEY", "needs_key": False, "default_model": "", "reasoning": (), "docs": ""},
    "openai_compatible": {"title": "Other OpenAI-compatible endpoint", "kind": "openai", "base_url": "",
                          "key": "COMPATIBLE_AI_API_KEY", "needs_key": False, "default_model": "", "reasoning": (),
                          "docs": ""},
}
PROFILE_FIELDS = ("enabled", "model", "base_url", "temperature", "max_output", "reasoning", "timeout")


class ProviderError(RuntimeError):
    """``retryable``: another provider may succeed (unreachable, overloaded, rate limited, misconfigured key)."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    native: list[dict] = field(default_factory=list)   # provider-native messages to append to the transcript
    stop: str = "end"                                    # end | tool_use | refusal | max_tokens
    note: str = ""
    usage: dict = field(default_factory=dict)            # prompt/completion/cached/reasoning/total tokens, cost (actual)
    model: str = ""                                      # the model that answered (a router may pick another)


# --------------------------------------------------------------------------- configuration

def _stored_profiles(cfg) -> dict:
    db = getattr(cfg, "db", None)
    raw = db.get_setting(PROFILES_KEY) if db is not None else None
    return raw if isinstance(raw, dict) else {}


def clean_profile(pid: str, data: dict) -> dict:
    if pid not in PROVIDERS:
        raise ValueError(f"Unknown AI provider {pid!r}")
    out: dict = {}
    for k in PROFILE_FIELDS:
        if k not in data or data[k] in (None, ""):
            continue
        v = data[k]
        if k == "enabled":
            out[k] = bool(v)
        elif k == "model":
            v = str(v).strip()
            if len(v) > 200 or any(c.isspace() for c in v):
                raise ValueError("Model id: no spaces, at most 200 characters")
            out[k] = v
        elif k == "base_url":
            v = str(v).strip().rstrip("/")
            if not v.startswith(("http://", "https://")) or len(v) > 300:
                raise ValueError("The endpoint must start with http:// or https://")
            out[k] = v
        elif k == "temperature":
            f = float(v)
            if not 0 <= f <= 2:
                raise ValueError("Temperature must be between 0 and 2")
            out[k] = f
        elif k == "max_output":
            n = int(v)
            if not 64 <= n <= 128_000:
                raise ValueError("Maximum output must be between 64 and 128000 tokens")
            out[k] = n
        elif k == "reasoning":
            v = str(v)
            if v not in ("off", *PROVIDERS[pid]["reasoning"]):
                raise ValueError(f"Reasoning for {PROVIDERS[pid]['title']}: off or one of "
                                 f"{', '.join(PROVIDERS[pid]['reasoning']) or '(not supported)'}")
            out[k] = v
        elif k == "timeout":
            f = float(v)
            if not 5 <= f <= 900:
                raise ValueError("Timeout must be between 5 and 900 seconds")
            out[k] = f
    return out


def save_profile(cfg, pid: str, data: dict) -> dict:
    profiles = _stored_profiles(cfg)
    profiles[pid] = clean_profile(pid, data)
    cfg.db.set_setting(PROFILES_KEY, profiles)
    return profile(cfg, pid)


def primary_id(cfg) -> str:
    p = str(cfg.get("ai_provider") or "none")
    return p if p in PROVIDERS else "none" if p == "none" else p


def failover_order(cfg) -> list[str]:
    raw = cfg.db.get_setting(FAILOVER_KEY) if getattr(cfg, "db", None) is not None else None
    items = raw if isinstance(raw, list) else [x.strip() for x in str(raw or "").split(",")]
    return [x for x in items if x in PROVIDERS]


def profile(cfg, pid: str) -> dict:
    """Everything about one provider: settings, whether it is ready, why not, and where data goes."""
    spec = PROVIDERS[pid]
    stored = _stored_profiles(cfg).get(pid, {})
    is_primary = primary_id(cfg) == pid
    legacy = (lambda k: str(cfg.get(k) or "").strip()) if is_primary else (lambda k: "")
    model = stored.get("model") or legacy("ai_model") or spec["default_model"]
    base = (stored.get("base_url") or legacy("ai_base_url") or spec["base_url"]).rstrip("/")
    key = cfg.credential(spec["key"]) or (cfg.credential(AI_KEY) if is_primary else None)
    reasoning = stored.get("reasoning") or (legacy("ai_effort") if spec["kind"] == "anthropic" else "") or \
        ("medium" if spec["kind"] == "anthropic" else "off")
    enabled = stored.get("enabled", True)
    problem, sdk = "", True
    if spec["kind"] == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            sdk = False
            problem = "The Anthropic provider needs the anthropic package: pip install anthropic"
        destination = (urlsplit(base).hostname or ANTHROPIC_HOST) if base else ANTHROPIC_HOST
    else:
        destination = urlsplit(base).hostname or ""
    if not problem:
        if not enabled:
            problem = "Disabled"
        elif spec["kind"] != "anthropic" and not base.startswith(("http://", "https://")):
            problem = "Set the endpoint (e.g. http://127.0.0.1:11434/v1)"
        elif not model:
            problem = "Set the model name"
        elif spec["needs_key"] and not key:
            problem = f"Add your {spec['title']} API key"
    return {"id": pid, "title": spec["title"], "kind": spec["kind"], "enabled": enabled, "model": model, "base_url": base,
            "key_name": spec["key"], "key_set": bool(key), "sdk_installed": sdk, "destination": destination,
            "local": destination in ("127.0.0.1", "localhost", "::1"), "ready": not problem, "problem": problem,
            "temperature": stored.get("temperature"), "max_output": stored.get("max_output") or MAX_TOKENS,
            "reasoning": reasoning, "reasoning_options": ["off", *spec["reasoning"]], "timeout": stored.get("timeout") or 180.0,
            "primary": is_primary, "docs": spec["docs"],
            "capabilities": capabilities_of(pid)}


def capabilities_of(pid: str) -> dict:
    kind = PROVIDERS[pid]["kind"]
    return {"chat": True, "tool_calling": True, "structured_output": True, "streaming": False,
            "reasoning": bool(PROVIDERS[pid]["reasoning"]), "token_usage": True,
            "cost_metadata": "actual (billed amount per request)" if pid == "openrouter" else
            "estimated from the pricing table", "model_listing": kind == "openai"}


def providers(cfg) -> list[dict]:
    order = failover_order(cfg)
    out = [profile(cfg, pid) for pid in PROVIDERS]
    for p in out:
        p["failover_position"] = order.index(p["id"]) + 1 if p["id"] in order else None
    return out


SENDS = ("your chat messages and the findings of this investigation (identifiers, profiles, evidence). Local-only data "
         "(the year of birth) and API keys are never sent.")


def config(cfg) -> dict:
    """The primary provider as the chat shows it (and the failover chain behind it)."""
    pid = primary_id(cfg)
    chain = [profile(cfg, p) for p in failover_order(cfg) if p != pid]
    if pid == "none" or pid not in PROVIDERS:
        unknown = pid not in ("none", *PROVIDERS)
        return {"provider": pid, "model": "", "base_url": "", "key_set": bool(cfg.credential(AI_KEY)), "sdk_installed": True,
                "destination": "", "local": False, "ready": False, "problem": f"Unknown provider {pid!r}" if unknown else "",
                "effort": str(cfg.get("ai_effort") or "medium"), "sends": SENDS,
                "failover": [{"id": c["id"], "title": c["title"], "ready": c["ready"], "model": c["model"]} for c in chain],
                "any_ready": any(c["ready"] for c in chain)}
    p = profile(cfg, pid)
    return {"provider": pid, "title": p["title"], "model": p["model"], "base_url": p["base_url"], "key_set": p["key_set"],
            "sdk_installed": p["sdk_installed"], "destination": p["destination"], "local": p["local"], "ready": p["ready"],
            "problem": p["problem"], "effort": p["reasoning"], "sends": SENDS, "provider_id": pid,
            "failover": [{"id": c["id"], "title": c["title"], "ready": c["ready"], "model": c["model"]} for c in chain],
            "any_ready": p["ready"] or any(c["ready"] for c in chain)}


def build(cfg, pid: str) -> "AIProvider":
    p = profile(cfg, pid)
    if not p["ready"]:
        raise ProviderError(f"{p['title']}: {p['problem']}")
    spec = PROVIDERS[pid]
    key = cfg.credential(spec["key"]) or (cfg.credential(AI_KEY) if p["primary"] else None)
    if spec["kind"] == "anthropic":
        return AnthropicProvider(key, p["model"], "" if p["base_url"] == spec["base_url"] else p["base_url"],
                                 p["reasoning"], max_output=p["max_output"], timeout=p["timeout"])
    return OpenAICompatibleProvider(key, p["model"], p["base_url"], provider_id=pid, temperature=p["temperature"],
                                    max_output=p["max_output"], reasoning=p["reasoning"], timeout=p["timeout"])


def make(cfg) -> "AIProvider":
    """The primary provider (raises when it is not ready)."""
    c = config(cfg)
    if not c["ready"]:
        raise ProviderError(c["problem"] or "No AI provider is configured (Settings → AI providers)", retryable=False)
    return build(cfg, c["provider"])


def fallbacks(cfg, exclude: str | None = None) -> list[tuple[str, str]]:
    """(provider id, why it is not usable or "") for the configured failover chain, primary excluded. Disabled or
    unconfigured providers are listed with the reason and never called."""
    out = []
    for pid in failover_order(cfg):
        if pid == exclude or pid == primary_id(cfg):
            continue
        p = profile(cfg, pid)
        out.append((pid, "" if p["ready"] else p["problem"]))
    return out


# --------------------------------------------------------------------------- the interface

class AIProvider:
    provider_id = "base"
    id = "base"                    # transcript namespace (kept for compatibility: equals provider_id)
    model = ""
    destination = ""

    @property
    def capabilities(self) -> dict:
        return capabilities_of(self.provider_id) if self.provider_id in PROVIDERS else {}

    def user_message(self, text: str) -> dict:
        raise NotImplementedError

    def assistant_text(self, text: str) -> dict:
        raise NotImplementedError

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict]:
        raise NotImplementedError

    async def complete(self, system: str, transcript: list[dict], tools: list[dict]) -> Reply:
        raise NotImplementedError


class AnthropicProvider(AIProvider):
    provider_id = id = "anthropic"

    def __init__(self, key: str, model: str, base_url: str = "", effort: str = "medium", *, max_output: int = MAX_TOKENS,
                 timeout: float = 180.0) -> None:
        import anthropic
        self._sdk = anthropic
        self.model = model
        self.effort = effort if effort in ("low", "medium", "high", "xhigh", "max") else "medium"
        self.max_output = int(max_output or MAX_TOKENS)
        self.first_party = not base_url
        self.destination = urlsplit(base_url).hostname if base_url else ANTHROPIC_HOST
        self.client = anthropic.AsyncAnthropic(api_key=key, base_url=base_url or None, max_retries=2, timeout=timeout)

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def assistant_text(self, text: str) -> dict:
        return {"role": "assistant", "content": [{"type": "text", "text": text or "(no answer)"}]}

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict]:
        # all results of one turn go back in a single user message
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": out,
                                              **({"is_error": True} if err else {})} for c, out, err in results]}]

    async def complete(self, system: str, transcript: list[dict], tools: list[dict]) -> Reply:
        a = self._sdk
        kwargs: dict = {"model": self.model, "max_tokens": self.max_output, "system": system, "messages": transcript,
                        "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                                  for t in tools]}
        if "haiku" not in self.model:
            kwargs["output_config"] = {"effort": self.effort}
        if self.first_party and self.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            resp = await self.client.beta.messages.create(**kwargs)
        except a.AuthenticationError:
            raise ProviderError("The Anthropic API rejected the API key") from None
        except a.PermissionDeniedError:
            raise ProviderError("The API key is not allowed to use this model") from None
        except a.NotFoundError:
            raise ProviderError(f"Unknown model {self.model!r}") from None
        except a.RateLimitError:
            raise ProviderError("The Anthropic API is rate limiting; try again shortly") from None
        except a.APIStatusError as exc:
            raise ProviderError(f"The Anthropic API answered HTTP {exc.status_code}: {str(exc.message)[:200]}",
                                retryable=exc.status_code >= 500 or exc.status_code in (408, 409, 429)) from None
        except a.APIConnectionError:
            raise ProviderError("Could not reach the Anthropic API") from None
        usage = _anthropic_usage(getattr(resp, "usage", None))
        model = str(getattr(resp, "model", "") or self.model)
        if resp.stop_reason == "refusal":
            det = getattr(resp, "stop_details", None)
            cat = getattr(det, "category", None) if det else None
            return Reply(text="The AI provider declined this request" + (f" ({cat})" if cat else "") + ". Nothing was run.",
                         stop="refusal", usage=usage, model=model)
        content = [b.to_dict(mode="json") for b in resp.content]
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        stop = "tool_use" if calls else "max_tokens" if resp.stop_reason == "max_tokens" else "end"
        return Reply(text=text, tool_calls=calls, native=[{"role": "assistant", "content": content}], stop=stop,
                     note="The answer was cut off (output limit)." if stop == "max_tokens" else "", usage=usage, model=model)


def _anthropic_usage(u) -> dict:
    if u is None:
        return {}
    g = (lambda k: int(getattr(u, k, 0) or 0)) if not isinstance(u, dict) else (lambda k: int(u.get(k) or 0))
    uncached, read, write, out = g("input_tokens"), g("cache_read_input_tokens"), g("cache_creation_input_tokens"), \
        g("output_tokens")
    # input_tokens excludes cached tokens: the prompt is all three together
    return {"prompt_tokens": uncached + read + write, "cached_tokens": read, "cache_write_tokens": write,
            "completion_tokens": out, "reasoning_tokens": 0, "total_tokens": uncached + read + write + out}


class OpenAICompatibleProvider(AIProvider):
    """``/chat/completions`` (OpenAI, OpenRouter, local servers, other compatible endpoints)."""

    def __init__(self, key: str | None, model: str, base_url: str, *, provider_id: str = "openai_compatible",
                 temperature: float | None = None, max_output: int | None = None, reasoning: str = "off",
                 timeout: float = 180.0) -> None:
        self.key = key
        self.model = model
        self.base = base_url.rstrip("/")
        self.destination = urlsplit(base_url).hostname or ""
        self.provider_id = self.id = provider_id
        self.temperature = temperature
        self.max_output = max_output
        self.reasoning = reasoning or "off"
        self.timeout = timeout

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def assistant_text(self, text: str) -> dict:
        return {"role": "assistant", "content": text or "(no answer)"}

    def tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": c.id, "content": ("ERROR: " if err else "") + out} for c, out, err in results]

    def request_body(self, system: str, transcript: list[dict], tools: list[dict]) -> dict:
        body: dict = {"model": self.model, "messages": [{"role": "system", "content": system}, *transcript]}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                "parameters": t["schema"]}} for t in tools]
            body["tool_choice"] = "auto"
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_output:
            body["max_completion_tokens" if self.provider_id == "openai" else "max_tokens"] = int(self.max_output)
        if self.reasoning and self.reasoning != "off":
            if self.provider_id == "openrouter":
                body["reasoning"] = {"effort": self.reasoning}
            else:
                body["reasoning_effort"] = self.reasoning
        if self.provider_id == "openrouter":
            body["usage"] = {"include": True}          # OpenRouter reports the billed cost of the request
        return body

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        if self.provider_id == "openrouter":
            h["X-Title"] = "Sextant"
        return h

    def _client(self):
        from ..sdk.http import SafeHttpClient
        return SafeHttpClient(allow_private_hosts=frozenset({self.destination}), timeout=self.timeout, max_bytes=8_000_000)

    async def complete(self, system: str, transcript: list[dict], tools: list[dict]) -> Reply:
        from ..sdk.http import FetchError
        http = self._client()
        try:
            resp = await http.request("POST", f"{self.base}/chat/completions", connector="ai_assistant",
                                      headers=self.headers(), content=json.dumps(self.request_body(system, transcript, tools)).encode(),
                                      cache_ttl=None, retries=1)
        except FetchError as exc:
            raise ProviderError(f"Could not reach {self.destination}: {exc}") from None
        finally:
            await http.aclose()
        if resp.status != 200:
            retry = resp.status >= 500 or resp.status in (401, 402, 403, 408, 429)
            raise ProviderError(f"{self.destination} answered HTTP {resp.status}: {resp.text[:200]}", retryable=retry)
        try:
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.destination} returned an unexpected answer") from None
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except (ValueError, KeyError, TypeError):
                args = {"_invalid_arguments": True}
            calls.append(ToolCall(str(tc.get("id")), tc["function"]["name"], args if isinstance(args, dict) else {}))
        native = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            native["tool_calls"] = msg["tool_calls"]
        finish = choice.get("finish_reason")
        stop = "tool_use" if calls else "max_tokens" if finish == "length" else "refusal" if finish == "content_filter" else "end"
        return Reply(text=(msg.get("content") or "").strip(), tool_calls=calls, native=[native], stop=stop,
                     note="The answer was cut off (output limit)." if stop == "max_tokens" else
                     "The provider's content filter stopped the answer." if stop == "refusal" else "",
                     usage=openai_usage(data.get("usage")), model=str(data.get("model") or self.model))

    async def list_models(self) -> list[dict]:
        """The models the endpoint offers now (``GET /models``), with published prices when the endpoint gives them
        (OpenRouter does: USD per token)."""
        from ..sdk.http import FetchError
        http = self._client()
        try:
            resp = await http.request("GET", f"{self.base}/models", connector="ai_assistant", headers=self.headers(),
                                      cache_ttl=None, retries=1)
        except FetchError as exc:
            raise ProviderError(f"Could not reach {self.destination}: {exc}") from None
        finally:
            await http.aclose()
        if resp.status != 200:
            raise ProviderError(f"{self.destination} answered HTTP {resp.status}: {resp.text[:200]}")
        out = []
        for m in (resp.json() or {}).get("data") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            pr = m.get("pricing") or {}
            row = {"id": str(m["id"])[:200], "name": str(m.get("name") or m["id"])[:200],
                   "context_length": m.get("context_length"),
                   "supports_tools": "tools" in (m.get("supported_parameters") or []) if m.get("supported_parameters")
                   else None,
                   "supports_reasoning": "reasoning" in (m.get("supported_parameters") or []) if m.get("supported_parameters")
                   else None}
            for src, dst in (("prompt", "input_per_mtok"), ("completion", "output_per_mtok"),
                             ("input_cache_read", "cached_input_per_mtok"), ("internal_reasoning", "reasoning_per_mtok"),
                             ("request", "per_request")):
                try:
                    if pr.get(src) not in (None, ""):
                        v = float(pr[src])
                        row[dst] = v if dst == "per_request" else round(v * 1_000_000, 6)
                except (TypeError, ValueError):
                    pass
            out.append(row)
        return out


def openai_usage(u) -> dict:
    if not isinstance(u, dict):
        return {}
    prompt, completion = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    out = {"prompt_tokens": prompt, "completion_tokens": completion,
           "cached_tokens": int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
           "reasoning_tokens": int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
           "total_tokens": int(u.get("total_tokens") or prompt + completion)}
    if u.get("cost") is not None:
        try:
            out["cost"] = float(u["cost"])           # OpenRouter: the billed amount in USD (credits)
        except (TypeError, ValueError):
            pass
    return out


__all__ = ["AIProvider", "AnthropicProvider", "OpenAICompatibleProvider", "ProviderError", "Reply", "ToolCall", "PROVIDERS",
           "config", "make", "build", "fallbacks", "providers", "profile", "save_profile", "failover_order", "primary_id",
           "AI_KEY", "openai_usage"]
