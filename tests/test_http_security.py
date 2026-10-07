"""SSRF guard, redirects, retries, size caps, credential stripping, request budget, streaming downloads."""

import httpcore
import httpx
import pytest

from conftest import mock_http, run
from sextant.sdk.http import FetchError, GuardedBackend, MemoryCache, SafeHttpClient, is_public_ip, validate_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://x", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://192.168.1.1/", "http://user:pw@example.com/",
    "http://example.com:8080/", "http://router.local/", "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/",
])
def test_validate_url_blocks(url):
    with pytest.raises(FetchError) as exc:
        validate_url(url)
    assert exc.value.kind == "blocked_url"


def test_validate_url_allows_public_and_explicit_private_hosts():
    assert validate_url("https://api.github.com/users/x")
    assert validate_url("http://127.0.0.1:8888/search", frozenset({"127.0.0.1"}))


def test_is_public_ip():
    assert is_public_ip("8.8.8.8")
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254", "::1", "fc00::1",
               "100.64.0.1", "224.0.0.1", "::ffff:10.0.0.1"):
        assert not is_public_ip(ip), ip


def test_guarded_backend_blocks_dns_rebinding():
    async def evil_resolver(host, port):
        return ["127.0.0.1"]

    with pytest.raises(httpcore.ConnectError, match="non-public"):
        run(GuardedBackend(resolver=evil_resolver).connect_tcp("innocent.example.com", 443))


def test_guarded_backend_blocks_mixed_answers():
    async def mixed(host, port):
        return ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(httpcore.ConnectError):
        run(GuardedBackend(resolver=mixed).connect_tcp("x.example.com", 443))


def test_redirect_to_private_address_is_blocked():
    client = mock_http({"https://public.example.com/": (302, "", {"location": "http://127.0.0.1/admin"})})
    with pytest.raises(FetchError) as exc:
        run(client.request("GET", "https://public.example.com/"))
    assert exc.value.kind == "blocked_url"


@pytest.mark.parametrize("header", ["Authorization", "X-API-KEY", "x-apikey", "API-Key", "X-Subscription-Token"])
def test_credentials_are_not_forwarded_across_hosts(header):
    calls = []
    client = mock_http({"https://a.example.com/": (302, "", {"location": "https://b.example.com/x"}),
                        "https://b.example.com/": (200, {"ok": True})}, calls)
    resp = run(client.request("GET", "https://a.example.com/", headers={header: "secret"}))
    assert resp.status == 200
    assert calls[0].headers.get(header) == "secret"
    assert header.lower() not in calls[1].headers


def test_retry_then_success_and_429():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(503 if state["n"] < 3 else 200, json={"n": state["n"]})

    async def no_sleep(_):
        pass

    client = SafeHttpClient(transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert run(client.request("GET", "https://api.example.com/", retries=2, cache_ttl=None)).status == 200
    assert state["n"] == 3
    client = mock_http({"https://api.example.com/": (429, {"err": 1}, {"retry-after": "1"})})
    with pytest.raises(FetchError) as exc:
        run(client.request("GET", "https://api.example.com/"))
    assert exc.value.kind == "rate_limited"


def test_size_cap_truncates_and_refuses_json():
    client = mock_http({"https://big.example.com/": (200, "x" * 5000)}, max_bytes=1000)
    resp = run(client.request("GET", "https://big.example.com/"))
    assert resp.truncated and len(resp.content) == 1000
    with pytest.raises(ValueError):
        resp.json()


def test_cache_is_used_and_cache_hits_cost_no_budget():
    calls, spent = [], []
    client = mock_http({"https://api.example.com/": (200, {"v": 1})}, calls, cache=MemoryCache(),
                       budget=lambda connector: spent.append(connector))
    run(client.request("GET", "https://api.example.com/", connector="x", cache_ttl=60))
    second = run(client.request("GET", "https://api.example.com/", connector="x", cache_ttl=60))
    assert second.from_cache and len(calls) == 1 and spent == ["x"]


def test_budget_hook_stops_requests():
    def budget(connector):
        raise FetchError("budget", "exhausted")

    client = mock_http({"https://api.example.com/": (200, {"v": 1})}, budget=budget)
    with pytest.raises(FetchError) as exc:
        run(client.request("GET", "https://api.example.com/", cache_ttl=None))
    assert exc.value.kind == "budget"


def test_streaming_download_and_size_cap(tmp_path):
    client = mock_http({"https://data.example.org/list.csv": (200, "a,b\n" * 100)})
    n = run(client.download("https://data.example.org/list.csv", tmp_path / "list.csv"))
    assert n == 400 and (tmp_path / "list.csv").read_text().startswith("a,b")
    with pytest.raises(FetchError):
        run(client.download("https://data.example.org/list.csv", tmp_path / "big.csv", max_bytes=100))
    assert not (tmp_path / "big.csv").exists() and not (tmp_path / "big.csv.part").exists()
    with pytest.raises(FetchError):
        run(client.download("http://169.254.169.254/x", tmp_path / "x"))
