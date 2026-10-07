"""What an investigation found, how sure it is, and what to do next — computed from the evidence graph.

Nothing here is generated text: every verdict is derived from the resolver's signed reasons, every row from a source
or a site check, every recommendation from the state of the graph and the registry. The AI chat (``copilot.py``) reads
the same output through its tools, so it cannot report more than the evidence shows.

Evidence classes (shown everywhere):

    provided   typed by the investigator
    observed   reported by a source (an API answer, a public page, a dataset)
    derived    computed locally from observed data (normalisation, numbering plans, a URL's domain)
    generated  a hypothesis Sextant generated to test (name-derived handles), never evidence
    inferred   concluded by the resolver from several observations (e.g. two accounts share a rare handle)

Identity verdicts never come from one weak signal: a name, a similar handle, a similar photo, the same country or city
alone leave a candidate at "Insufficient evidence". "Strong" needs a high score from at least two independent
non-name signals; one strong signal makes it "Likely".
"""

from __future__ import annotations

from collections import defaultdict

from ..ontology import ENTITY_TYPES
from ..registry import connectors as REG
from ..registry import platforms as P
from ..resolution.scorer import MEDIUM_FEATURES, STRONG_FEATURES
from .names import candidates as name_candidates
from .planner import DEPTH_MODE, MODES

NAME_FEATURES = frozenset({"name_full", "name_contained", "name_fuzzy", "name_surname", "name_first", "m_name",
                           "m_name_partial", "r_name_exact", "r_name_contained", "anchor_display_name"})
WEAK_ALONE = NAME_FEATURES | {"country", "city", "nationality_same", "occupation_same", "anchor_avatar", "username_base",
                              "search_rank", "username_weak"}
# a handle generated from the name and then found proves nothing about the person: the match is circular
CIRCULAR_FOR_GENERATED = frozenset({"username", "username_unique", "username_base"})
IDENTITY_TYPES = ("account", "website", "person", "public_record", "document", "publication")
VERDICTS = {
    "confirmed": ("Confirmed by you", "good"),
    "strong": ("Strong candidate", "good"),
    "likely": ("Likely: one strong signal", "info"),
    "possible": ("Possible", "warn"),
    "insufficient": ("Insufficient evidence", ""),
    "rejected": ("Rejected by you", "bad"),
}
VALUE_RANK = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- evidence classes

def source_class(src) -> str:
    if src is None:
        return "observed"
    if src.connector == "sextant" or src.id == "resolution":
        return "inferred"
    if src.kind == "user" or src.id == "user-input":
        return "provided"
    if src.connector == "username_guesses":
        return "generated"
    if src.kind == "local":
        return "derived"
    return "observed"


SOURCE_KINDS = {"public": "Public", "user_provided": "User-provided", "licensed": "Licensed provider",
                "commercial_api": "Commercial API", "derived": "Derived", "inferred": "Inferred",
                "generated": "Generated (hypothesis)"}


def source_kind(src) -> str:
    """Where a fact comes from, in the terms of the evidence graph: public, user-provided, licensed provider (a people-
    data or enterprise provider used with your licence), commercial API (a metered API returning public data), derived,
    inferred or generated."""
    cls = source_class(src)
    if cls == "provided":
        return "user_provided"
    if cls in ("derived", "inferred", "generated"):
        return cls
    from ..registry import connectors as REG
    conn = REG.REGISTRY.get(getattr(src, "connector", "") or "")
    if conn is not None:
        m = conn.manifest
        if m.category == "people_search" or m.access == "enterprise":
            return "licensed"
        if m.access in ("metered", "paid"):
            return "commercial_api"
    return "public"


def origin_of(g, e) -> dict:
    """Where an identifier came from: typed by you, generated, or discovered (and by which source)."""
    if e.status == "seed":
        return {"class": "provided", "title": "Provided by you"}
    if e.get("hypothesis"):
        return {"class": "generated", "title": "Generated candidate · Not verified", "source_kind": "generated"}
    srcs = g.sources_of(e.key)
    first = next((s for s in srcs if source_class(s) == "observed"), srcs[0] if srcs else None)
    out = {"class": source_class(first) if first else "observed",
           "title": f"Discovered by {first.title or first.connector}" if first else "Discovered",
           "url": first.url if first else None, "source_kind": source_kind(first) if first else "public"}
    if e.get("discovery") == "search_result" or e.get("found_by") == "search_result":
        out["title"] = f"Discovered in a search result (rank {e.get('search_rank')}, {e.get('search_provider')})"
        out["query"] = e.get("search_query")
    return out


# --------------------------------------------------------------------------- one candidate

def assess(e, generated: bool = False) -> dict:
    """The verdict on one candidate with its reasons — the resolver's numbers, read conservatively. ``generated``: the
    account was found under a name-derived handle, so a handle match is not counted as independent evidence."""
    weak = WEAK_ALONE | (CIRCULAR_FOR_GENERATED if generated else frozenset())
    support = [r for r in e.reasons if r.weight > 0]
    contra = [r for r in e.reasons if r.weight < 0]
    signals = sorted({r.feature for r in support if r.feature in STRONG_FEATURES | MEDIUM_FEATURES
                      and r.feature not in weak})
    name_only = bool(support) and all(r.feature in weak for r in support)
    if e.status == "confirmed":
        verdict = "confirmed"
    elif e.status in ("separated", "ignored"):
        verdict = "rejected"
    elif name_only or not signals:
        verdict = "insufficient"
    elif e.tier == "high" and len(signals) >= 2:
        verdict = "strong"
    elif e.tier == "high":
        verdict = "likely"
    elif e.tier == "medium":
        verdict = "possible"
    else:
        verdict = "insufficient"
    title, kind = VERDICTS[verdict]
    explanation = explain(support, contra, signals, verdict, generated)
    note = ""
    if verdict == "insufficient":
        note = ("Found under a handle generated from the name: existence only, no evidence it is the person."
                if generated else "Same name only: not enough evidence to say this is the same person." if name_only
                else "Not enough evidence to say whether this is the same person.")
    elif verdict == "likely":
        note = "One strong signal. Look for a second, independent one before relying on it."
    return {"verdict": verdict, "verdict_title": title, "verdict_kind": kind, "score": round(e.confidence * 100),
            "tier": e.tier, "independent_signals": signals, "name_only": name_only, "note": note,
            "explanation": explanation,
            "support": [{"text": r.description, "weight": round(r.weight, 2), "feature": r.feature,
                         "strength": strength(r.feature, generated)} for r in support],
            "contradictions": [{"text": r.description, "weight": round(r.weight, 2), "feature": r.feature} for r in contra]}


CONFIDENCE_WORDS = {"confirmed": "Confirmed", "strong": "High", "likely": "Medium", "possible": "Low",
                    "insufficient": "Insufficient evidence", "rejected": "Rejected"}


def strength(feature: str, generated: bool = False) -> str:
    if generated and feature in CIRCULAR_FOR_GENERATED:
        return "weak"
    if feature in STRONG_FEATURES:
        return "strong"
    if feature in WEAK_ALONE:
        return "weak"
    if feature in MEDIUM_FEATURES:
        return "medium"
    return "weak"


def explain(support, contra, signals, verdict: str, generated: bool) -> dict:
    """Explainable evidence scoring: a confidence word with the reasons for and against, each labelled strong, medium or
    weak, and what is missing. Deliberately not a percentage: the weights are judgement calls, not calibrated."""
    against = [{"text": r.description, "strength": "contradiction"} for r in contra]
    strong_n = sum(1 for r in support if strength(r.feature, generated) == "strong")
    if verdict in ("likely", "possible", "insufficient") and len(signals) < 2:
        against.append({"text": "No second independent corroborating source", "strength": "missing"})
    if support and all(strength(r.feature, generated) == "weak" for r in support):
        against.append({"text": "Only weak signals (name, similar handle, rank, place): never enough on their own",
                        "strength": "missing"})
    if generated:
        against.append({"text": "Found under a handle generated from the name: the handle match is circular",
                        "strength": "missing"})
    return {"confidence": CONFIDENCE_WORDS[verdict], "strong_signals": strong_n, "independent_signals": len(signals),
            "for": [{"text": r.description, "strength": strength(r.feature, generated)} for r in support],
            "against": against,
            "rule": "High needs two or more independent non-name signals; one strong signal is Medium; weak signals alone "
                    "stay at Insufficient evidence."}


def candidate_row(g, e) -> dict:
    generated = _under_hypothesis(g, e)
    a = assess(e, generated)
    return {"id": e.id, "key": e.key, "type": e.type, "type_title": ENTITY_TYPES[e.type].title, "label": e.label,
            "platform": e.get("platform"), "platform_title": e.get("platform_title"), "username": e.get("username"),
            "url": e.get("profile_url") or e.get("url") or e.get("official_url"), "status": e.status,
            "origin": origin_of(g, e), "found_under_generated_handle": generated, **a}


def _under_hypothesis(g, e) -> bool:
    for r in g.edges(e.key):
        other = g.get(r.target if r.source == e.key else r.source)
        if other is not None and other.type == "username" and other.get("hypothesis"):
            return True
    return False


# --------------------------------------------------------------------------- candidate identities (clusters)

def identities(g) -> list[dict]:
    """Profiles grouped into candidate identities: cross-linked accounts, accounts the resolver links as the same owner
    (SAME_AS), and accounts sharing a handle. Each group says why it is a group and how strong its best member is."""
    cands = [e for e in g.entities.values() if e.type in IDENTITY_TYPES and e.key != g.target_key
             and ENTITY_TYPES[e.type].resolvable and e.status != "seed"]
    parent = {e.key: e.key for e in cands}
    why: dict[str, set[str]] = defaultdict(set)

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: str, b: str, reason: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
        why[find(a)].add(reason)

    keys = set(parent)
    for r in g.relationships.values():
        if r.status == "rejected" or r.source not in keys or r.target not in keys:
            continue
        if r.type == "SAME_AS":
            union(r.source, r.target, "the resolver links them as the same owner (inferred)")
        elif r.type == "LINKED_TO":
            union(r.source, r.target, "one profile links to the other (observed)")
    by_handle: dict[str, list[str]] = defaultdict(list)
    for e in cands:
        u = str(e.get("username") or "").casefold()
        if e.type == "account" and u:
            by_handle[u].append(e.key)
    for u, ks in by_handle.items():
        for k in ks[1:]:
            union(ks[0], k, f"same handle “{u}”")
    groups: dict[str, list] = defaultdict(list)
    for e in cands:
        groups[find(e.key)].append(e)
    out = []
    order = ["confirmed", "strong", "likely", "possible", "insufficient", "rejected"]
    for root, members in groups.items():
        rows = sorted((candidate_row(g, m) for m in members), key=lambda r: (order.index(r["verdict"]), -r["score"]))
        best = rows[0]
        reasons = sorted(why.get(root, set()))
        if len(rows) > 1 and best["verdict"] in ("possible", "likely", "strong"):
            reasons.append("each member keeps its own verdict: a group is not a confirmation")
        out.append({"id": best["id"], "verdict": best["verdict"], "verdict_title": best["verdict_title"],
                    "verdict_kind": best["verdict_kind"], "score": best["score"], "title": _group_title(rows),
                    "members": rows, "grouped_because": reasons})
    out.sort(key=lambda gr: (order.index(gr["verdict"]), -gr["score"], gr["title"].lower()))
    for i, gr in enumerate(out):
        gr["letter"] = chr(65 + i) if i < 26 else str(i + 1)
    return out


def _group_title(rows: list[dict]) -> str:
    handles = sorted({r["username"] for r in rows if r.get("username")})
    plats = [r["platform_title"] for r in rows if r.get("platform_title")]
    if handles:
        return f"“{handles[0]}”" + (f" + {len(handles) - 1} other handle(s)" if len(handles) > 1 else "") + \
            (f" on {', '.join(sorted(set(plats))[:4])}" if plats else "")
    return rows[0]["label"]


# --------------------------------------------------------------------------- platforms

CHECK_TITLES = {"found": "Found", "not_found": "Not found", "blocked": "Blocked", "error": "Error", "unknown": "Unknown",
                "skipped": "Skipped"}


def platform_table(g, user_platforms: tuple = ()) -> list[dict]:
    """Platform | Candidate | Status | Evidence | Confidence — one row per (platform, identifier) checked, plus the
    platforms that can only be checked by hand."""
    rows: dict[tuple[str, str], dict] = {}
    for c in g.checks.values():
        ident = g.get(f"username:{c.identifier.casefold()}") if c.identifier_type == "username" else None
        acc = g.get(c.account_key) if c.account_key else None
        key = (c.platform_title or P.title(c.platform), c.identifier.casefold())
        rank = {"found": 5, "not_found": 4, "blocked": 3, "error": 2, "unknown": 2, "skipped": 1}
        cur = rows.get(key)
        if cur is not None and rank.get(cur["check"], 0) >= rank.get(c.status, 0):
            continue
        a = assess(acc, _under_hypothesis(g, acc)) if acc is not None else None
        rows[key] = {
            "platform": key[0], "platform_id": c.platform, "candidate": c.identifier, "check": c.status,
            "status": CHECK_TITLES.get(c.status, c.status) if not (ident and ident.get("hypothesis") and c.status == "found")
            else "Found (generated handle)",
            "candidate_origin": origin_of(g, ident)["title"] if ident else "",
            "evidence": ("Account exists (site check)" + (f"; {len(a['independent_signals'])} identity signal(s)" if a else ""))
            if c.status == "found" else c.detail[:160],
            "verdict": a["verdict_title"] if a else "", "verdict_kind": a["verdict_kind"] if a else "",
            "confidence": a["score"] if a else None, "url": c.url, "account_id": acc.id if acc else None,
            "connector": c.connector, "checked_at": c.checked_at}
    for acc in g.of_type("account"):
        title = acc.get("platform_title") or P.title(str(acc.get("platform") or ""))
        key = (title, str(acc.get("username") or acc.label).casefold())
        if key in rows:
            continue
        a = assess(acc, _under_hypothesis(g, acc))
        rows[key] = {"platform": title, "platform_id": acc.get("platform"), "candidate": acc.get("username") or acc.label,
                     "check": "found", "status": "Found", "candidate_origin": origin_of(g, acc)["title"],
                     "evidence": "Profile reported by " + ", ".join(sorted({str(v) for v in acc.values("found_by")}) or
                                                                     {"a source"}),
                     "verdict": a["verdict_title"], "verdict_kind": a["verdict_kind"], "confidence": a["score"],
                     "url": acc.get("profile_url"), "account_id": acc.id, "connector": "", "checked_at": acc.first_seen}
    out = sorted(rows.values(), key=lambda r: ({"found": 0, "blocked": 1, "error": 2, "unknown": 2, "not_found": 3}.get(
        r["check"], 4), -(r["confidence"] or 0), r["platform"].lower()))
    return out[:400] + manual_rows(g, user_platforms)


def manual_rows(g, user_platforms: tuple = ()) -> list[dict]:
    """Platforms that forbid automated checks, as links for the usernames worth checking."""
    manual = []
    for u in g.of_type("username"):
        if u.active and (u.status == "seed" or u.confidence >= 0.5) and not u.get("hypothesis"):
            for link in P.manual_links(u.label, user_platforms)[:30]:
                manual.append({"platform": link["title"], "platform_id": link["platform"], "candidate": u.label,
                               "check": "manual", "status": "Manual check", "candidate_origin": origin_of(g, u)["title"],
                               "evidence": link.get("reason") or "This platform forbids automated checks: open the link",
                               "verdict": "", "verdict_kind": "", "confidence": None, "url": link["url"],
                               "account_id": None, "connector": "", "checked_at": None})
    return manual[:120]


# --------------------------------------------------------------------------- generated handles

def generated(rt) -> dict:
    g = rt.graph
    t = g.target
    names = []
    if t is not None and t.type == "person":
        names = [n for n in [*t.values("full_name"), *t.all_list("aliases")] if isinstance(n, str)] or \
            ([t.label] if t.label and not t.label.startswith("Unknown") else [])
    cands = [c.to_dict() for c in name_candidates(names, 10)] if names else []
    tested = {}
    for u in g.of_type("username"):
        if u.get("hypothesis"):
            found = [c for c in g.checks.values() if c.identifier.casefold() == u.value and c.status == "found"]
            done = [c for c in g.checks.values() if c.identifier.casefold() == u.value]
            tested[u.value] = {"entity_id": u.id, "checks": len(done), "found": len(found),
                               "platforms_found": sorted({c.platform_title or c.platform for c in found})[:12]}
    for c in cands:
        c["tested"] = tested.get(c["handle"])
    mode = DEPTH_MODE.get(rt.limits.depth, "balanced")
    return {"names": names, "candidates": cands, "testable": MODES[mode]["candidates"] or 4, "mode": mode,
            "tested_any": bool(tested)}


# --------------------------------------------------------------------------- next best actions

def _planned(rt, e) -> bool:
    """Something already ran (or is queued) on this entity."""
    return any(t.entity_key == e.key and t.status in ("queued", "running", "done") for t in rt.tasks.values())


def _free_transforms(engine, rt, e) -> list[str]:
    """Free, available, automatic transforms that have not run on this entity yet."""
    out = []
    for t in REG.transforms_for(e.type):
        conn = REG.REGISTRY[t.connector]
        if not t.auto or conn.manifest.requires_subscription or engine.tried(rt, t.name, e.key):
            continue
        status, _ = engine._connector_available(rt, conn)
        if status == "available":
            out.append(t.name)
    return out


def next_actions(rt, engine, cfg, user_platforms: tuple = ()) -> list[dict]:
    g = rt.graph
    acts: list[dict] = []

    def add(**a) -> None:
        a.setdefault("executable", True)
        a.setdefault("requires_confirmation", False)
        a.setdefault("cost_tier", "FREE")
        acts.append(a)

    # discovered pivots: identifiers found on a profile are usually better than the name
    for e in g.entities.values():
        if not e.active or e.status == "seed" or e.get("hypothesis") or e.type not in ("username", "email", "domain", "phone"):
            continue
        if _planned(rt, e) or e.value_score < 0.35:
            continue
        free = _free_transforms(engine, rt, e)
        if not free:
            continue
        src = origin_of(g, e)
        what = {"username": "username", "email": "email address", "domain": "domain", "phone": "phone number"}[e.type]
        value = "high" if e.type in ("email", "phone") or e.value_score >= 0.7 else "medium"
        add(id=f"pivot:{e.id}", kind="pivot", title=f"Search the {what} “{e.label}” across configured sources",
            reason=f"{src['title']}. A discovered {what} is a stronger pivot than another name search.",
            expected_value=value, entity_id=e.id, transforms=free, count=len(free))
    # identity discovery through web search (stages 4–9): the name on each platform, then discovered handles
    from ..discovery import router as ROUTER
    primary = engine.search_primary(rt)
    t = g.target
    if t is not None and t.type == "person" and t.get("full_name"):
        disc = f"{primary}.profile_discovery" if primary else None
        if disc and not engine.tried(rt, disc, t.key):
            add(id=f"discover:{t.id}", kind="discover", title=f"Search the web for “{t.label}” on each platform",
                reason="Finds profiles whose handle could not be guessed from the name: the search result shows the name, "
                       "its URL shows the handle, which then becomes a new pivot.",
                expected_value="high", entity_id=t.id, transforms=[disc, f"{primary}.person_web"], cost_tier="API KEY"
                if REG.REGISTRY[primary].manifest.requires_subscription else "FREE",
                provider=REG.REGISTRY[primary].manifest.title)
        elif not primary:
            add(id="web_unavailable", kind="advice", title="Web discovery unavailable — API-only search was used",
                reason=ROUTER.unavailable_message(cfg) or "No web-search provider is available for this investigation.",
                expected_value="high", executable=False, cost_tier="MANUAL", url="#/sources")
    for u in g.of_type("username"):
        if not u.active or u.get("hypothesis") or u.get("discovery") != "search_result" or not primary:
            continue
        name = f"{primary}.profile_discovery"
        free = _free_transforms(engine, rt, u)
        todo = ([] if engine.tried(rt, name, u.key) else [name]) + free
        if todo and not _planned(rt, u):
            add(id=f"pivot_handle:{u.id}", kind="discover", title=f"Search the discovered handle “{u.label}” on other platforms",
                reason=f"Found in a search result on {u.get('discovered_on')} (rank {u.get('search_rank')}). Checking it "
                       "elsewhere shows whether the same person uses it on other platforms.",
                expected_value="high" if u.get("pivot") else "medium", entity_id=u.id, transforms=todo, count=len(todo),
                cost_tier="API KEY")
    # strong accounts not expanded yet
    for e in g.of_type("account"):
        if e.active and e.status != "seed" and e.tier in ("high", "medium") and not _planned(rt, e):
            free = _free_transforms(engine, rt, e)
            if free:
                add(id=f"expand:{e.id}", kind="expand", title=f"Read the full profile of {e.label}",
                    reason="A possible or likely match: its profile may list a website, email or links that confirm or "
                           "rule it out.", expected_value="medium", entity_id=e.id, transforms=free, count=len(free))
    # name-derived handles
    gen = generated(rt)
    if gen["candidates"] and not gen["tested_any"] and \
            not any(u.status == "seed" for u in g.of_type("username", "account")):
        n = gen["testable"]
        add(id="test_candidates", kind="test_candidates", title=f"Test {n} name-derived handles (hypotheses)",
            reason="No username is known yet. Common handle patterns from the name are checked on platforms; an account "
                   "found this way is only a lead until independent evidence links it.",
            expected_value="medium" if g.target and not g.of_type("account") else "low", count=n,
            handles=[c["handle"] for c in gen["candidates"][:n]])
    # review and caution on the strongest candidates
    for grp in identities(g)[:3]:
        best = grp["members"][0]
        if best["verdict"] in ("strong", "likely") and best["status"] not in ("confirmed", "separated", "ignored"):
            add(id=f"review:{best['id']}", kind="review", title=f"Review candidate {grp['letter']}: {best['label']}",
                reason="Nothing is confirmed automatically. Confirm it if the evidence convinces you, or separate it.",
                expected_value="high", entity_id=best["id"], executable=False, cost_tier="LOCAL")
        if best["verdict"] == "likely":
            add(id=f"corroborate:{best['id']}", kind="advice", title=f"Find a second independent signal for {best['label']}",
                reason="One strong signal can be a coincidence or a shared account. An email, a website or a cross-link "
                       "from another profile would settle it.", expected_value="medium", entity_id=best["id"],
                executable=False, cost_tier="LOCAL")
    # verification required
    for v in (rt.data.get("verification") or {}).values():
        add(id=f"verify:{v['connector']}", kind="verification", title=f"Verification required — manual action needed: {v['title']}",
            reason="The source showed a CAPTCHA or bot check. Sextant does not try to get past it.", expected_value="medium",
            url=v["url"], connector=v["connector"], entity_id=v.get("entity"), executable=False, cost_tier="MANUAL")
        add(id=f"fallback:{v['connector']}", kind="fallback", title=f"Try another configured source instead of {v['title']}",
            reason="Runs the same lookup through the other free sources of the same category.", expected_value="medium",
            connector=v["connector"], entity_id=v.get("entity"))
    # paid sources that apply but did not run
    allow_paid = cfg.get_bool("allow_paid_sources")
    for e in [x for x in g.entities.values() if x.status == "seed" or x.key == g.target_key]:
        for t in REG.transforms_for(e.type):
            conn = REG.REGISTRY[t.connector]
            m = conn.manifest
            if not m.requires_subscription or engine.tried(rt, t.name, e.key):
                continue
            status, _ = engine._connector_available(rt, conn)
            if status != "available":
                continue
            add(id=f"paid:{t.name}:{e.id}", kind="paid", title=f"Run {m.title} on {e.label} (paid)",
                reason=f"{m.category_title}: configured with your subscription. {m.cost}"
                       + (f" Estimated ${m.cost_per_call_usd:g} per call." if m.cost_per_call_usd else ""),
                expected_value="medium", entity_id=e.id, transforms=[t.name], requires_confirmation=True,
                cost_tier="PAID", executable=allow_paid)
    # manual platform checks and manual official sources
    manual = manual_rows(g, user_platforms)
    if manual:
        names = sorted({r["platform"] for r in manual})
        add(id="manual_platforms", kind="manual", title=f"Check {len(names)} platform(s) by hand: {', '.join(names[:5])}"
            + ("…" if len(names) > 5 else ""), reason="These platforms forbid automated checks; the links are ready in the "
            "platform table.", expected_value="medium", executable=False, cost_tier="MANUAL")
    from ..registry import jurisdictions as J
    for code in rt.info.countries:
        j = J.jurisdictions().get(code)
        add(id=f"jurisdiction:{code}", kind="jurisdiction", title=f"Open the official sources for {j.name if j else code}",
            reason="Registers and courts without an API are links to search yourself; the country page lists them with "
                   "what each publishes.", expected_value="medium", url=f"#/coverage/{code}", executable=False,
            cost_tier="MANUAL")
    if rt.status == "limit_reached":
        add(id="continue", kind="continue", title="Continue the investigation with raised limits",
            reason=f"It stopped at a limit ({rt.stop_reason}); pending leads were not followed.", expected_value="medium")
    from ..costs import ledger as LEDGER
    for a in acts:
        est = 0.0
        for tn in a.get("transforms") or []:
            tr = REG.TRANSFORMS.get(tn)
            if tr is not None:
                est += LEDGER.task_estimate(tr, REG.REGISTRY[tr.connector], cfg)
        a["estimated_cost_usd"] = round(est, 4) if a.get("transforms") else (0.0 if a["executable"] else None)
        a["potential_value"] = a["expected_value"].title()
    acts.sort(key=lambda a: (not a["executable"] and a["kind"] not in ("review", "verification"),
                             VALUE_RANK[a["expected_value"]], a["kind"] == "paid", a["title"]))
    return acts


# --------------------------------------------------------------------------- memory and summary

def searched(rt, limit: int = 300) -> dict:
    runs = sorted(rt.runs.values(), key=lambda r: r.started_at, reverse=True)
    by_conn: dict[str, dict] = {}
    for r in runs:
        d = by_conn.setdefault(r.connector, {"connector": r.connector, "title": REG.REGISTRY[r.connector].manifest.title
                                             if r.connector in REG.REGISTRY else r.connector, "runs": 0, "statuses": {}})
        d["runs"] += 1
        d["statuses"][r.status] = d["statuses"].get(r.status, 0) + 1
    return {"runs": [{"at": r.started_at, "connector": r.connector, "transform": r.transform, "input": r.input_label,
                      "sent": r.sent, "destinations": r.destinations, "status": r.status, "message": r.message[:200],
                      "trigger": r.trigger, "requests": r.requests} for r in runs[:limit]],
            "total_runs": len(runs), "by_connector": sorted(by_conn.values(), key=lambda d: -d["runs"])}


def progress(rt, gen: dict, ids: list[dict]) -> list[dict]:
    g = rt.graph
    running = rt.status == "running"
    pending = len(rt.queue)
    runs = len(rt.runs)

    def st(done: bool, active: bool = False) -> str:
        return "done" if done else "running" if active and running else "pending"
    return [
        {"step": "Normalize identifiers", "status": "done"},
        {"step": "Generate candidate usernames", "status": "done" if gen["candidates"] else "skipped",
         "note": f"{len(gen['candidates'])} hypotheses" if gen["candidates"] else "no full name to derive from"},
        {"step": "Search configured sources", "status": st(runs > 0 and not pending, True), "note": f"{runs} runs, {pending} queued"},
        {"step": "Platform checks", "status": st(bool(g.checks) and not pending, True), "note": f"{len(g.checks)} checks"},
        {"step": "Correlate evidence", "status": st(runs > 0 and any(e.tier for e in g.entities.values()), True)},
        {"step": "Build candidate identities", "status": st(bool(ids) and not pending, True), "note": f"{len(ids)} candidate(s)"},
        {"step": "Your review", "status": "done" if any(e.status in ("confirmed", "separated") for e in g.entities.values())
         else "pending"},
    ]


def _identifier_rows(rt) -> dict:
    from . import identifiers as IDS
    items = IDS.rows(rt)
    return {"items": items, "counts": IDS.counts(items), "states": IDS.STATES}


def _costs(rt, engine, cfg) -> dict:
    from ..costs import ledger as LEDGER
    return LEDGER.investigation_costs(rt, engine.db, cfg)


def analyze(rt, engine, cfg, user_platforms: tuple = ()) -> dict:
    g = rt.graph
    ids = identities(g)
    counts = defaultdict(int)
    for grp in ids:
        counts[grp["verdict"]] += 1
    gen = generated(rt)
    acts = next_actions(rt, engine, cfg, user_platforms)
    plats = platform_table(g, user_platforms)
    best = ids[0] if ids else None
    if best is None:
        headline = "No candidate profile yet."
    elif best["verdict"] in ("confirmed", "strong"):
        m = best["members"][0]
        headline = f"Strongest candidate: {m['label']} ({m['verdict_title'].lower()}, {len(m['independent_signals'])} " \
                   "independent signal(s))."
    elif best["verdict"] in ("likely", "possible"):
        headline = f"Best candidate so far: {best['members'][0]['label']} ({best['verdict_title'].lower()}). Not enough " \
                   "evidence yet to call it the same person."
    else:
        headline = "Insufficient evidence: nothing found so far links a profile to the person beyond weak signals."
    from ..discovery import stages as STAGES
    disc = STAGES.report(rt, cfg, ids)
    if disc["web_discovery"]["unavailable_message"] and g.target is not None and g.target.type == "person":
        headline += " Web discovery unavailable — API-only search was used, so a missing profile is not evidence."
    mode = DEPTH_MODE.get(rt.limits.depth, "balanced")
    return {
        "query": rt.title, "input": rt.data.get("input"), "status": rt.status, "depth": rt.limits.depth,
        "max_depth": rt.max_depth, "mode": {"id": mode, **MODES[mode]}, "headline": headline,
        "counts": {"identities": len(ids), **{k: counts.get(k, 0) for k in VERDICTS},
                   "platform_found": sum(1 for r in plats if r["check"] == "found"),
                   "websites": len(g.of_type("website")), "documents": len(g.of_type("document")),
                   "organizations": len(g.of_type("organization")), "domains": len(g.of_type("domain")),
                   "emails": len(g.of_type("email")), "public_records": len(g.of_type("public_record"))},
        "progress": progress(rt, gen, ids), "identities": ids[:60], "platforms": plats, "generated": gen,
        "discovery": disc, "identifiers": _identifier_rows(rt), "costs": _costs(rt, engine, cfg),
        "next_actions": acts, "verification": list((rt.data.get("verification") or {}).values()),
        "paid_spend_usd": engine.paid_spend(rt),
        "rejected": [candidate_row(g, e) for e in g.entities.values() if e.status in ("separated", "ignored")][:50],
        "searched": searched(rt, 150),
        "source_kinds": SOURCE_KINDS,
        "legend": {"provided": "Typed by you", "observed": "Reported by a source", "derived": "Computed locally from "
                   "observed data", "generated": "A hypothesis Sextant generated to test — not evidence",
                   "inferred": "Concluded by the resolver from several observations"},
        "disclaimer": "Scores are a transparent heuristic, not a probability. A missing result never proves that "
                      "information does not exist, and only you confirm an identity.",
    }
