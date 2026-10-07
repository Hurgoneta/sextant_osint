"""Jurisdiction audit — what the catalog knows about one jurisdiction, where the gaps are, and what could fill them.

The audit is **read-only**: it reads the current catalog (built-in sources, the investigator's own sources from the
database, connector states, link checks, discovered datasets, the last live connector search) and returns a report.
It never adds, edits or deletes anything. Recommended additions are offered to the investigator, who reviews them and
saves them through the normal source form.

Nothing is invented. Every recommendation is an entry from a maintained dataset shipped with Sextant (GLEIF
registration authorities, OpenSanctions, Wikidata, IANA) or from the investigator's last live Wikidata search, with
its provenance. Anything already in the catalog (same address, or a dataset entry that corroborates a built-in source)
is not recommended. Every recommendation is marked *Not verified*: datasets say an institution exists, not what it
publishes or whether it can be searched by name. Confidence is computed from the evidence and its reasons are listed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import catalog as CAT
from . import countries as C
from . import discovered as D
from . import jurisdictions as J
from .connectors import REGISTRY, STATUS_TITLES

CORE_GROUPS = ("corporate", "judicial", "public_records", "regulatory", "procurement", "news", "domains")
RATINGS = {
    "automated": ("Automated", "good"),
    "automatable": ("Automation exists: needs configuration", "warn"),
    "global": ("Covered by global connectors only", "info"),
    "links": ("Partial: links only (manual)", "info"),
    "potential": ("Limited: unreviewed dataset entries only", "warn"),
    "gap": ("Gap: nothing known", "bad"),
}
# global connectors with data filtered to the country (coverage_model.global_capabilities) → jurisdiction group
GLOBAL_CONNECTOR_GROUP = {"gdelt": "news", "rdap": "domains", "dns": "domains", "ripestat": "infrastructure",
                          "opensanctions": "regulatory", "gleif": "corporate", "phone_offline": "other"}
NOT_OFFICIAL_KINDS = ("commercial_provider", "aggregator", "people_directory", "business_directory", "search_engine",
                      "forum", "news_media", "news_agency")
DATASET_TRUST = {"gleif_ra": "high", "opensanctions": "high", "iana": "high", "rir": "high", "wikidata": "medium",
                 "gdelt": "medium", "wikidata_live": "medium"}
LEVELS = ("low", "medium", "high")
OK_LINK = ("ok", "reachable_refuses_bots")
LINK_PROBLEM = ("tls_problem", "server_error", "unreachable")
UNAVAILABLE = ("needs_credentials", "needs_subscription", "disabled", "not_installed")
MAX_RECOMMENDATIONS = 40


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _evidence(v: dict | None) -> tuple[str, str]:
    """(label, badge kind) of a link check result."""
    result = (v or {}).get("result")
    if not result:
        return "Not verified", ""
    if result in OK_LINK:
        return "Verified link", "good"
    if result == "dead":
        return "Dead link", "bad"
    return "Link problem", "warn"


def _nature(kind: str, official: bool | None = None, access: str = "") -> str:
    if kind == "commercial_provider" or access == "paid":
        return "Commercial"
    if official is True or (official is None and kind not in NOT_OFFICIAL_KINDS):
        return "Official / public"
    return "Third-party"


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def audit(code: str, ctx, snap: CAT.Snapshot, live: dict | None = None) -> dict:
    """``ctx``: a CoverageContext for the current configuration; ``snap``: the catalog's user layer; ``live``: the
    stored result of the last live connector search for this country (``discovery:<CC>``), if any."""
    code = code.upper()
    js = J.jurisdictions()
    j = js.get(code)
    if j is None:
        raise KeyError(code)
    country = C.get(code)
    states = ctx.states
    vsrc = ctx.verification.get("sources") or {}
    vdisc = ctx.verification.get("discovered") or {}

    # ------------------------------------------------------------------ what the catalog holds now
    current: list[dict] = []
    for s in j.sources:
        v = vsrc.get(s.id)
        ev, ek = _evidence(v)
        auto = ("automated" if s.connector and states.get(s.connector) == "available" else
                "automatable" if s.connector and states.get(s.connector) in UNAVAILABLE else "manual")
        current.append({"id": s.id, "title": s.title, "url": s.url, "origin": "system",
                        "layer": "official" if s.provenance == "curated" else "regional", "group": s.group,
                        "kind_title": J.SOURCE_KINDS.get(s.kind, s.kind), "nature": _nature(s.kind),
                        "automation": auto, "connector": s.connector, "evidence": ev, "evidence_kind": ek,
                        "checked_at": (v or {}).get("checked_at"), "name_search": s.name_search})
    for b, s in J.bloc_sources(code):
        v = vsrc.get(s.id)
        ev, ek = _evidence(v)
        current.append({"id": s.id, "title": s.title, "url": s.url, "origin": "system", "layer": f"bloc ({b.id})",
                        "group": s.group, "kind_title": J.SOURCE_KINDS.get(s.kind, s.kind), "nature": _nature(s.kind),
                        "automation": "automated" if s.connector and states.get(s.connector) == "available" else "manual",
                        "connector": s.connector, "evidence": ev, "evidence_kind": ek,
                        "checked_at": (v or {}).get("checked_at"), "name_search": s.name_search})
    mine = snap.for_jurisdiction(code)
    for s in mine:
        ev, ek = _evidence(s.get("link_check"))
        a = CAT.automation(s, states)
        auto = ("automated" if a["mode"] == "connector" and a.get("connector_status") == "available" else
                "automatable" if a["mode"] == "connector" else "manual")
        current.append({"id": s["id"], "title": s["title"], "url": s["url"], "origin": "user", "layer": "yours",
                        "group": CAT.group_of(s, snap), "kind_title": CAT.public(s, snap)["category_title"],
                        "nature": _nature(s.get("kind") or "", bool(s.get("official")) or None, s["access"]),
                        "automation": auto, "connector": s.get("connector") or "", "evidence": ev, "evidence_kind": ek,
                        "checked_at": (s.get("link_check") or {}).get("checked_at"), "name_search": bool(s.get("name_search")),
                        "active": CAT.active(s), "review": CAT.REVIEW[s["review"]]})
    disc_all = D.for_country(code) if len(code) == 2 else []
    potential = [d for d in disc_all if not d.corroborates]

    # ------------------------------------------------------------------ connectors per group (country-level or global)
    conn_groups: dict[str, dict[str, str]] = {}
    for name in ctx.country_connectors(code):
        conn_groups.setdefault(CAT.CATEGORY_GROUP.get(REGISTRY[name].manifest.category, "other"), {})[name] = "country"
    for c in current:
        if c["connector"] and c["connector"] in REGISTRY:
            conn_groups.setdefault(c["group"], {})[c["connector"]] = "country"
    global_caps = []
    if len(code) == 2 and country is not None:
        from .coverage_model import global_capabilities
        global_caps = global_capabilities(code, ctx)
        for g in global_caps:
            conn_groups.setdefault(GLOBAL_CONNECTOR_GROUP.get(g["connector"], "other"), {}).setdefault(g["connector"], "global")

    # ------------------------------------------------------------------ coverage per group
    coverage = []
    for gid, gtitle in J.SOURCE_GROUPS.items():
        items = [c for c in current if c["group"] == gid and c.get("active", True)]
        scope = conn_groups.get(gid, {})
        conns = sorted(scope)
        ready = [n for n in conns if states.get(n) == "available" and scope[n] == "country"]
        needs = [n for n in conns if states.get(n) in UNAVAILABLE and scope[n] == "country"]
        ready_global = [n for n in conns if states.get(n) == "available" and scope[n] == "global"]
        pot = [d for d in potential if d.group == gid]
        if not items and not conns and not pot and gid not in CORE_GROUPS:
            continue
        if ready:
            rating = "automated"
        elif needs:
            rating = "automatable"
        elif ready_global:
            rating = "global"
        elif items:
            rating = "links"
        elif pot:
            rating = "potential"
        else:
            rating = "gap"
        notes = []
        if gid == "judicial" and j.judgments_anonymized:
            notes.append("Judgments are published without party names by law: name searches are impossible by design.")
        if gid == "public_records" and not j.criminal_records_public and len(code) == 2:
            notes.append("Criminal records are not public: only the data subject can request an extract.")
        coverage.append({
            "group": gid, "title": gtitle, "core": gid in CORE_GROUPS, "rating": rating,
            "rating_title": RATINGS[rating][0], "rating_kind": RATINGS[rating][1],
            "system": sum(1 for c in items if c["origin"] == "system"), "user": sum(1 for c in items if c["origin"] == "user"),
            "verified_links": sum(1 for c in items if c["evidence"] == "Verified link"),
            "unverified_links": sum(1 for c in items if c["evidence"] == "Not verified"),
            "connectors": [{"name": n, "title": REGISTRY[n].manifest.title, "status": states.get(n, ""), "scope": scope[n],
                            "status_title": STATUS_TITLES.get(states.get(n, ""), "")} for n in conns],
            "potential": len(pot), "notes": notes,
        })
    by_rating = {c["group"]: c["rating"] for c in coverage}
    gaps = [{"group": c["group"], "title": c["title"], "rating": c["rating"], "rating_title": c["rating_title"],
             "message": (f"No {c['title'].lower()} source is known for {j.name}." if c["rating"] == "gap" else
                         f"Only unreviewed dataset entries ({c['potential']}) for {c['title'].lower()}."),
             "notes": c["notes"]}
            for c in coverage if c["core"] and c["rating"] in ("gap", "potential")]

    # ------------------------------------------------------------------ potential duplicates
    entries = list(current) + [{"id": d.id, "title": d.title, "url": d.url, "origin": "dataset",
                                       "layer": f"potential ({D.DATASETS.get(d.dataset, d.dataset)})"} for d in potential]
    duplicates: list[dict] = []
    seen_pairs: set[frozenset] = set()

    def dup(a: dict, b: dict, reason: str, strength: str) -> None:
        pair = frozenset((a["id"], b["id"]))
        if a["id"] == b["id"] or pair in seen_pairs:
            return
        if "user" not in (a["origin"], b["origin"]) and "dataset" in (a["origin"], b["origin"]):
            return                               # dataset vs built-in: handled by corroboration, not a duplicate
        seen_pairs.add(pair)
        mine_ = [x for x in (a, b) if x["origin"] == "user"]
        duplicates.append({"reason": reason, "strength": strength,
                           "items": [{k: x.get(k) for k in ("id", "title", "url", "origin", "layer")} for x in (a, b)],
                           "suggestion": ("Your source repeats one that is already listed: delete or disable your copy."
                                          if len(mine_) == 1 and "dataset" not in (a["origin"], b["origin"]) else
                                          "Two of your sources look the same: keep one." if len(mine_) == 2 else
                                          "You already added this dataset entry." if mine_ else
                                          "Two built-in entries share this address (shared portal or alternative name).")})

    keys = [(CAT.url_key(e["url"]), CAT.title_key(e["title"])) for e in entries]
    for i, a in enumerate(entries):
        ka, ta = keys[i]
        for jdx in range(i + 1, len(entries)):
            b = entries[jdx]
            if a["origin"] == b["origin"] == "dataset":
                continue
            kb, tb = keys[jdx]
            if ka and ka == kb:
                if {"user", "dataset"} == {a["origin"], b["origin"]}:
                    continue                     # you added a dataset entry: that is the point, not a duplicate
                dup(a, b, "Same address", "likely")
            elif "user" in (a["origin"], b["origin"]) and "dataset" not in (a["origin"], b["origin"]) and \
                    ka.split("/", 1)[0] == kb.split("/", 1)[0] and ("/" not in ka or "/" not in kb):
                dup(a, b, "Same website", "possible")
            elif len(ta) >= 4 and ta == tb and "dataset" not in (a["origin"], b["origin"]):
                dup(a, b, "Same name", "likely")

    # ------------------------------------------------------------------ recommendations (evidence only)
    known = {CAT.url_key(c["url"]) for c in current}
    known |= {CAT.url_key(s["url"]) for s in snap.sources}
    known |= {CAT.url_key(REGISTRY[n].manifest.website) for n in REGISTRY if REGISTRY[n].manifest.website}
    ignored_ids = {item for cc, item in snap.ignores if cc == code}
    info = D.dataset_info()
    cands: list[dict] = []
    ignored: list[dict] = []
    for d in potential:
        if d.connector:
            continue                             # already queried through a connector (see the global connectors)
        cands.append({"id": d.id, "title": d.title, "url": d.url, "kind": d.kind, "dataset": d.dataset,
                      "dataset_title": D.DATASETS.get(d.dataset, d.dataset), "dataset_ref": d.dataset_ref,
                      "retrieved_at": (info.get(d.dataset) or {}).get("retrieved_at"),
                      "check": vdisc.get(D.verification_key(d)), "notes": d.notes})
    if live:
        for w in live.get("new_sources") or []:
            cands.append({"id": w["id"], "title": w["title"], "url": w["url"], "kind": w.get("kind") or "government_portal",
                          "dataset": "wikidata_live", "dataset_title": "Wikidata (your live connector search)",
                          "dataset_ref": w.get("qid", ""), "retrieved_at": live.get("ran_at"),
                          "check": (live.get("checks") or {}).get(w["id"]), "notes": ""})
    recs: list[dict] = []
    seen_urls: set[str] = set()
    place = j.name
    for c in cands:
        key = CAT.url_key(c["url"])
        if key in known or key in seen_urls:
            continue
        seen_urls.add(key)
        group = J.KIND_GROUP.get(c["kind"], "government")
        category = CAT.category_for_kind(c["kind"])
        kind_title = J.SOURCE_KINDS.get(c["kind"], c["kind"])
        why = {"gleif_ra": f"GLEIF lists it as a registration authority for legal entities in {place} ({c['dataset_ref']}).",
               "opensanctions": f"OpenSanctions indexes it as an official list published in {place} ({c['dataset_ref']}).",
               "iana": f"IANA lists it as the registry of {c['dataset_ref']}.",
               "wikidata": f"Wikidata lists it as {kind_title.lower()} in {place} ({c['dataset_ref']}).",
               "wikidata_live": f"Your live Wikidata search found it as {kind_title.lower()} in {place} ({c['dataset_ref']})."
               }.get(c["dataset"], f"Listed by {c['dataset_title']}.")
        level = LEVELS.index(DATASET_TRUST.get(c["dataset"], "medium"))
        reasons = [f"Source of the suggestion: {c['dataset_title']}"
                   + (f", snapshot of {str(c['retrieved_at'])[:10]}" if c.get("retrieved_at") else "")]
        result = (c.get("check") or {}).get("result")
        if result in OK_LINK:
            reasons.append(f"The link answered at the last check ({str(c['check'].get('checked_at', ''))[:10]})")
        elif result == "dead":
            level = 0
            reasons.append("The link was dead at the last check")
        elif result in LINK_PROBLEM:
            level = max(0, level - 1)
            reasons.append(f"The link had a problem at the last check ({result.replace('_', ' ')})")
        else:
            level = min(level, 1)
            reasons.append("The link has not been checked")
        rating = by_rating.get(group)
        if group in CORE_GROUPS and rating in ("gap", "potential", None):
            priority, fills = 0, f"Fills a gap: {J.SOURCE_GROUPS[group].lower()} ({RATINGS.get(rating or 'gap')[0].lower()})"
        elif group in CORE_GROUPS and rating == "links":
            priority, fills = 1, f"Adds to partial coverage: {J.SOURCE_GROUPS[group].lower()}"
        elif group in CORE_GROUPS and rating == "global":
            priority, fills = 1, f"Adds a {place}-specific source: {J.SOURCE_GROUPS[group].lower()} is covered by global " \
                                 "connectors only"
        else:
            priority, fills = 2, f"Adds a {kind_title.lower()}"
        rec = {
            "id": c["id"], "title": c["title"], "url": c["url"], "jurisdiction": code, "jurisdiction_name": place,
            "kind": c["kind"], "kind_title": kind_title, "group": group, "group_title": J.SOURCE_GROUPS[group],
            "category": category, "type": "reference", "type_title": CAT.SOURCE_TYPES["reference"]["title"],
            "reason": f"{fills}. {why}", "provenance": {"dataset": c["dataset_title"], "ref": c["dataset_ref"],
                                                       "retrieved_at": c.get("retrieved_at")},
            "confidence": LEVELS[level], "confidence_reasons": reasons, "status": "Not verified",
            "link_check": c.get("check"), "priority": priority, "nature": _nature(c["kind"]),
            "prefill": {"title": c["title"][:200], "url": c["url"], "category": category, "kind": c["kind"],
                        "jurisdictions": [code], "source_type": "reference", "access": "unknown",
                        "official": c["kind"] not in NOT_OFFICIAL_KINDS,
                        "notes": f"Suggested by the jurisdiction audit. {why}"[:2000]},
        }
        (ignored if c["id"] in ignored_ids else recs).append(rec)
    recs.sort(key=lambda r: (r["priority"], -LEVELS.index(r["confidence"]), list(J.SOURCE_GROUPS).index(r["group"]),
                             r["title"].lower()))

    # ------------------------------------------------------------------ improvements to what exists
    improvements: list[dict] = []
    for name in sorted({n for g in conn_groups.values() for n in g}):  # every connector relevant here
        st = states.get(name)
        m = REGISTRY[name].manifest
        if st in ("needs_credentials", "needs_subscription"):
            improvements.append({"kind": "configure", "title": f"Add the {'subscription' if st == 'needs_subscription' else 'API key'} "
                                                               f"for {m.title} so it can run for {place}",
                                 "href": f"#/sources/{name}"})
        elif st == "disabled":
            improvements.append({"kind": "enable", "title": f"{m.title} is switched off"
                                                            + (f" ({m.opt_in_reason})" if m.opt_in_reason else ""),
                                 "href": f"#/sources/{name}"})
        elif st == "not_installed":
            improvements.append({"kind": "install", "title": f"{m.title} needs an external tool that is not installed",
                                 "href": f"#/sources/{name}"})
    for c in current:
        if c["origin"] == "system" and c["evidence"] in ("Dead link", "Link problem"):
            improvements.append({"kind": "link", "title": f"Official link “{c['title']}” failed its last check "
                                                          f"({c['evidence'].lower()}); it may have moved",
                                 "href": c["url"]})
    for s in mine:
        lc = s.get("link_check")
        if not s["enabled"]:
            improvements.append({"kind": "user", "title": f"Your source “{s['title']}” is switched off",
                                 "href": f"#/sources/{s['id']}"})
        elif lc and lc.get("result") not in OK_LINK:
            improvements.append({"kind": "user", "title": f"Your source “{s['title']}” failed its link check "
                                                          f"({str(lc.get('result', '')).replace('_', ' ')})",
                                 "href": f"#/sources/{s['id']}"})
    unchecked = [s for s in mine if s["enabled"] and not s.get("link_check")]
    if unchecked:
        improvements.append({"kind": "user", "title": f"{len(unchecked)} of your sources were never link-checked: "
                                                      + ", ".join(s["title"] for s in unchecked[:5]),
                             "href": f"#/sources/{unchecked[0]['id']}"})
    if len(code) == 2 and country is not None:
        age = _age_days((live or {}).get("ran_at"))
        if live is None:
            improvements.append({"kind": "discover", "title": "Run “Search for connectors” to look for institutions added to "
                                                              "Wikidata since the shipped snapshot (sends no personal data)",
                                 "href": f"#/coverage/{code}"})
        elif age is not None and age > 90:
            improvements.append({"kind": "discover", "title": f"The last connector search is {age} days old",
                                 "href": f"#/coverage/{code}"})

    snapshot_dates = {k: v.get("retrieved_at") for k, v in info.items()}
    return {
        "code": code, "name": j.name, "audited_at": _now(), "read_only": True,
        "read_from": {"catalog_version": snap.version, "your_sources": len(mine),
                      "built_in_sources": sum(1 for c in current if c["origin"] == "system"),
                      "dataset_entries": len(disc_all), "potential_sources": len(potential),
                      "country_connectors": len(ctx.country_connectors(code)), "global_connectors": len(global_caps),
                      "links_checked_at": ctx.verification.get("checked_at"),
                      "live_search_at": (live or {}).get("ran_at"), "datasets": snapshot_dates},
        "legal": {"notes": j.legal_notes, "criminal_records_public": j.criminal_records_public,
                  "judgments_anonymized": j.judgments_anonymized},
        "coverage": coverage, "gaps": gaps, "sources": current,
        "global_connectors": [{"connector": g["connector"], "what": g["what"], "status": g.get("status"),
                               "group": GLOBAL_CONNECTOR_GROUP.get(g["connector"], "other")} for g in global_caps],
        "duplicates": duplicates, "recommendations": recs[:MAX_RECOMMENDATIONS], "recommendations_total": len(recs),
        "ignored": ignored, "improvements": improvements,
        "legend": {"Verified link": "The address answered at its last check (not a review of the content)",
                   "Not verified": "Nobody has checked this address yet",
                   "Automated": "A connector of this installation queries it",
                   "Manual": "A link you open and search yourself",
                   "Official / public": "Published by a public authority",
                   "Third-party": "Run by a private party (aggregator, media, community)",
                   "Commercial": "Paid service"},
        "disclaimer": ("The audit only reads the catalog; it changes nothing. Suggestions come from maintained datasets "
                       "and are not verified: open each one, check what it publishes, then add it if useful."),
    }


__all__ = ["audit", "RATINGS", "CORE_GROUPS"]
