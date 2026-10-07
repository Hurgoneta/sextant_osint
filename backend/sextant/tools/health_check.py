"""Runs each connector's health probe against the real source and records the outcome with a date.

    python -m sextant.tools.health_check                 # probes in data/health/probes.toml (fast ones)
    python -m sextant.tools.health_check --all           # include slow probes (large list downloads)
    python -m sextant.tools.health_check --only github rdap
    python -m sextant.tools.health_check --data-dir ~/.local/share/sextant   # also record in that installation

The probes send only public test inputs (example domains, mascot accounts, large listed companies). Results go to
``data/health/_health.json`` (shipped with the code, shown as "last verified") and, with --data-dir, into the
local database. A probe for a connector that needs credentials is skipped unless the key is in the environment.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ..registry import connectors as REG
from ..registry.health import DATA as HEALTH_DIR
from ..sdk.http import SafeHttpClient
from ..sdk.testing import run_transform, view
from ..sdk.transform import InvestigationInfo


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_probes() -> dict:
    with open(HEALTH_DIR / "probes.toml", "rb") as fh:
        return tomllib.load(fh).get("probe", {})


def run_probe(name: str, spec: dict, data_dir: Path | None) -> dict:
    typ, value, *label = spec["entity"]
    ent = view(typ, value, label=label[0] if label else None, **(spec.get("props") or {}))
    started = time.monotonic()
    import os
    out, status, message = run_transform(spec["transform"], ent, http=SafeHttpClient(), info=InvestigationInfo(),
                                         credentials=lambda k: os.environ.get(k), data_dir=data_dir)
    return {"transform": spec["transform"], "status": status, "message": message[:300], "checked_at": now(),
            "seconds": round(time.monotonic() - started, 1), "entities": len(out.entities)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="include slow probes")
    ap.add_argument("--only", nargs="+", metavar="CONNECTOR")
    ap.add_argument("--data-dir", type=Path, help="also record the outcomes in this installation's database")
    ap.add_argument("--dry-run", action="store_true", help="don't write data/health/_health.json")
    args = ap.parse_args(argv)
    REG.load_all()
    probes = load_probes()
    if args.only:
        probes = {k: v for k, v in probes.items() if k in args.only}
    path = HEALTH_DIR / "_health.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"connectors": {}}
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="sextant-health-"))
    results = {}
    for name, spec in probes.items():
        if name not in REG.REGISTRY:
            print(f"  {name:22} unknown connector — skipped")
            continue
        if spec.get("slow") and not args.all and not args.only:
            continue
        try:
            r = run_probe(name, spec, work)
        except Exception as exc:  # a crashing probe is a broken connector
            r = {"transform": spec["transform"], "status": "error", "message": f"{exc.__class__.__name__}: {exc}"[:300],
                 "checked_at": now(), "seconds": 0, "entities": 0}
        results[name] = r
        print(f"  {name:22} {r['status']:16} {r['seconds']:6}s  {r['message'][:90]}")
    doc.setdefault("connectors", {}).update(results)
    doc["checked_at"] = now()
    if not args.dry_run:
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    if args.data_dir:
        from ..storage.crypto import Vault
        from ..storage.db import Database
        db = Database(args.data_dir, Vault.open(args.data_dir))
        db.record_health([(n, r["status"], r["checked_at"]) for n, r in results.items()])
        db.close()
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{len(results)} probes: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
