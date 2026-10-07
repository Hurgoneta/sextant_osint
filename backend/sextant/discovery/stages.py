"""The identity-discovery pipeline of one investigation, stage by stage, from what actually happened.

    1 normalize the name        5 platform-specific searches      9 search other platforms
    2 candidate names           6 extract profile URLs           10 correlate evidence
    3 candidate usernames       7 extract handles                11 rank candidate identities
    4 web search for the name   8 pivot on discovered handles

Every number comes from the graph, the run log or the search journal. When no web-search provider is available the
report says "Web discovery unavailable — API-only search was used" instead of letting an empty result look like
"no username found".
"""

from __future__ import annotations

from ..assistant.names import candidates as name_candidates
from ..util.normalize import fold
from . import router as ROUTER
from .queries import name_variants


def report(rt, cfg, identities: list[dict] | None = None) -> dict:
    g = rt.graph
    t = g.target
    names = []
    if t is not None and t.type == "person":
        names = [str(n) for n in [*t.values("full_name"), *t.all_list("aliases")] if n]
    journal = rt.data.get("search_journal") or []
    web_runs = [r for r in rt.runs.values() if r.transform.endswith((".person_web", ".profile_discovery",
                                                                       ".identifier_web"))]
    available = ROUTER.available(cfg)
    unavailable = "" if available or web_runs else ROUTER.unavailable_message(cfg)
    q_name = [j for j in journal if j.get("kind") in ("name", "web") and j.get("status") == "ok"]
    q_platform = [j for j in journal if j.get("kind") in ("name_platform", "handle_platform") and j.get("status") == "ok"]
    q_err = [j for j in journal if j.get("status") == "error"]
    profiles = {p for j in journal for p in j.get("profiles") or []}
    discovered = [u for u in g.of_type("username") if u.get("discovery") == "search_result"]
    pivots = rt.data.get("discovery_pivots") or []
    pivot_runs = [r for r in rt.runs.values() if r.input_key in {u.key for u in discovered}]
    other = {(c.platform, c.identifier.casefold()) for c in g.checks.values() if c.status == "found"
             and any(c.identifier.casefold() == u.value for u in discovered)}
    scored = [e for e in g.entities.values() if e.type == "account" and e.reasons]
    gen = [c.handle for c in name_candidates(names, 10)] if names else []
    unpredictable = [u.label for u in discovered if u.value not in {h.casefold() for h in gen}]

    running = rt.status == "running"
    searched_handles = {r.input_key for r in pivot_runs}

    def st(started: bool, done: bool, skip: bool = False) -> str:
        """skipped (nothing to do) · pending (not started) · running · done (ran; a result of 0 is a result)."""
        if skip:
            return "skipped"
        if not started:
            return "pending"
        return "done" if done or not running else "running"

    stages = [
        {"n": 1, "step": "Normalize the name", "status": "done" if names else "skipped",
         "detail": ", ".join(sorted({fold(n) for n in names})) or "no person name in the input"},
        {"n": 2, "step": "Candidate names", "status": "done" if names else "skipped",
         "detail": " · ".join(v[0] for v in name_variants(names[0]))[:300] if names else ""},
        {"n": 3, "step": "Candidate usernames (hypotheses)", "status": "done" if gen else "skipped",
         "detail": f"{len(gen)} generated, labelled hypotheses — the pipeline does not stop here"},
        {"n": 4, "step": "Web search for the full name", "status": st(bool(q_name), False, bool(unavailable)),
         "detail": unavailable or f"{len(q_name)} quer(ies) run"},
        {"n": 5, "step": "Platform-specific searches", "status": st(bool(q_platform), False, bool(unavailable)),
         "detail": "web discovery unavailable" if unavailable else f"{len(q_platform)} site-restricted quer(ies)"
         + (f", {len(q_err)} failed" if q_err else "")},
        {"n": 6, "step": "Extract profile URLs", "status": st(bool(journal), bool(profiles), bool(unavailable)),
         "detail": f"{len(profiles)} profile URL(s) recognised in results"},
        {"n": 7, "step": "Extract handles", "status": st(bool(journal), bool(discovered), bool(unavailable)),
         "detail": f"{len(discovered)} handle(s) discovered" + (f", {len(unpredictable)} not predictable from the "
                                                                  f"name: {', '.join(unpredictable[:5])}" if unpredictable else "")},
        {"n": 8, "step": "Pivot on discovered handles", "status": st(bool(discovered), bool(searched_handles),
                                                                     not discovered and not running and bool(journal)),
         "detail": f"{len(searched_handles)} of {len(discovered)} discovered handle(s) searched further "
                   f"({len(pivot_runs)} run(s); {len(pivots)} chosen automatically from name results)"},
        {"n": 9, "step": "Search other platforms", "status": st(bool(pivot_runs), bool(other),
                                                                not discovered and not running and bool(journal)),
         "detail": f"{len(other)} platform match(es) for discovered handles"},
        {"n": 10, "step": "Correlate evidence", "status": st(bool(rt.runs), bool(scored)),
         "detail": f"{len(scored)} account(s) scored with reasons for and against"},
        {"n": 11, "step": "Rank candidate identities", "status": st(bool(rt.runs), identities is not None and bool(identities)),
         "detail": f"{len(identities or [])} candidate identit(ies)"},
    ]
    return {"stages": stages, "web_discovery": {
        "available": available, "primary": ROUTER.primary(available, rt.data.get("search_failed") or []),
        "failed": rt.data.get("search_failed") or [], "failovers": rt.data.get("search_failovers") or [],
        "unavailable_message": unavailable, "queries_run": len([j for j in journal if j.get("status") == "ok"]),
        "queries_failed": len(q_err)},
        "discovered_handles": [{"handle": u.label, "id": u.id, "on": u.get("discovered_on"), "rank": u.get("search_rank"),
                                "query": u.get("search_query"), "provider": u.get("search_provider"),
                                "pivot": u.key in pivots, "searched_further": u.key in searched_handles,
                                "predictable_from_name": u.value in {h.casefold() for h in gen}}
                               for u in discovered][:100],
        "journal": journal[-200:]}


__all__ = ["report"]
