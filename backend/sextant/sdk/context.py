"""TransformContext — everything a running transform may use.

Network access goes exclusively through ``ctx.get/post/download``: the guarded client with the
connector's rate limiter, the investigation's request budget and per-investigation cache.
Credentials are read through ``ctx.credential`` (environment > encrypted store) and never logged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..model import utcnow

from .errors import Blocked, NeedsCredentials, NotFound, RateLimited, SourceUnavailable, VerificationRequired
from .http import HttpResponse, RateLimiter, SafeHttpClient, is_verification_challenge
from .manifest import Manifest
from .result import TransformResult
from .transform import EntityView, InvestigationInfo


RAW_BODY_CAP = 1_000_000        # bytes of one raw response kept as evidence
RAW_KEEP = 24                   # bodies kept per run (the newest); digests are kept for every fetch


@dataclass
class FetchRecord:
    """One response a transform received — its digest is the raw-evidence reference of the facts drawn from it."""
    method: str
    url: str
    status: int
    content_type: str
    size: int
    digest: str
    at: str
    body: bytes | None = None


def _same_url(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        u = u.split("#", 1)[0].rstrip("/")
        u = u.split("://", 1)[-1]
        return u[4:] if u.startswith("www.") else u
    return norm(a).lower() == norm(b).lower()


class TransformContext:
    def __init__(self, *, transform: str, manifest: Manifest, entity: EntityView, info: InvestigationInfo,
                 http: SafeHttpClient, limiter: RateLimiter | None, credentials: Callable[[str], str | None],
                 settings: Callable[[str], Any], out: TransformResult, run_id: str = "",
                 data_dir: Path | None = None, counters: dict | None = None, journal: list | None = None) -> None:
        self.transform = transform
        self.manifest = manifest
        self.entity = entity
        self.info = info
        self.http = http
        self.limiter = limiter
        self._credentials = credentials
        self._settings = settings
        self.out = out
        self.run_id = run_id
        self.data_dir = data_dir
        self._counters = counters if counters is not None else {}
        self._journal = journal if journal is not None else []
        self.fetches: list[FetchRecord] = []
        self.challenge_url: str | None = None        # first response that was a CAPTCHA / bot check, if any

    # ------------------------------------------------------------------ network
    async def get(self, url: str, *, evidence: bool = True, **kw) -> HttpResponse:
        """``evidence=False`` for downloads that are not facts (datasets, tool databases): not kept as raw evidence."""
        resp = await self.http.request("GET", url, connector=self.manifest.name, limiter=self.limiter, **kw)
        return self._log("GET", resp) if evidence else resp

    async def post(self, url: str, *, evidence: bool = True, **kw) -> HttpResponse:
        kw.setdefault("cache_ttl", None)
        resp = await self.http.request("POST", url, connector=self.manifest.name, limiter=self.limiter, **kw)
        return self._log("POST", resp) if evidence else resp

    def _log(self, method: str, resp: HttpResponse) -> HttpResponse:
        content = resp.content or b""
        if self.challenge_url is None and is_verification_challenge(resp.status, resp.headers, content):
            self.challenge_url = resp.url
        rec = FetchRecord(method, resp.url, resp.status, resp.headers.get("content-type", ""), len(content),
                          hashlib.sha256(content).hexdigest(), utcnow(), content[:RAW_BODY_CAP])
        self.fetches.append(rec)
        if len(self.fetches) > RAW_KEEP:
            self.fetches[-RAW_KEEP - 1].body = None
        if len(self.fetches) > 5000:
            del self.fetches[:1000]
        return resp

    def verification_outcome(self, status: str, message: str, url: str | None = None) -> tuple[str, str, str | None]:
        """A run that failed after a CAPTCHA / bot-check page is reported as *verification required*, whichever way the
        connector handled the refusal. Returns (status, message, url of the page or None)."""
        url = url or self.challenge_url
        if url is None or status not in ("blocked", "rate_limited", "error", "timeout"):
            return status, message, None
        if not message.startswith("Verification required"):
            message = ("Verification required — manual action needed: the source answered with a CAPTCHA or bot "
                       f"check ({message}). Open it in your browser; Sextant does not try to get past it.")
        return "blocked", message, url

    def raw_evidence(self) -> dict[str, FetchRecord]:
        """Links each source of the result to the raw responses it was drawn from (sets ``raw_refs``) and returns
        the responses to keep, by digest. A source matches the responses fetched from its own URL; an API source
        whose URL is a human-readable page matches the run's successful responses when there are only a few."""
        keep: dict[str, FetchRecord] = {}
        ok = [f for f in self.fetches if 200 <= f.status < 300 and f.body is not None]
        for draft in self.out.sources.values():
            refs = [f for f in self.fetches if draft.url and _same_url(f.url, draft.url) and f.body is not None]
            if not refs and len(ok) <= 3:
                refs = ok
            if refs:
                draft.raw_refs = tuple(dict.fromkeys(f.digest for f in refs))[:4]
                for f in refs:
                    keep[f.digest] = f
        return keep

    async def download(self, url: str, dest: Path, **kw) -> int:
        return await self.http.download(url, dest, connector=self.manifest.name, limiter=self.limiter, **kw)

    def journal(self, entry: dict) -> None:
        """Appends to the investigation's search journal (what was searched, where, with what result). It answers
        "which searches did you perform?" and drives the discovery stages; capped so it cannot grow without bound."""
        self._journal.append({"at": utcnow(), "connector": self.manifest.name, "transform": self.transform,
                              "run_id": self.run_id, **entry})
        if len(self._journal) > 1000:
            del self._journal[:200]

    def take(self, quota: str, cap: int) -> bool:
        """Consumes one unit of a per-investigation quota (e.g. search queries). False when exhausted."""
        key = f"{self.manifest.name}:{quota}"
        if self._counters.get(key, 0) >= cap:
            return False
        self._counters[key] = self._counters.get(key, 0) + 1
        return True

    # ------------------------------------------------------------------ configuration
    def credential(self, key: str) -> str | None:
        v = self._credentials(key)
        return str(v) if v not in (None, "") else None

    def require(self, *keys: str) -> list[str]:
        values = [self.credential(k) for k in keys]
        missing = [k for k, v in zip(keys, values) if not v]
        if missing:
            raise NeedsCredentials(f"{self.manifest.title} needs: {', '.join(missing)} (Data Sources → Configure)")
        return [v for v in values if v]

    def setting(self, key: str, default: Any = None) -> Any:
        v = self._settings(key)
        return default if v in (None, "") else v

    def flag(self, key: str, default: bool = False) -> bool:
        v = self._settings(key)
        if v in (None, ""):
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------------ response helpers
    def expect(self, resp: HttpResponse, *, ok: tuple[int, ...] = (200,), not_found: tuple[int, ...] = (404,),
               what: str = "") -> HttpResponse:
        """Maps HTTP outcomes to honest run statuses. A CAPTCHA / bot-check page is reported as *verification
        required* (a manual step for the investigator), never retried around."""
        what = what or self.manifest.title
        if is_verification_challenge(resp.status, resp.headers, resp.content):
            raise VerificationRequired(f"Verification required — manual action needed: {what} answered with a CAPTCHA or "
                                       "bot check. Open it in your browser; Sextant does not try to get past it.",
                                       url=resp.url)
        if resp.status in ok:
            return resp
        if resp.status in not_found:
            raise NotFound(f"{what}: not found")
        if resp.status == 401:
            raise NeedsCredentials(f"{what} rejected the credentials (HTTP 401)")
        if resp.status == 403:
            raise Blocked(f"{what} refused the request (HTTP 403) — not retried around")
        if resp.status == 429:
            raise RateLimited(f"{what} is rate limiting (HTTP 429)")
        raise SourceUnavailable(f"{what} answered HTTP {resp.status}")

    @staticmethod
    def json(resp: HttpResponse) -> Any:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            raise SourceUnavailable("The source returned an invalid or truncated response") from None
