"""Investigation report: JSON (complete, with provenance), CSV (entities) and a self-contained HTML report.

Every important claim carries source references ([S3]) that resolve to the evidence appendix: connector,
original URL, retrieval time, what was sent.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import APP_NAME, __version__
from ..api import views
from ..model import utcnow
from ..ontology import ENTITY_TYPES

TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]), trim_blocks=True,
                   lstrip_blocks=True)


class SourceIndex:
    """Stable [S#] numbering of sources in citation order."""

    def __init__(self, g) -> None:
        self.g = g
        self.order: dict[str, int] = {}

    def ref(self, sid: str | None) -> int | None:
        if not sid or sid not in self.g.sources:
            return None
        if sid not in self.order:
            self.order[sid] = len(self.order) + 1
        return self.order[sid]

    def refs_for_entity(self, key: str, limit: int = 4) -> list[int]:
        out = []
        for s in self.g.sources_of(key):
            n = self.ref(s.id)
            if n and n not in out:
                out.append(n)
            if len(out) >= limit:
                break
        return out

    def appendix(self) -> list[dict]:
        out = []
        for sid, n in sorted(self.order.items(), key=lambda kv: kv[1]):
            s = self.g.sources[sid]
            out.append({"n": n, "connector": s.connector, "title": s.title, "url": s.url, "retrieved_at": s.retrieved_at,
                        "kind": s.kind, "request": s.request, "license": s.license, "reliability": s.reliability})
        return out


def export_input(rt) -> dict:
    """The investigation input as exported: local-only fields (the birth year) stay on this machine."""
    inp = dict(rt.data.get("input") or {})
    inp.pop("birth_year", None)
    inp["structured"] = {k: v for k, v in (inp.get("structured") or {}).items() if k not in ("birth_year", "born")}
    return inp


def _strong(e) -> list[str]:
    return [r.description for r in e.reasons if r.weight > 0][:4]


def _contra(e) -> list[str]:
    return [r.description for r in e.reasons if r.weight < 0][:3]


def build_model(rt, coverage: dict, decisions: list[dict]) -> dict:
    g = rt.graph
    idx = SourceIndex(g)
    ents = list(g.entities.values())
    active = [e for e in ents if e.active]

    def row(e, **extra) -> dict:
        return {"label": e.label, "type": ENTITY_TYPES[e.type].title, "confidence": round(e.confidence * 100),
                "tier": e.tier, "match": e.match_label, "status": e.status, "support": _strong(e),
                "contradictions": _contra(e), "refs": idx.refs_for_entity(e.key), **extra}

    accounts = sorted((e for e in active if e.type == "account"), key=lambda e: -e.confidence)
    persons = sorted((e for e in active if e.type == "person" and e.key != g.target_key), key=lambda e: -e.confidence)
    likely = [e for e in active if ENTITY_TYPES[e.type].resolvable and e.status != "seed"
              and (e.status == "confirmed" or e.confidence >= 0.75)]
    records = views.records_view(g)
    for r in records:
        r["refs"] = [idx.ref(s["id"]) for s in r.get("sources") or [] if s][:4]
    contradictions = [e for e in active if any(r.weight < 0 for r in e.reasons) and 0.25 <= e.confidence <= 0.85
                      and e.status == "candidate"]
    same_as = [r for r in g.relationships.values() if r.type == "SAME_AS" and r.status != "rejected"]
    target = g.target
    inp = export_input(rt)
    by_type = Counter(e.type for e in ents)
    cov = coverage.get("summary", {})
    recs = recommendations(rt, coverage, likely, contradictions, records)
    model = {
        "app": APP_NAME, "version": __version__, "generated_at": utcnow(), "id": rt.id, "title": rt.title,
        "purpose": rt.data.get("purpose"), "status": rt.status, "stop_reason": rt.stop_reason,
        "started_at": rt.started_at, "finished_at": rt.finished_at, "limits": rt.limits.to_dict(),
        "target": {"label": target.label if target else None, "type": target.type if target else None},
        "input": inp,
        "summary": {"entities": len(ents), "relationships": len(g.relationships), "sources": len(g.sources),
                    "observations": len(g.observations), "runs": len(rt.runs), "likely": len(likely),
                    "accounts": len(accounts), "likely_accounts": sum(1 for e in accounts if e.confidence >= 0.75 or e.status == "confirmed"),
                    "records": len(records), "by_type": dict(by_type)},
        "key_findings": [row(e) for e in sorted(likely, key=lambda e: (-(e.status == "confirmed"), -e.confidence))[:15]],
        "identity_candidates": [row(e, birth=e.get("birth_date"), role=e.get("role")) for e in persons[:40]],
        "accounts": [row(e, platform=e.get("platform_title") or e.get("platform"), username=e.get("username"),
                         url=e.get("profile_url")) for e in accounts[:200]],
        "domains": [row(e, registrar=e.get("registrar"), registered=e.get("registered"), site_title=e.get("site_title"),
                        technologies=", ".join(map(str, e.all_list("technologies")))[:120])
                    for e in sorted((e for e in active if e.type == "domain"), key=lambda e: -e.confidence)[:60]],
        "websites": [row(e, url=e.get("url"), category=e.get("category"), matched=", ".join(e.all_list("matched")))
                     for e in sorted((e for e in active if e.type == "website"), key=lambda e: -e.confidence)[:80]],
        "documents": [row(e, url=e.get("url"), file_type=e.get("file_type"))
                      for e in sorted((e for e in active if e.type == "document"), key=lambda e: -e.confidence)[:60]],
        "publications": [row(e, year=e.get("year"), venue=e.get("venue"), url=e.get("url"))
                         for e in sorted((e for e in active if e.type == "publication"), key=lambda e: -e.confidence)[:40]],
        "organizations": [row(e, jurisdiction=e.get("jurisdiction"), number=e.get("registration_number"),
                              status_text=e.get("status")) for e in sorted((e for e in active if e.type == "organization"),
                                                                           key=lambda e: -e.confidence)[:60]],
        "records": records,
        "identifiers": [row(e) for e in sorted((e for e in active if e.type in ("email", "phone", "username", "name", "ip")),
                                               key=lambda e: (e.type, -e.confidence))[:120]],
        "breaches": [row(e, date=e.get("breach_date"), classes=", ".join(map(str, e.all_list("data_classes"))))
                     for e in active if e.type == "breach"],
        "timeline": [{**t, "refs": [idx.ref(t["source"]["id"])] if t.get("source") else []}
                     for t in views.timeline_view(g, 0.5)][:200],
        "same_as": [{"a": g.entities[r.source].label, "b": g.entities[r.target].label, "confidence": round(r.confidence * 100),
                     "why": r.evidence[0].description if r.evidence else ""} for r in same_as[:60]],
        "contradictions": [row(e) for e in contradictions[:40]],
        "coverage": cov, "coverage_groups": coverage.get("groups", []),
        "coverage_rows": [c for c in coverage.get("connectors", []) if c["state"] not in ("not_applicable",)],
        "disclaimer": coverage.get("disclaimer"),
        "global_coverage": coverage.get("global"),
        "decisions": decisions, "recommendations": recs, "model_note": views.MODEL_NOTE,
    }
    model["sources"] = idx.appendix()
    return model


def recommendations(rt, coverage: dict, likely, contradictions, records) -> list[str]:
    out = []
    rows = coverage.get("connectors", [])
    creds = [r["title"] for r in rows if r["state"] in ("needs_credentials",)]
    subs = [r["title"] for r in rows if r["state"] in ("needs_subscription",)]
    failed = [r["title"] for r in rows if r["state"] == "failed"]
    if creds:
        out.append(f"{len(creds)} applicable sources need a free API key or approval: {', '.join(creds[:8])}"
                   f"{'…' if len(creds) > 8 else ''}. Configure them under Data Sources to widen coverage.")
    if subs:
        out.append(f"{len(subs)} applicable sources need a subscription: {', '.join(subs[:6])}. Use them only if you hold "
                   "a licence for this purpose.")
    if failed:
        out.append(f"These sources were unreachable or refused access: {', '.join(failed[:6])}. Retry later — nothing was "
                   "concluded from them.")
    cand = [e for e in rt.graph.entities.values() if e.status == "candidate" and 0.4 <= e.confidence < 0.75
            and ENTITY_TYPES[e.type].resolvable]
    if cand:
        out.append(f"Review {len(cand)} possible matches (40–75%) and confirm, separate or ignore them; each decision "
                   "re-scores the rest of the graph.")
    if contradictions:
        out.append(f"{len(contradictions)} candidates carry contradicting evidence — check them before relying on them.")
    rec_names = [r for r in records if r.get("identity_state") == "Possible name match"]
    if rec_names:
        out.append(f"{len(rec_names)} public records match by name only. Verify each at its official URL and compare "
                   "dates of birth or other identifiers; never treat a name match as an identity match.")
    if rt.stop_reason:
        out.append(f"The run stopped early ({rt.stop_reason}). Use Continue to finish the queued transforms.")
    if rt.limits.max_depth < 3 and likely:
        out.append("Raise the depth to 3 to follow the strongest leads further.")
    return out


def to_json(rt, coverage: dict, decisions: list[dict], extras: dict | None = None) -> bytes:
    g = rt.graph
    doc = {
        "app": APP_NAME, "version": __version__, "exported_at": utcnow(), "investigation": {
            "id": rt.id, "title": rt.title, "input": export_input(rt), "purpose": rt.data.get("purpose"),
            "limits": rt.limits.to_dict(), "status": rt.status},
        "entities": [{**e.summary(), "first_seen": e.first_seen,
                      "properties": {k: [{"value": pv["v"], "observations": pv["obs"], "confidence": pv["conf"]} for pv in v]
                                     for k, v in e.props.items()
                                     if not (ENTITY_TYPES[e.type].prop(k) and ENTITY_TYPES[e.type].prop(k).local_only)},
                      "assessment": [r.to_dict() for r in e.reasons]} for e in g.entities.values()],
        "relationships": [{"id": r.id, "source": g.entities[r.source].id, "target": g.entities[r.target].id, "type": r.type,
                           "confidence": r.confidence, "status": r.status, "props": r.props,
                           "evidence": [ev.to_dict() for ev in r.evidence]} for r in g.relationships.values()
                          if r.source in g.entities and r.target in g.entities],
        "observations": [{**o.to_dict(), "entity_id": g.get(o.entity).id if g.get(o.entity) else None}
                         for o in g.observations.values()
                         if not (o.prop == "birth_year")],
        "sources": [s.to_dict() for s in g.sources.values()],
        "checks": [c.to_dict() for c in g.checks.values()],
        "transform_runs": [r.to_dict() for r in rt.runs.values()],
        "coverage": coverage, "decisions": decisions,
        **(extras or {}),
        "note": "birth_year entered by the investigator is local-only and excluded from exports.",
    }
    return json.dumps(doc, ensure_ascii=False, indent=1, default=str).encode()


def _safe_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rt) -> bytes:
    g = rt.graph
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "type", "label", "value", "status", "confidence", "tier", "match", "depth", "sources", "urls",
                "supporting_evidence", "contradictions"])
    for e in g.entities.values():
        srcs = g.sources_of(e.key)
        w.writerow([_safe_cell(x) for x in (
            e.id, e.type, e.label, e.value, e.status, round(e.confidence, 3), e.tier, e.match_label, e.depth,
            "; ".join(sorted({s.connector for s in srcs})), "; ".join(sorted({s.url for s in srcs if s.url}))[:2000],
            " | ".join(r.description for r in e.reasons if r.weight > 0), " | ".join(r.description for r in e.reasons if r.weight < 0))])
    return buf.getvalue().encode("utf-8-sig")


def build_report(rt, coverage: dict, decisions: list[dict], fmt: str, extras: dict | None = None) -> tuple[bytes, str]:
    """``extras``: identity discovery (stages, discovered handles, web queries), the identifier layer with states, costs
    and notes — added to the JSON export and as sections of the HTML report."""
    if fmt == "json":
        return to_json(rt, coverage, decisions, extras), "application/json"
    if fmt == "csv":
        return to_csv(rt), "text/csv"
    model = build_model(rt, coverage, decisions)
    model["extras"] = extras or {}
    html = _env.get_template("report.html").render(**model)
    return html.encode(), "text/html; charset=utf-8"


__all__ = ["build_report", "build_model"]
