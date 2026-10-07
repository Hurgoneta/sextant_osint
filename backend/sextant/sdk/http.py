"""SafeHttpClient — the only way connectors talk to the network.

Responsibilities
* SSRF protection: http/https only, ports 80/443, and every resolved IP must be public.
  The IP check happens *at connect time* inside a custom httpcore network backend, so a
  hostname can't pass validation and then re-resolve to 127.0.0.1 (DNS-rebinding TOCTOU).
  Redirects are followed manually and each hop is re-validated.
* Responsible collection: per-connector token buckets, a global concurrency cap,
  retries with exponential backoff + jitter that honour ``Retry-After``.
* Hygiene: size caps, timeouts, no proxy env vars, honest User-Agent, per-investigation cache.
* Budgets: an optional ``budget`` hook is called before every network request so an investigation's
  request ceiling is enforced at the lowest level (cache hits are free).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import random
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from .. import USER_AGENT

MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF = 30.0
PATIENT_BACKOFF = 120.0           # "enhanced automation": wait longer for a provider's Retry-After
PATIENT_RETRIES = 4

# Pages that ask a human to prove they are one. Sextant detects them only to report "verification required" and stop;
# it never solves, bypasses or retries around them.
_CHALLENGE_STRONG = (b"challenge-platform", b"cf_chl_opt", b"cf-chl-", b"captcha-delivery.com", b"px-captcha",
                     b"<title>just a moment...</title>", b"challenges.cloudflare.com", b"/cdn-cgi/challenge")
_CHALLENGE_WEAK = (b"g-recaptcha", b"recaptcha/api", b"hcaptcha.com", b"h-captcha", b"cf-turnstile", b"turnstile",
                   b"attention required! | cloudflare", b"verify you are human", b"are you a robot", b"unusual traffic",
                   b"datadome", b"perimeterx", b"captcha")


def is_verification_challenge(status: int, headers: dict, content: bytes) -> bool:
    """True when a response is a CAPTCHA / bot-check / browser-challenge page rather than the source's answer.
    Refusals (403/429/503) are checked against a broad list; a 200 page only against interstitial-specific markers,
    so an ordinary page that embeds a CAPTCHA widget (a comment form) is not mistaken for a challenge."""
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    body = (content or b"")[:200_000].lower()
    if any(m in body for m in _CHALLENGE_STRONG):
        return True
    return status in (403, 429, 503) and any(m in body for m in _CHALLENGE_WEAK)


class FetchError(Exception):
    """Raised when a request can't be completed. ``kind`` drives the user-facing status."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind          # blocked_url | network | timeout | too_many_redirects | rate_limited | budget


SENSITIVE_HEADERS = frozenset({
    "authorization", "x-subscription-token", "hibp-api-key", "x-api-key", "x-apikey", "api-key", "apikey", "key",
    "token", "x-auth-token", "client-id", "x-client-id", "x-goog-api-key",
})


# --------------------------------------------------------------------------- IP / URL policy

def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_url(url: str, allow_private_hosts: frozenset[str] = frozenset()) -> str:
    """Static checks before any DNS lookup. Returns the URL or raises FetchError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("blocked_url", f"Malformed URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise FetchError("blocked_url", f"Scheme '{parts.scheme}' is not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise FetchError("blocked_url", "URL has no host")
    if parts.username or parts.password:
        raise FetchError("blocked_url", "Credentials in URLs are not allowed")
    if host in allow_private_hosts:
        return url
    if port not in (None, 80, 443):
        raise FetchError("blocked_url", f"Port {port} is not allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise FetchError("blocked_url", f"Host '{host}' is not a public host")
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal and not is_public_ip(host.strip("[]")):
        raise FetchError("blocked_url", f"Address {host} is not public")
    return url


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, rejects non-public IPs, then connects to the vetted IP."""

    def __init__(self, allow_private_hosts: frozenset[str] = frozenset(), resolver=None) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._allow = allow_private_hosts
        self._resolver = resolver  # injectable for tests: async (host, port) -> list[str]

    async def _resolve(self, host: str, port: int) -> list[str]:
        if self._resolver:
            return await self._resolver(host, port)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out: list[str] = []
        for info in infos:
            addr = info[4][0]
            if addr not in out:
                out.append(addr)
        return out

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host = host.decode() if isinstance(host, bytes) else host
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"DNS resolution failed for {host}: {exc}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"No addresses for {host}")
        if host.lower() not in self._allow:
            bad = [a for a in addresses if not is_public_ip(a)]
            if bad:
                raise httpcore.ConnectError(f"Blocked: {host} resolves to non-public address {bad[0]}")
        last: Exception | None = None
        for addr in addresses:
            try:
                return await self._inner.connect_tcp(addr, port, timeout=timeout,
                                                     local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        raise httpcore.ConnectError(str(last))

    async def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - never allowed
        raise httpcore.ConnectError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    # Python 3.13 adds VERIFY_X509_STRICT, which rejects valid certificates of many government sites (e.g. a missing
    # Subject Key Identifier). The chain and the host name are still verified, as browsers and curl do.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


class GuardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, allow_private_hosts: frozenset[str] = frozenset(), resolver=None) -> None:
        super().__init__()
        # Replace the default pool with one that uses the guarded network backend.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_ssl_context(), max_connections=20, max_keepalive_connections=10,
            keepalive_expiry=30.0, http1=True, http2=False,
            network_backend=GuardedBackend(allow_private_hosts, resolver),
        )


# --------------------------------------------------------------------------- rate limiting

class RateLimiter:
    """Async token bucket: ``per_second`` steady rate with ``burst`` capacity."""

    def __init__(self, per_second: float, burst: int = 1) -> None:
        self.rate = max(per_second, 0.001)
        self.capacity = max(burst, 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(name: str, per_second: float, burst: int) -> RateLimiter:
    """Process-wide limiter per source, shared across concurrent investigations (of one event loop: a lock cannot be
    shared across loops, so a new loop — a restarted server, a test — gets a fresh limiter)."""
    lim = _LIMITERS.get(name)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if lim is None or lim.rate != per_second or lim.capacity != burst or (loop is not None and lim.loop is not loop):
        lim = _LIMITERS[name] = RateLimiter(per_second, burst)
    return lim


# --------------------------------------------------------------------------- responses / cache

@dataclass
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    from_cache: bool = False
    truncated: bool = False

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if self.truncated:
            raise ValueError("Response was truncated; refusing to parse partial JSON")
        return json.loads(self.content or b"null")

    def to_cache(self) -> dict:
        return {"status": self.status, "url": self.url, "headers": self.headers,
                "content": base64.b64encode(self.content).decode(), "truncated": self.truncated}

    @classmethod
    def from_cache_dict(cls, d: dict) -> "HttpResponse":
        return cls(status=d["status"], url=d["url"], headers=d.get("headers") or {},
                   content=base64.b64decode(d.get("content") or ""), from_cache=True,
                   truncated=bool(d.get("truncated")))


class HttpCache(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def set(self, key: str, value: dict, ttl: int) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, dict]] = {}

    async def get(self, key: str) -> dict | None:
        item = self._data.get(key)
        if not item or item[0] < time.time():
            return None
        return item[1]

    async def set(self, key: str, value: dict, ttl: int) -> None:
        self._data[key] = (time.time() + ttl, value)


_KEEP_HEADERS = ("content-type", "retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "location", "date")


# --------------------------------------------------------------------------- client

class SafeHttpClient:
    def __init__(self, *, cache: HttpCache | None = None, allow_private_hosts: frozenset[str] = frozenset(),
                 transport: httpx.AsyncBaseTransport | None = None, user_agent: str = USER_AGENT,
                 timeout: float = 20.0, max_bytes: int = MAX_BYTES, max_concurrency: int = 8,
                 sleep=asyncio.sleep, budget: Callable[[str], None] | None = None, patient: bool = False) -> None:
        self.budget = budget          # called with the connector name before each network request; may raise
        self.patient = patient        # enhanced automation: more retries and longer Retry-After waits on 429/5xx
        self.allow_private_hosts = frozenset(h.lower() for h in allow_private_hosts)
        self._client = httpx.AsyncClient(
            transport=transport or GuardedTransport(self.allow_private_hosts),
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False, trust_env=False,
            headers={"User-Agent": user_agent, "Accept-Language": "en"},
        )
        self.cache = cache
        self.max_bytes = max_bytes
        self._sem = asyncio.Semaphore(max_concurrency)
        self._sleep = sleep
        self.requests: dict[str, int] = {}
        self.total_requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SafeHttpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @staticmethod
    def cache_key(method: str, url: str, body: bytes | None) -> str:
        return f"{method} {url} {base64.b64encode(body or b'').decode()[:2048]}"

    async def request(self, method: str, url: str, *, connector: str = "", limiter: RateLimiter | None = None,
                      params: dict | None = None, headers: dict | None = None, data: dict | None = None,
                      content: bytes | None = None, files: dict | None = None, cache_ttl: int | None = 86_400,
                      retries: int = 2, max_bytes: int | None = None, follow_redirects: bool = True) -> HttpResponse:
        full_url = str(httpx.URL(url, params=params)) if params else url
        validate_url(full_url, self.allow_private_hosts)
        body = content if content is not None else (str(httpx.QueryParams(data)).encode() if data else None)
        ckey = self.cache_key(method, full_url, body) + ("" if follow_redirects else " noredirect")
        if self.cache and cache_ttl and method == "GET":
            cached = await self.cache.get(ckey)
            if cached:
                return HttpResponse.from_cache_dict(cached)

        attempt = 0
        while True:
            try:
                async with self._sem:
                    if self.budget is not None:
                        self.budget(connector)
                    if limiter:
                        await limiter.acquire()
                    self.requests[connector] = self.requests.get(connector, 0) + 1
                    self.total_requests += 1
                    resp = await self._send_following(method, full_url, headers or {}, data, content,
                                                      max_bytes or self.max_bytes, follow_redirects, files)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                err = self._classify(exc)
                if err.kind == "blocked_url" or attempt >= retries:
                    raise err from None
                await self._sleep(self._backoff(attempt, None))
                attempt += 1
                continue
            limit = max(retries, PATIENT_RETRIES) if self.patient else retries
            if resp.status in RETRY_STATUSES and attempt < limit and \
                    not is_verification_challenge(resp.status, resp.headers, resp.content):
                await self._sleep(self._backoff(attempt, resp.headers.get("retry-after"),
                                                PATIENT_BACKOFF if self.patient else MAX_BACKOFF))
                attempt += 1
                continue
            if resp.status == 429:
                raise FetchError("rate_limited", "Source is rate limiting requests (HTTP 429)")
            if self.cache and cache_ttl and method == "GET" and resp.status in (200, 404, 410) and not resp.truncated:
                await self.cache.set(ckey, resp.to_cache(), cache_ttl)
            return resp

    @staticmethod
    def _classify(exc: Exception) -> FetchError:
        msg = str(exc)
        if "Blocked:" in msg:
            return FetchError("blocked_url", msg[msg.index("Blocked:"):])
        if isinstance(exc, httpx.TimeoutException):
            return FetchError("timeout", f"Timed out ({exc.__class__.__name__})")
        return FetchError("network", f"Network error: {msg or exc.__class__.__name__}")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None, ceiling: float = MAX_BACKOFF) -> float:
        if retry_after:
            try:
                return min(ceiling, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(ceiling, (2 ** attempt) + random.uniform(0, 0.5))

    async def _send_following(self, method: str, url: str, headers: dict, data: dict | None,
                              content: bytes | None, max_bytes: int, follow: bool = True,
                              files: dict | None = None) -> HttpResponse:
        origin_host = urlsplit(url).hostname
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(url, self.allow_private_hosts)
            req = self._client.build_request(method, url, headers=headers, data=data, content=content, files=files)
            resp = await self._client.send(req, stream=True)
            try:
                if follow and resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    if resp.status_code in (301, 302, 303):
                        method, data, content, files = "GET", None, None, None
                    if urlsplit(url).hostname != origin_host:
                        # never forward credentials (API keys, tokens) to another host
                        headers = {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}
                    continue
                buf, truncated = bytearray(), False
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        del buf[max_bytes:]
                        break
                kept = {k: resp.headers[k] for k in _KEEP_HEADERS if k in resp.headers}
                return HttpResponse(resp.status_code, str(resp.url), kept, bytes(buf), truncated=truncated)
            finally:
                await resp.aclose()
        raise FetchError("too_many_redirects", "Too many redirects")

    async def download(self, url: str, dest: Path, *, connector: str = "", limiter: RateLimiter | None = None,
                       max_bytes: int = 200 * 1024 * 1024, headers: dict | None = None) -> int:
        """Streams a large public file (e.g. a sanctions list) to ``dest``. Same URL policy, budget and
        redirect re-validation as ``request``; nothing is cached in memory. Returns the size in bytes."""
        validate_url(url, self.allow_private_hosts)
        if self.budget is not None:
            self.budget(connector)
        if limiter:
            await limiter.acquire()
        self.requests[connector] = self.requests.get(connector, 0) + 1
        self.total_requests += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        origin_host = urlsplit(url).hostname
        hdrs = dict(headers or {})
        try:
            for _ in range(MAX_REDIRECTS + 1):
                validate_url(url, self.allow_private_hosts)
                req = self._client.build_request("GET", url, headers=hdrs)
                resp = await self._client.send(req, stream=True)
                try:
                    if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        if urlsplit(url).hostname != origin_host:
                            hdrs = {k: v for k, v in hdrs.items() if k.lower() not in SENSITIVE_HEADERS}
                        continue
                    if resp.status_code != 200:
                        raise FetchError("network", f"HTTP {resp.status_code} downloading {urlsplit(url).hostname}")
                    size = 0
                    with open(tmp, "wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchError("network", f"Download larger than {max_bytes // (1024 * 1024)} MB")
                            fh.write(chunk)
                    tmp.replace(dest)
                    return size
                finally:
                    await resp.aclose()
            raise FetchError("too_many_redirects", "Too many redirects")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._classify(exc) from None
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
