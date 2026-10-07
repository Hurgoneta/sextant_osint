"""Testing helpers for connector authors.

    from sextant.sdk.testing import mock_http, run_transform, view

    http = mock_http({"https://api.example.org/users/jdoe": (200, {"login": "jdoe"})})
    out, status, message = run_transform("example.username_lookup", view("username", "jdoe"), http=http)
    assert status == "found" and "account:example:jdoe" in out.entities

Transforms run exactly as in the engine (same context, same error-to-status mapping) but without an
investigation, so parsers and transforms can be tested offline with recorded responses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import httpx

from ..ontology import normalize_value
from .context import TransformContext
from .errors import TransformError
from .http import FetchError, RateLimiter, SafeHttpClient
from .result import TransformResult
from .transform import EntityView, InvestigationInfo, Transform

FETCH_STATUS = {"rate_limited": "rate_limited", "timeout": "timeout", "budget": "budget_exhausted"}


def mock_http(routes: dict[str, tuple], calls: list | None = None, **kw) -> SafeHttpClient:
    """SafeHttpClient whose transport answers from ``routes``: {url_prefix: (status, body[, headers])}.
    The longest matching prefix wins; dict/list bodies are sent as JSON; unknown URLs get 404. A route may also be a
    callable ``request -> (status, body[, headers])`` (e.g. a fake search index answering per query)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(request)
        for prefix in sorted(routes, key=len, reverse=True):
            if url.startswith(prefix):
                entry = routes[prefix]
                if callable(entry):
                    entry = entry(request)
                status, body = entry[0], entry[1]
                headers = dict(entry[2]) if len(entry) > 2 else {}
                if isinstance(body, (dict, list)) or body is None:
                    body = json.dumps(body).encode()
                    headers = {"content-type": "application/json", **headers}
                elif isinstance(body, str):
                    body = body.encode()
                return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(404, content=b"{}", headers={"content-type": "application/json"})

    async def no_sleep(_):
        return None

    return SafeHttpClient(transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)


def view(type_id: str, value: str, *, label: str | None = None, status: str = "seed", **props: Any) -> EntityView:
    """An input entity for a transform (properties are given as keyword arguments)."""
    norm = normalize_value(type_id, value)
    if norm is None:
        raise ValueError(f"{value!r} is not a valid {type_id}")
    return EntityView(key=f"{type_id}:{norm}", type=type_id, value=norm, label=label or value, status=status,
                      props={k: (tuple(v) if isinstance(v, (list, tuple)) else (v,)) for k, v in props.items()})


def run_transform(transform: str | Transform, entity: EntityView, *, http: SafeHttpClient | None = None,
                  info: InvestigationInfo | None = None, credentials: dict[str, str] | Callable[[str], str | None] | None = None,
                  settings: dict[str, Any] | None = None, data_dir: Path | None = None) -> tuple[TransformResult, str, str]:
    """Runs one transform and returns (result, status, message) — statuses as the engine records them."""
    return asyncio.run(arun_transform(transform, entity, http=http, info=info, credentials=credentials,
                                      settings=settings, data_dir=data_dir))


async def arun_transform(transform: str | Transform, entity: EntityView, *, http: SafeHttpClient | None = None,
                         info: InvestigationInfo | None = None,
                         credentials: dict[str, str] | Callable[[str], str | None] | None = None,
                         settings: dict[str, Any] | None = None, data_dir: Path | None = None
                         ) -> tuple[TransformResult, str, str]:
    """``run_transform`` inside a running event loop (the API's "Test connection")."""
    from ..registry import connectors as REG
    REG.load_all()
    t = REG.TRANSFORMS[transform] if isinstance(transform, str) else transform
    manifest = REG.REGISTRY[t.connector].manifest
    creds = credentials if callable(credentials) else (lambda k, c=credentials or {}: c.get(k))
    out = TransformResult(t.name, manifest.name, entity.key, entity.type, entity.display)
    ctx = TransformContext(transform=t.name, manifest=manifest, entity=entity, info=info or InvestigationInfo(),
                           http=http or mock_http({}), limiter=RateLimiter(1000, 1000), credentials=creds,
                           settings=lambda k, s=settings or {}: s.get(k), out=out, run_id="test", data_dir=data_dir,
                           counters={})

    async def go() -> tuple[str, str]:
        try:
            await t.transform(entity, ctx)
        except TransformError as exc:
            return exc.status, exc.message or str(exc)
        except FetchError as exc:
            return FETCH_STATUS.get(exc.kind, "error"), str(exc)
        return out.status or ("found" if out.found_anything else "empty"), out.message

    status, message = await go()
    status, message, _url = ctx.verification_outcome(status, message)
    return out, status, message
