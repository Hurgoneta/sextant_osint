"""Investigation plan: which sources the assistant would use for an input, why, at what cost, and in what order.

Everything comes from the registries of this installation: connectors and their live status (keys, subscriptions,
switches), the sources of the jurisdictions involved (built-in and the investigator's own), the jurisdiction audit's
coverage ratings, and the platform registry. Nothing is recommended that the catalog does not contain. Free and local
sources come first; paid ones follow the "Allow paid sources" setting and are never run without the investigator's
configuration. A jurisdiction inferred from the input is labelled as inferred, never as confirmed.
"""

from __future__ import annotations

from ..registry import catalog as CAT
from ..registry import catalog_audit as AUDIT
from ..registry import connectors as REG
from ..registry import coverage_model as CM
from ..registry import jurisdictions as J
from ..registry import platforms as P
from .names import STATUS as CANDIDATE_STATUS
from .names import candidates as name_candidates

MODES = {
    "quick": {"title": "Quick", "depth": "1", "candidates": 0,
              "help": "Direct identifiers only, one hop. Fast, free sources first; name-derived handles are listed but "
                      "not offered for testing."},
    "balanced": {"title": "Balanced", "depth": "2", "candidates": 4,
                 "help": "Direct identifiers plus one-hop pivots (an account's website, email, linked profiles). Up to 4 "
                         "name-derived handles offered for testing."},
    "deep": {"title": "Deep", "depth": "3", "candidates": 8,
             "help": "Two hops of correlation with a lower expansion threshold; up to 8 name-derived handles offered for "
                     "testing. Always within the investigation's limits (entities, requests, time)."},
}
DEPTH_MODE = {"1": "quick", "2": "balanced", "3": "deep", "4": "deep"}
COST_TIER = {"local": "LOCAL", "tool": "LOCAL", "free": "FREE", "free_key": "API KEY", "restricted": "API KEY",
             "metered": "PAID", "paid": "PAID", "enterprise": "PAID", "manual": "MANUAL"}
VALUE = {"official": "high", "high": "high", "medium": "medium", "low": "low"}
COST_ORDER = {"LOCAL": 0, "FREE": 1, "API KEY": 2, "PAID": 3, "MANUAL": 4}
INPUT_TITLES = {"person": "the person's name", "organization": "the organization", "email": "the email address",
                "phone": "the phone number", "username": "the username", "domain": "the domain", "website": "the website",
                "account": "the profile", "ip": "the IP address", "asn": "the AS number", "location": "the location",
                "name": "the name"}
CATEGORY_REASON = {
    "search": "Public web pages that mention {what}",
    "social": "Public social profiles linked to {what}",
    "usernames": "Whether accounts with {what} exist on many platforms (existence only, not identity)",
    "forums": "Public forum and community profiles for {what}",
    "developer_platforms": "Developer profiles: often list a website, an employer or a public email",
    "gaming": "Public gaming profiles for {what}",
    "music": "Public music profiles for {what}",
    "video": "Public video / streaming channels for {what}",
    "blogs": "Public blogs and articles by {what}",
    "marketplaces": "Public marketplace profiles for {what}",
    "professional_networks": "Public professional profiles for {what}",
    "email": "Email analysis: provider, domain and public accounts registered with {what}",
    "phone": "Numbering-plan analysis (country, line type) and public references to {what}",
    "breach_metadata": "Whether {what} appears in known breaches (metadata only, never leaked data)",
    "domains": "Domain intelligence for {what}", "dns": "DNS records of {what}", "rdap": "Registration data (RDAP) of {what}",
    "certificates": "Certificate-transparency logs: subdomains and history of {what}",
    "infrastructure": "IP / network ownership for {what}", "reputation": "Reputation and threat-intel listings for {what}",
    "technology_detection": "Technologies used by {what}", "websites": "The public website of {what}",
    "archives": "Archived versions of {what}", "documents": "Public documents about {what}",
    "companies": "Company registers: officers, filings, registered addresses for {what}",
    "organizations": "Organization identifiers (LEI, Wikidata) for {what}",
    "public_records": "Official public records for {what}", "legal_records": "Court and case-law records for {what}",
    "sanctions": "Sanctions, watchlists and PEP lists for {what}", "procurement": "Public contracts involving {what}",
    "publications": "Research publications by {what}", "news": "News coverage of {what}",
    "images": "Image analysis for {what}", "geolocation": "Place lookup for {what}",
    "people_search": "Commercial people-search results for {what} (your own licence only)",
    "adapters": "OSINT tools run locally for {what}",
}
NAME_ONLY = ("companies", "legal_records", "public_records", "sanctions", "procurement", "news", "search", "publications")


def source_entry(name: str, cfg, countries: list[str], inputs: set[str]) -> dict:
    conn = REG.REGISTRY[name]
    m = conn.manifest
    status, note = REG.connector_status(conn, cfg)
    tier = COST_TIER.get(m.access, "FREE")
    if m.requires_subscription and status == "available" and cfg is not None and not cfg.get_bool("allow_paid_sources"):
        status, note = "disabled", "Paid sources are switched off in Settings"
    if status == "disabled" and cfg is not None and cfg.connector_prefs(name).get("enabled") is None:
        hit = [c for c in m.supported_countries if c != "*" and c in countries]
        if hit:
            status, note = "available", f"Pre-selected: the investigation points to {', '.join(hit)}"
    what = " or ".join(sorted({INPUT_TITLES.get(t, t) for t in inputs})) or "the input"
    reason = CATEGORY_REASON.get(m.category, f"{m.category_title} for {{what}}").format(what=what)
    value = VALUE.get(m.reliability, "medium")
    if inputs <= {"person", "name"} and m.category in NAME_ONLY:
        value = "low" if value != "high" else "medium"
        reason += ". A name alone returns namesakes: every hit is a possible name match until corroborated"
    return {"name": name, "title": m.title, "category": m.category, "category_title": m.category_title,
            "status": status, "status_title": REG.STATUS_TITLES.get(status, status), "note": note, "cost_tier": tier,
            "cost": m.cost, "value": value, "reason": reason, "auto": REG.auto_allowed(conn, cfg),
            "sends": m.disclosure.sends, "destinations": list(m.disclosure.destinations),
            "country_specific": tuple(m.supported_countries) != ("*",)}


def plan(parsed, countries: list[dict], mode: str, cfg, snap: CAT.Snapshot | None = None,
         health_records: dict | None = None) -> dict:
    """``parsed``: ParsedInput; ``countries``: infer_countries() output ({code, name, why})."""
    mode = mode if mode in MODES else "balanced"
    spec = MODES[mode]
    REG.load_all()
    types = {s.type for s in parsed.seeds}
    if parsed.target_name:
        types.add("organization" if parsed.target_type == "organization" else "person")
    codes = [c["code"] for c in countries]

    # ------------------------------------------------------------------ 1. what was typed, normalised
    identifiers = [{"type": s.type, "value": s.value, "label": s.label, "note": s.note, "origin": "Provided by you"}
                   for s in parsed.seeds]
    names = [n for n in [parsed.target_name, *parsed.aliases] if n] if parsed.target_type == "person" else []
    cands = name_candidates(names, 10) if names else []
    name_analysis = None
    if names:
        from .names import readings
        from ..util.normalize import fold
        name_analysis = {"names": names, "diacritic_free": [fold(n) for n in names],
                         "readings": [r.title for r in readings(names[0])],
                         "note": "The order of given and family names is ambiguous; candidates are generated for each "
                                 "reading and say which one they assume."}

    # ------------------------------------------------------------------ 2. sources per input
    by_input: dict[str, list[dict]] = {}
    not_used: list[dict] = []
    for name, conn in REG.REGISTRY.items():
        ins = set(conn.input_types()) & types
        if not ins:
            continue
        entry = source_entry(name, cfg, codes, ins)
        if entry["status"] != "available":
            not_used.append(entry)
            continue
        if entry["country_specific"] and not set(conn.manifest.supported_countries) & set(codes):
            continue
        for t in sorted(ins):
            by_input.setdefault(t, []).append(entry)
    for lst in by_input.values():
        lst.sort(key=lambda e: (COST_ORDER.get(e["cost_tier"], 9), {"high": 0, "medium": 1, "low": 2}[e["value"]],
                                e["title"]))

    # ------------------------------------------------------------------ 3. jurisdictions
    ctx = CM.CoverageContext(cfg, health_records or {}, snap) if codes else None
    jur = []
    for c in countries:
        code = c["code"]
        if code not in J.jurisdictions():
            continue
        audit = AUDIT.audit(code, ctx, snap or CAT.Snapshot(0, [], [], [], set()))
        conns = [source_entry(n, cfg, codes, types) for n in ctx.country_connectors(code)]
        official = [{"title": s["title"], "url": s["url"], "origin": s["origin"], "automation": s["automation"],
                     "evidence": s["evidence"], "nature": s["nature"]} for s in audit["sources"]]
        jur.append({
            "code": code, "name": c["name"], "why": c.get("why") or [],
            "status": "selected by you" if "selected by you" in (c.get("why") or []) else "inferred — not confirmed",
            "connectors": conns,
            "automated": [o for o in official if o["automation"] == "automated"],
            "manual": [o for o in official if o["automation"] != "automated"][:25],
            "yours": [o for o in official if o["origin"] == "user"],
            "coverage": [{"group": g["title"], "rating": g["rating_title"], "kind": g["rating_kind"]}
                         for g in audit["coverage"] if g["core"]],
            "gaps": [g["message"] for g in audit["gaps"]],
        })

    # ------------------------------------------------------------------ 4. platforms
    platforms = None
    if "username" in types or cands:
        cat = P.catalog(ctx.states if ctx else {n: REG.connector_status(c, cfg)[0] for n, c in REG.REGISTRY.items()},
                        ctx.adapters if ctx else {}, include_imported=False,
                        extra=snap.platform_objects if snap else ())
        manual = [{"name": p["name"], "category": p["category_title"], "profile_url": p["profile_url"]}
                  for p in cat["curated"] if p["search_status"] == "manual" and p["profile_url"]]
        platforms = {"searchable": cat["counts"]["searchable"], "api_only": cat["counts"]["api_only"],
                     "manual": manual[:40], "manual_count": len(manual),
                     "note": "Usernames are checked where this installation can check automatically. Platforms that "
                             "forbid automation are offered as links to open yourself."}

    # ------------------------------------------------------------------ 5. steps
    steps = [{"phase": "normalize", "title": "Normalize identifiers", "cost_tier": "LOCAL",
              "reason": "Turns what you typed into typed identifiers (email, phone in international format, handle, "
                        "domain…). Nothing is sent anywhere.", "items": len(identifiers)}]
    if names:
        steps.append({"phase": "candidates", "title": "Generate candidate usernames from the name", "cost_tier": "LOCAL",
                      "reason": f"{len(cands)} common handle patterns, labelled “{CANDIDATE_STATUS}”. "
                                + (f"Up to {spec['candidates']} can be tested in the Assistant tab once you approve; "
                                   "a found account is only a lead." if spec["candidates"] else
                                   "Quick mode lists them without testing.")})
    for t, lst in sorted(by_input.items()):
        steps.append({"phase": "search", "title": f"Search sources for {INPUT_TITLES.get(t, t)}", "input": t,
                      "reason": "Sources this installation can query for this identifier, free and local first.",
                      "sources": lst})
    for j in jur:
        steps.append({"phase": "jurisdiction", "title": f"{j['name']}: country-specific sources ({j['status']})",
                      "reason": f"{len(j['connectors'])} country connector(s) run automatically; "
                                f"{len(j['manual'])} official source(s) are links to open yourself.",
                      "jurisdiction": j["code"]})
    if platforms:
        steps.append({"phase": "platforms", "title": "Platform checks", "reason": platforms["note"],
                      "searchable": platforms["searchable"], "manual_count": platforms["manual_count"]})
    steps += [{"phase": "correlate", "title": "Correlate evidence and score identities", "cost_tier": "LOCAL",
               "reason": "Each candidate gets signed reasons (+ supporting, − contradicting). A name match alone never "
                         "makes a likely match."},
              {"phase": "review", "title": "Your review", "cost_tier": "LOCAL",
               "reason": "Nothing is confirmed automatically: you confirm, separate or ignore each candidate."}]

    allow_paid = cfg.get_bool("allow_paid_sources") if cfg is not None else True
    used = [e for lst in by_input.values() for e in lst]
    return {
        "mode": {"id": mode, **spec}, "modes": MODES, "depth": spec["depth"],
        "identifiers": identifiers, "name_analysis": name_analysis,
        "candidates": [c.to_dict() for c in cands], "candidates_testable": spec["candidates"],
        "jurisdictions": jur,
        "jurisdiction_note": None if codes else "No jurisdiction inferred from the input: only global sources apply. "
                                                "Choose a country to add its sources.",
        "steps": steps, "platforms": platforms,
        "not_used": sorted(not_used, key=lambda e: (e["status"], e["title"]))[:80],
        "cost": {"free": sum(1 for e in used if e["cost_tier"] in ("FREE", "LOCAL")),
                 "api_key": sum(1 for e in used if e["cost_tier"] == "API KEY"),
                 "paid": sum(1 for e in used if e["cost_tier"] == "PAID"),
                 "paid_blocked": sum(1 for e in not_used if e["cost_tier"] == "PAID"),
                 "allow_paid": allow_paid,
                 "cap_usd": cfg.get_float("max_paid_cost_usd", 0.0, 1e6) if cfg is not None else 0.0},
    }
