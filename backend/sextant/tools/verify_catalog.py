"""Liveness check for every URL in the jurisdiction registry, the platform registry and connector manifests.

    python -m sextant.tools.verify_catalog            # check and write data/jurisdictions/_verification.json
    python -m sextant.tools.verify_catalog --dry-run  # check only
    python -m sextant.tools.verify_catalog --only jur:ma-opendata conn:crtsh   # re-check entries, merge into the file

Results: ``ok``; ``reachable_refuses_bots`` (401/403/405/429 — many government sites refuse automated
or foreign clients, which says nothing about the link being wrong); ``tls_problem`` (the server
exists but sends an incomplete certificate chain — browsers usually cope); ``unreachable`` (timeouts,
often geo-blocking, or a name the local resolver cannot resolve although public DNS can); ``server_error`` (5xx);
``dead`` (the name does not exist in public DNS, refused connection, 404/410). A redirect to a bot-check page
counts as ``reachable_refuses_bots``, not ``ok``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from .. import USER_AGENT
from ..registry import jurisdictions as J
from ..registry import platforms as P

CONCURRENCY = 12
PUBLIC_RESOLVERS = ["1.1.1.1", "9.9.9.9"]
BOT_WALL_MARKERS = ("unblock.", "captcha", "/cdn-cgi/", "challenge", "/sorry/", "bot-protection")
DNS_ERRORS = ("name or service not known", "temporary failure in name resolution", "nodename nor servname",
              "no address associated", "getaddrinfo failed")


def resolves_publicly(host: str) -> bool:
    """Whether public DNS knows the name (distinguishes a dead domain from a flaky local resolver)."""
    try:
        import dns.resolver
        r = dns.resolver.Resolver(configure=False)
        r.nameservers, r.lifetime = PUBLIC_RESOLVERS, 8.0
        for rtype in ("A", "AAAA"):
            try:
                if r.resolve(host, rtype):
                    return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
    except Exception:
        return False
    return False


def classify(status: int | None, error: str | None, final_url: str | None = None, host: str = "") -> str:
    if error:
        low = error.lower()
        if any(m in low for m in DNS_ERRORS):
            return "unreachable" if host and resolves_publicly(host) else "dead"
        if "certificate" in low or "ssl" in low:
            return "tls_problem"          # server exists but serves an incomplete/invalid chain (browsers often cope)
        if "timeout" in low or "readerror" in low or "disconnected" in low or "all connection attempts" in low:
            return "unreachable"          # slow, geo-blocked or rate limiting unknown clients
        return "dead"
    if status is None:
        return "dead"
    if status in (404, 410):
        return "dead"
    if status in (401, 403, 405, 406, 429, 451) or status >= 500:
        return "reachable_refuses_bots" if status < 500 else "server_error"
    if final_url and any(m in final_url.lower() for m in BOT_WALL_MARKERS):
        return "reachable_refuses_bots"   # redirected to a bot-check page: the link is fine, the client was refused
    return "ok"


async def check(client: httpx.AsyncClient, url: str) -> dict:
    status, final, error = None, None, None
    try:
        resp = await client.get(url)
        status, final = resp.status_code, str(resp.url)
    except httpx.HTTPError as exc:
        error = f"{exc.__class__.__name__}: {str(exc)[:120]}"
    final = final if final and final != url else None
    host = httpx.URL(url).host
    result = await asyncio.to_thread(classify, status, error, final, host)
    return {"status": status, "final_url": final, "error": error, "result": result}


async def run(dry_run: bool, only: list[str] | None = None, discovered: bool = False) -> int:
    targets: dict[str, tuple[str, str]] = {}
    for s in J.all_sources():
        targets[f"jur:{s.id}"] = ("jurisdiction", s.url)
    if discovered:
        from ..registry import discovered as D
        for s in D.all_discovered():
            targets[f"disc:{D.verification_key(s)}"] = ("discovered", s.url)
    for p in P.curated():
        targets[f"plat:{p.id}"] = ("platform", p.url)
    try:
        from ..registry import connectors as C
        C.load_all()
        for name, conn in C.REGISTRY.items():
            if conn.manifest.website.startswith(("https://", "http://")):
                targets[f"conn:{name}"] = ("connector", conn.manifest.website)
    except Exception as exc:  # the catalog check must still work while connectors are being developed
        print(f"(connectors not loaded: {exc})", file=sys.stderr)

    if only:
        unknown = [k for k in only if k not in targets]
        if unknown:
            print(f"Unknown entries: {', '.join(unknown)} (use jur:<id>, plat:<id>, conn:<name> or disc:<id>)", file=sys.stderr)
            return 2
        targets = {k: targets[k] for k in only}

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict] = {}
    by_url: dict[str, asyncio.Task] = {}             # shared regional portals are checked once per URL
    limits = httpx.Limits(max_connections=CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(20.0), limits=limits, trust_env=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as client:
        async def fetch(url: str) -> dict:
            async with sem:
                return await check(client, url)

        async def one(key: str, url: str) -> None:
            if url not in by_url:
                by_url[url] = asyncio.ensure_future(fetch(url))
            results[key] = {"url": url, **await by_url[url]}
        await asyncio.gather(*(one(k, u) for k, (_, u) in targets.items()))

    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    print(f"Checked {len(results)} URLs: {counts}")
    for key in sorted(results):
        r = results[key]
        if r["result"] in ("dead", "server_error", "unreachable", "tls_problem") and not key.startswith("disc:"):
            print(f"  {r['result'].upper():13} {key:40} {r['url']}  {r['status'] or ''} {r['error'] or ''}")
    if not dry_run:
        path = J.DATA / "_verification.json"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for r in results.values():
            r["checked_at"] = now
        sections = {"jur:": "sources", "plat:": "platforms", "conn:": "connectors", "disc:": "discovered"}
        doc = {"checked_at": now, "sources": {}, "platforms": {}, "connectors": {}, "discovered": {}}
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            if only:
                doc = {**doc, **old}
            elif not discovered:
                doc["discovered"] = old.get("discovered", {})     # keep the last discovered-source check
        for key, r in results.items():
            prefix, ident = key.split(":", 1)
            doc[sections[prefix + ":"]][ident] = r
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="check only; don't write the results file")
    ap.add_argument("--only", nargs="+", metavar="KEY", help="re-check these entries (jur:<id>, plat:<id>, conn:<name>) "
                                                             "and merge them into the existing results")
    ap.add_argument("--discovered", action="store_true", help="also check the dataset-discovered sources (~5,000 URLs)")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.dry_run, args.only, args.discovered))


if __name__ == "__main__":
    sys.exit(main())
