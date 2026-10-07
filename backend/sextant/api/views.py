"""Read models for the API: entity detail with full provenance, accounts matrix, records, timeline."""

from __future__ import annotations

from collections import defaultdict

from ..model import DATE_PROPS, SIGHTING, Entity, Graph
from ..ontology import ENTITY_TYPES, RELATION_TYPES
from ..registry import platforms as PLAT

MODEL_NOTE = ("Confidence is a transparent heuristic (sum of the listed weights through a logistic curve), not a "
              "calibrated probability. Nothing is confirmed automatically.")


def source_dict(g: Graph, sid: str) -> dict | None:
    s = g.sources.get(sid)
    if s is None:
        return None
    return {"id": s.id, "connector": s.connector, "kind": s.kind, "url": s.url, "title": s.title,
            "retrieved_at": s.retrieved_at, "last_retrieved": s.last_retrieved or None, "reliability": s.reliability,
            "license": s.license, "request": s.request, "transform": s.transform, "raw_refs": list(s.raw_refs or [])}


def entity_detail(g: Graph, e: Entity) -> dict:
    etype = ENTITY_TYPES[e.type]
    props = []
    order = {p.name: i for i, p in enumerate(etype.properties)}
    for name, values in sorted(e.props.items(), key=lambda kv: (order.get(kv[0], 999), kv[0])):
        spec = etype.prop(name)
        if spec and spec.local_only:
            local = True
        else:
            local = False
        props.append({
            "name": name, "title": spec.title if spec else name.replace("_", " ").capitalize(),
            "kind": spec.kind if spec else "text", "local_only": local,
            "values": [{"value": pv["v"], "confidence": round(pv["conf"], 2),
                        "observations": [{"id": oid, "observed_at": g.observations[oid].observed_at,
                                          "evidence": g.observations[oid].evidence,
                                          "source": source_dict(g, g.observations[oid].source)}
                                         for oid in pv["obs"] if oid in g.observations]}
                       for pv in values]})
    sightings = [{"evidence": o.evidence, "observed_at": o.observed_at, "source": source_dict(g, o.source)}
                 for o in g.observations_of(e.key) if o.prop == SIGHTING]
    rels = []
    for r in g.edges(e.key, include_rejected=True):
        out = r.source == e.key
        other = g.entities.get(r.target if out else r.source)
        if other is None:
            continue
        rt = RELATION_TYPES.get(r.type)
        rels.append({"id": r.id, "type": r.type, "title": (rt.title if out else rt.inverse) if rt else r.type,
                     "direction": "out" if out else "in", "other": other.summary(), "confidence": round(r.confidence, 3),
                     "status": r.status, "props": r.props,
                     "evidence": [{"description": ev.description, "confidence": ev.confidence, "observed_at": ev.observed_at,
                                   "source": source_dict(g, ev.source)} for ev in r.evidence]})
    rels.sort(key=lambda d: (d["status"] == "rejected", -d["confidence"], d["type"]))
    srcs = {s.id: source_dict(g, s.id) for s in g.sources_of(e.key)}
    merged = [{"key": m["merged"], "label": (m.get("entity") or {}).get("label"), "at": m.get("at"), "note": m.get("note")}
              for m in g.merge_log if g.resolve(m["keep"]) == e.key]
    checks = [c.to_dict() for c in g.checks.values() if c.account_key == e.key]
    reasons = [r.to_dict() for r in e.reasons]
    return {
        "entity": {**e.summary(), "type_title": etype.title, "first_seen": e.first_seen, "last_seen": e.last_seen,
                   "resolvable": etype.resolvable},
        "properties": props, "sightings": sightings, "relationships": rels, "sources": list(srcs.values()),
        "assessment": {"confidence": round(e.confidence, 3), "tier": e.tier, "label": e.match_label,
                       "support": [r for r in reasons if r["kind"] == "support"],
                       "contradictions": [r for r in reasons if r["kind"] == "contradiction"],
                       "info": [r for r in reasons if r["kind"] == "info"], "model": "identity-v1", "note": MODEL_NOTE},
        "merged": merged, "checks": checks,
        "links": _links(e),
    }


def _links(e: Entity) -> list[dict]:
    out = []
    for prop in ("profile_url", "url", "official_url", "website", "example_url"):
        for v in e.values(prop):
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                out.append({"label": prop.replace("_", " "), "url": v})
    if e.type == "domain":
        out.append({"label": "website", "url": f"https://{e.value}"})
    return out[:6]


def graph_payload(g: Graph, min_confidence: float = 0.0, include_ignored: bool = True) -> dict:
    ents, keep = [], set()
    for e in g.entities.values():
        if not include_ignored and not e.active:
            continue
        if e.status not in ("seed", "confirmed") and ENTITY_TYPES[e.type].resolvable and e.confidence < min_confidence:
            continue
        d = e.summary()
        thumb = e.get("avatar_thumb") or e.get("thumb")
        if thumb:
            d["thumb"] = thumb
        d["platform"] = e.get("platform")
        ents.append(d)
        keep.add(e.key)
    rels = []
    for r in g.relationships.values():
        if r.source in keep and r.target in keep:
            s, t = g.entities[r.source], g.entities[r.target]
            rels.append({"id": r.id, "source": s.id, "target": t.id, "type": r.type, "confidence": round(r.confidence, 3),
                         "status": r.status, "evidence": len(r.evidence)})
    return {"entities": ents, "relationships": rels, "stats": g.stats(), "target": g.target.id if g.target else None}


def accounts_view(g: Graph, user_platforms: tuple = ()) -> dict:
    """Section 8: per platform — existence check status and, separately, identity confidence. ``user_platforms``: the
    investigator's own platforms, offered as manual-check links like the built-in manual ones."""
    by_identifier: dict[str, list[dict]] = defaultdict(list)
    for c in g.checks.values():
        acc = g.get(c.account_key) if c.account_key else None
        by_identifier[f"{c.identifier_type}:{c.identifier}"].append({
            **c.to_dict(), "account": acc.summary() if acc else None,
            "platform_title": c.platform_title or PLAT.title(c.platform)})
    accounts = []
    for e in g.of_type("account"):
        reasons = [r.to_dict() for r in e.reasons]
        accounts.append({**e.summary(), "platform": e.get("platform"), "platform_title": e.get("platform_title"),
                         "username": e.get("username"), "display_name": e.get("display_name"),
                         "profile_url": e.get("profile_url"), "category": e.get("category"), "thumb": e.get("avatar_thumb"),
                         "found_by": e.values("found_by"), "evidence": [r for r in reasons if r["weight"] > 0],
                         "contradictions": [r for r in reasons if r["weight"] < 0]})
    accounts.sort(key=lambda a: (-a["confidence"], a["label"]))
    summary = defaultdict(int)
    for c in g.checks.values():
        summary[c.status] += 1
    manual = []
    for u in g.of_type("username"):
        if u.active and (u.status == "seed" or u.confidence >= 0.5):
            manual.append({"username": u.label, "links": PLAT.manual_links(u.label, user_platforms)})
    return {"by_identifier": {k: sorted(v, key=lambda d: (d["status"] != "found", d["platform_title"]))
                              for k, v in by_identifier.items()},
            "accounts": accounts, "check_summary": dict(summary), "manual": manual}


RECORD_STATES = ("Public record found", "Possible name match", "Confirmed identity match", "Unable to verify")


def records_view(g: Graph) -> list[dict]:
    out = []
    for e in g.of_type("public_record"):
        p = {k: e.get(k) for k in ("record_type", "jurisdiction", "authority", "official_url", "reference", "date",
                                    "title", "category", "matched_name", "name_match", "role", "role_meaning",
                                    "birth_years", "nationality", "criminal", "status", "summary", "list_name",
                                    "how_to_verify", "note")}
        reasons = [r.to_dict() for r in e.reasons]
        out.append({**e.summary(), **p, "record_state": "Public record found", "identity_state": e.match_label,
                    "evidence": [r for r in reasons if r["weight"] > 0],
                    "contradictions": [r for r in reasons if r["weight"] < 0],
                    "sources": [source_dict(g, s.id) for s in g.sources_of(e.key)]})
    out.sort(key=lambda r: (r["status"] != "confirmed", -r["confidence"], str(r.get("date") or "")))
    return out




def timeline_view(g: Graph, min_confidence: float = 0.0) -> list[dict]:
    items = []
    for e in g.entities.values():
        if not e.active or (ENTITY_TYPES[e.type].resolvable and e.status not in ("seed", "confirmed")
                            and e.confidence < min_confidence):
            continue
        for prop, title in DATE_PROPS.items():
            for pv in e.props.get(prop, []):
                v = str(pv["v"])[:10]
                if len(v) >= 4 and v[:4].isdigit():
                    src = g.observations.get(pv["obs"][0]).source if pv["obs"] and pv["obs"][0] in g.observations else None
                    items.append({"date": v, "event": title, "entity": e.summary(), "source": source_dict(g, src) if src else None})
        if e.type == "publication" and e.get("year"):
            items.append({"date": str(e.get("year")), "event": "Publication", "entity": e.summary(), "source": None})
    items.sort(key=lambda d: d["date"])
    return items


def relationship_detail(g: Graph, r) -> dict:
    """"Why are these connected?" — every piece of evidence behind one edge, with its source and evidence class."""
    from ..assistant.analysis import source_class
    rt = RELATION_TYPES.get(r.type)
    s, t = g.entities.get(r.source), g.entities.get(r.target)
    ev = []
    for item in r.evidence:
        src = g.sources.get(item.source)
        ev.append({"description": item.description, "confidence": round(item.confidence, 3), "observed_at": item.observed_at,
                   "class": source_class(src), "source": source_dict(g, item.source)})
    classes = {e["class"] for e in ev}
    return {"id": r.id, "type": r.type, "title": rt.title if rt else r.type, "description": rt.description if rt else "",
            "from": s.summary() if s else None, "to": t.summary() if t else None, "confidence": round(r.confidence, 3),
            "status": r.status, "first_seen": r.first_seen, "evidence": ev,
            "inferred_only": bool(ev) and classes <= {"inferred", "generated"},
            "note": "Inferred by the resolver, not reported by a source: treat it as a hypothesis."
                    if classes and classes <= {"inferred"} else ""}
