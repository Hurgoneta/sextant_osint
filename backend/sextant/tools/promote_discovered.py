"""Curation assistant: proposes official sources for countries that only have dataset-discovered leads.

    python -m sextant.tools.promote_discovered --out data/jurisdictions/curated_global.toml
    python -m sextant.tools.promote_discovered --countries AL BA ME --dry-run

For each sovereign state without a curated [[country]] table it takes, from the discovered datasets, the core
official institutions — company/business register (GLEIF registration-authority list), official gazette, highest
courts, statistics office, parliament, open-data portal, election, anti-corruption and data-protection authorities,
government portal — applies plausibility rules (a central bank listed as a "register" is not a company register),
checks every URL live, and writes only the reachable ones. Each entry records where it came from and that its search
capabilities (name search, personal data) have not been reviewed: a reviewer completes those fields over time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import USER_AGENT
from ..registry import countries as C
from ..registry import discovered as D
from ..registry import jurisdictions as J
from .verify_catalog import check

PLAN = (("company_registry", 2), ("gazette", 2), ("court", 3), ("statistics", 1), ("parliament", 1), ("open_data", 1),
        ("election_authority", 1), ("anti_corruption", 1), ("data_protection", 1), ("intellectual_property", 1),
        ("government_portal", 1))
REGISTER_WORDS = re.compile(r"regist|commerc|compan|business|enterpris|trade|corporat|rccm|guichet|investment|"
                            r"one.stop|chamber|registro|registre|cadastre", re.I)
NOT_REGISTER = re.compile(r"\bbank\b|banque|banco|securities|insurance|assurance|statistic|tax|revenue|monetary|"
                          r"financial (services|supervis|regulat)|capital market|stock exchange", re.I)
KEEP = ("ok", "reachable_refuses_bots", "tls_problem")
NOTE = ("Official institution identified from {dataset} ({ref}); link checked {date}. Search capabilities (name search, "
        "personal data) not yet reviewed.")


def plausible(s: D.DiscoveredSource) -> bool:
    if s.kind == "company_registry":
        text = f"{s.title} {s.notes}"
        return bool(REGISTER_WORDS.search(text)) and not NOT_REGISTER.search(s.title)
    if s.kind == "government_portal":
        return "mfa." not in s.url and "foreign" not in s.url
    return True


def candidates(code: str) -> list[D.DiscoveredSource]:
    items = [s for s in D.for_country(code) if plausible(s)]
    out: list[D.DiscoveredSource] = []
    seen_hosts: set[str] = set()
    for kind, limit in PLAN:
        picked = 0
        for s in sorted((x for x in items if x.kind == kind), key=lambda x: (x.dataset != "gleif_ra", x.title)):
            host = httpx.URL(s.url).host.removeprefix("www.")
            if host in seen_hosts or picked >= limit:
                continue
            seen_hosts.add(host)
            out.append(s)
            picked += 1
    return out


def toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


async def run(codes: list[str], out_path: Path | None) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    proposals = {code: candidates(code) for code in codes}
    sem = asyncio.Semaphore(12)
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(20.0), trust_env=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as client:
        async def one(s: D.DiscoveredSource) -> None:
            async with sem:
                results[s.id] = await check(client, s.url)
        await asyncio.gather(*(one(s) for items in proposals.values() for s in items))
    lines = ["# Official sources for countries that had only dataset-discovered leads, proposed by",
             "# `python -m sextant.tools.promote_discovered` and link-checked on " + today + ".",
             "# Every entry is an official institution named by GLEIF (registration authorities) or Wikidata, with a working",
             "# link. Fields a reviewer still has to complete: name_search, person_data, access. Until then access is",
             "# \"unknown\" and name_search is false, so nothing here claims more than \"this official site exists\".", ""]
    kept = dropped = 0
    for code in codes:
        c = C.get(code)
        items = [s for s in proposals[code] if results.get(s.id, {}).get("result") in KEEP]
        dropped += len(proposals[code]) - len(items)
        if not items:
            continue
        lines += ["[[country]]", f"code = {toml_str(code)}", f"name = {toml_str(c.name)}", f"region = {toml_str(c.region)}"]
        n: dict[str, int] = {}
        for s in items:
            n[s.kind] = n.get(s.kind, 0) + 1
            kept += 1
            ref = s.dataset_ref if s.dataset != "wikidata" else f"Wikidata {s.dataset_ref}"
            lines += ["[[country.source]]", f"id = {toml_str(f'{code.lower()}-{s.kind.replace(chr(95), chr(45))}-{n[s.kind]}')}",
                      f"title = {toml_str(s.title)}", f"kind = {toml_str(s.kind)}", f"url = {toml_str(s.url)}",
                      'access = "unknown"',
                      f"notes = {toml_str(NOTE.format(dataset=D.DATASETS[s.dataset], ref=ref, date=today))}"]
        lines.append("")
    text = "\n".join(lines) + "\n"
    print(f"{kept} sources kept, {dropped} dropped (dead or unreachable links) for {len(codes)} countries")
    if out_path:
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote {out_path}")
    else:
        print(text[:4000])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--countries", nargs="+", metavar="CC")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    import tomllib
    own = args.out.resolve() if args.out else None
    curated_elsewhere: set[str] = set()
    for f in J.DATA.glob("*.toml"):              # countries curated by hand (this tool's own output does not count)
        if f.resolve() == own or f.name in ("regional.toml", "supplement.toml"):
            continue
        curated_elsewhere |= {t["code"] for t in tomllib.loads(f.read_text(encoding="utf-8")).get("country", [])}
    codes = [c.upper() for c in args.countries] if args.countries else \
        [c.code for c in C.sovereign() if c.code not in curated_elsewhere]
    return asyncio.run(run(sorted(codes), None if args.dry_run else args.out))


if __name__ == "__main__":
    sys.exit(main())
