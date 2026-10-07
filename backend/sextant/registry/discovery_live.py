""""Search for connectors" for one country — live source discovery, run on demand from the Global Coverage screen.

It does two honest things and reports exactly what it did:

1. asks Wikidata (CC0) live for this country's public institutions with an official website — statistics office,
   gazette, courts, parliament, registers, regulators, archives, news agencies, open-data portals… — so sources
   added to Wikidata since the shipped snapshot appear;
2. checks that every potential and official source URL of the country answers (same classification as
   ``verify_catalog``), through the guarded HTTP client.

Nothing about any person is sent: only the country's Wikidata item and the public URLs being checked. Results are
stored locally (settings key ``discovery:<CC>``) and merged into the country view as *discovered (live)*.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from . import countries as C
from . import discovered as D
from . import jurisdictions as J
from ..sdk.http import FetchError, SafeHttpClient
from ..tools.import_datasets import WIKIDATA_CLASSES
from ..tools.verify_catalog import classify

WDQS = "https://query.wikidata.org/sparql"
CONCURRENCY = 6


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def wikidata_institutions(http: SafeHttpClient, qid: str) -> list[dict]:
    cls_kind = {c: k for k, (classes, _limit) in WIKIDATA_CLASSES.items() for c in classes}
    values = " ".join(f"wd:{c}" for c in cls_kind)
    query = f"""
        SELECT ?item ?cls ?web ?en ?mul WHERE {{
          hint:Query hint:optimizer "None".
          VALUES ?cls {{ {values} }}
          ?item wdt:P17 wd:{qid}.
          ?item wdt:P31 ?cls.
          ?item wdt:P856 ?web.
          MINUS {{ ?item wdt:P576 [] }}
          OPTIONAL {{ ?item rdfs:label ?en FILTER(lang(?en) = "en") }}
          OPTIONAL {{ ?item rdfs:label ?mul FILTER(lang(?mul) = "mul") }}
        }} LIMIT 400"""
    resp = await http.request("GET", WDQS, connector="discovery", params={"query": query},
                              headers={"Accept": "application/sparql-results+json"}, cache_ttl=None, retries=1)
    if resp.status != 200:
        raise FetchError("http", f"Wikidata answered HTTP {resp.status}")
    out, seen = [], set()
    for b in json.loads(resp.content)["results"]["bindings"]:
        item = b["item"]["value"].rsplit("/", 1)[-1]
        url = b["web"]["value"]
        label = (b.get("en") or b.get("mul") or {}).get("value", "")
        kind = cls_kind.get(b["cls"]["value"].rsplit("/", 1)[-1], "government_portal")
        if not label or (item, url) in seen or not re.match(r"^https?://", url):
            continue
        seen.add((item, url))
        out.append({"id": f"wdlive:{item}", "qid": item, "kind": kind, "title": label, "url": url})
    return out


async def check_url(http: SafeHttpClient, url: str) -> dict:
    status, error, final = None, None, None
    try:
        resp = await http.request("GET", url, connector="discovery", cache_ttl=None, retries=0, max_bytes=300_000)
        status, final = resp.status, resp.url
    except FetchError as exc:
        error = f"{exc.kind}: {str(exc)[:120]}"
    except Exception as exc:  # validation errors (private address, bad scheme) are results too
        error = f"{exc.__class__.__name__}: {str(exc)[:120]}"
    result = classify(status, error, final if final and final != url else None, "")
    return {"url": url, "status": status, "error": error, "result": result, "checked_at": _now()}


async def discover(code: str, http: SafeHttpClient | None = None) -> dict:
    c = C.get(code)
    if c is None:
        raise KeyError(code)
    own = http is None
    http = http or SafeHttpClient()
    try:
        notes = []
        live: list[dict] = []
        if c.wikidata:
            try:
                live = await wikidata_institutions(http, c.wikidata)
            except (FetchError, ValueError, KeyError) as exc:
                notes.append(f"Wikidata query failed: {exc}")
        known_urls = {s.url.rstrip("/") for s in D.for_country(code)} | \
            {s.url.rstrip("/") for s in (J.jurisdictions()[code].sources if code in J.jurisdictions() else [])}
        new = [w for w in live if w["url"].rstrip("/") not in known_urls]
        targets = {s.id: s.url for s in D.for_country(code)}
        targets.update({s.id: s.url for s in (J.jurisdictions()[code].sources if code in J.jurisdictions() else [])})
        targets.update({w["id"]: w["url"] for w in new})
        sem = asyncio.Semaphore(CONCURRENCY)
        checks: dict[str, dict] = {}

        async def one(sid: str, url: str) -> None:
            async with sem:
                checks[sid] = await check_url(http, url)
        await asyncio.gather(*(one(k, v) for k, v in targets.items()))
        counts: dict[str, int] = {}
        for r in checks.values():
            counts[r["result"]] = counts.get(r["result"], 0) + 1
        return {"country": code, "ran_at": _now(), "wikidata_items": len(live), "new_sources": new, "checks": checks,
                "counts": counts, "notes": notes,
                "contacted": ["query.wikidata.org"] + sorted({u.split("/")[2] for u in targets.values() if "//" in u})[:50]}
    finally:
        if own:
            await http.aclose()
