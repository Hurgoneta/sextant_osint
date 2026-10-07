"""The identifier layer: every identifier of an investigation with its state, read from the evidence graph.

It is a *view* over the graph, not a second store: each row is a graph entity (a name, handle, email, phone, domain,
URL, organization, address or platform profile) with its provenance. That keeps it connected to evidence, entities and
resolution, and it is persisted with the investigation.

States (never mixed up):

    Provided    you typed it
    Generated   a hypothesis Sextant produced from the name (common handle patterns) and has not seen anywhere
    Discovered  seen in a search result (the URL or title of a result), not yet seen on the platform itself
    Observed    reported by a source that showed it directly (a platform API, a site check, a profile page)
    Possible    an account a source reported that the resolver rates as a possible/likely/strong candidate
    Verified    you confirmed it
    Rejected    you separated or ignored it
"""

from __future__ import annotations

from .analysis import _under_hypothesis, assess, origin_of

TYPE_MAP = {"name": "full_name", "username": "username", "email": "email", "phone": "phone", "domain": "domain",
            "website": "url", "organization": "organization", "location": "address", "account": "platform_profile",
            "public_record": "case_number"}
STATES = {"provided": "Provided", "generated": "Generated", "discovered": "Discovered", "observed": "Observed",
          "possible": "Possible", "verified": "Verified", "rejected": "Rejected"}


def state_of(g, e) -> str:
    if e.status in ("separated", "ignored"):
        return "rejected"
    if e.status == "confirmed":
        return "verified"
    if e.status == "seed":
        return "provided"
    ident = str(e.get("username") or e.value).casefold() if e.type in ("account", "username") else str(e.value)
    found_checks = [c for c in g.checks.values() if c.status == "found" and c.identifier.casefold() == ident
                    and (e.type != "account" or c.platform == e.get("platform"))]
    if e.get("hypothesis"):
        return "possible" if found_checks else "generated"
    if e.type == "account":
        a = assess(e, _under_hypothesis(g, e))
        if a["verdict"] in ("strong", "likely", "possible"):
            return "possible"
    if e.get("discovery") == "search_result" or e.get("found_by") == "search_result":
        # seen directly by something other than a search engine (an API, a site check, a profile page)?
        direct = [s for s in g.sources_of(e.key) if s.kind not in ("user", "local")
                  and not str(s.title or "").endswith(" results")]
        return "observed" if found_checks or direct else "discovered"
    return "observed"


def rows(rt, limit: int = 500) -> list[dict]:
    g = rt.graph
    out = []
    pivoted = {t.entity_key for t in rt.tasks.values() if t.status in ("queued", "running", "done")}
    for e in g.entities.values():
        typ = TYPE_MAP.get(e.type)
        if typ is None or e.key == g.target_key:
            continue
        o = origin_of(g, e)
        st = state_of(g, e)
        platforms = sorted({c.platform_title or c.platform for c in g.checks.values()
                            if c.status == "found" and c.identifier.casefold() == str(e.value)}) if e.type == "username" else []
        row = {"id": e.id, "key": e.key, "type": typ, "entity_type": e.type, "value": e.label,
               "normalized_value": e.value, "state": st, "state_title": STATES[st], "source": o["title"],
               "source_kind": o.get("source_kind"), "source_url": o.get("url") or e.get("source_url") or e.get("profile_url"),
               "discovered_at": e.first_seen, "confidence": round(e.confidence, 2), "tier": e.tier,
               "investigation_id": rt.id, "platforms_found": platforms[:20], "pivoted": e.key in pivoted,
               "search_rank": e.get("search_rank"), "search_query": e.get("search_query"),
               "search_provider": e.get("search_provider"), "platform": e.get("platform_title")}
        if e.type == "account":
            a = assess(e, _under_hypothesis(g, e))
            row["verdict"] = a["verdict_title"]
            row["explanation"] = a["explanation"]
        out.append(row)
    order = ["verified", "provided", "possible", "observed", "discovered", "generated", "rejected"]
    out.sort(key=lambda r: (order.index(r["state"]), r["type"], -(r["confidence"] or 0), str(r["value"]).lower()))
    return out[:limit]


def counts(items: list[dict]) -> dict:
    out = {k: 0 for k in STATES}
    for r in items:
        out[r["state"]] += 1
    return out


__all__ = ["rows", "counts", "state_of", "STATES", "TYPE_MAP"]
