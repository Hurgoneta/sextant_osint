"""Global coverage model — what this installation can investigate, per country and per platform.

Every number shown on the Global Coverage screen, the coverage panel, the start screen and the final audit
comes from here, and is computed from the registries and the configuration. Nothing is typed in by hand.

Coverage level of a country (only country-specific sources count; global sources apply to all):

    connectors   at least one connector targets the country (its data, its registers)
    links        no country connector, but curated official sources (links the investigator opens)
    discovered   only potential sources found in maintained datasets (not yet reviewed)
    none         nothing country-specific known — "No connector currently available"

Global connectors that can filter by country (GDELT news by outlet country, RDAP for the ccTLD, official
OpenSanctions datasets, the offline phone-numbering metadata, GLEIF's LEI index) are listed separately per
country, with what they actually cover there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import phonenumbers

from . import catalog as CAT
from . import countries as C
from . import discovered as D
from . import jurisdictions as J
from . import platforms as P
from .connectors import REGISTRY, connector_status, load_all
from .health import HEALTH_TITLES, connector_health, link_health, snapshot

LEVEL_TITLES = {
    "connectors": "Country connectors available",
    "links": "Official sources (links only)",
    "discovered": "Potential sources only (from datasets)",
    "none": "No connector currently available",
}
NO_CONNECTOR_REASON = "No verified public/authorized API discovered"
INACTIVE = ("catalog_only", "retired", "excluded")
COMMERCIAL_ACCESS = ("paid", "metered", "enterprise")
SEARCH_DISCLAIMER = ("Sextant searched all currently configured and applicable sources available to this installation. "
                     "It did not search the entire Internet, and a missing result never proves that information does "
                     "not exist.")


@dataclass
class CoverageContext:
    cfg: object = None
    health_records: dict = field(default_factory=dict)
    catalog: CAT.Snapshot | None = None          # the investigator's own sources and platforms (user layer)

    def __post_init__(self) -> None:
        load_all()
        self.states = {name: connector_status(c, self.cfg)[0] for name, c in REGISTRY.items()}
        self.adapters: dict = {t: self.states.get(t) for t in ("maigret", "sherlock")}   # available | disabled | other
        include_adult = bool(self.cfg.get_bool("include_adult_sites")) if self.cfg is not None else False
        top = self.cfg.get_int("maigret_top_sites", 50, 8000) if self.cfg is not None else 500
        self.adapters.update(include_adult=include_adult, maigret_top_n=top)
        current = (str(self.cfg.get("maigret_database") or "current") if self.cfg is not None else "current") == "current"
        for tool, module in (("maigret", "maigret"), ("sherlock", "sherlock_project")):
            if self.adapters.get(tool) in ("available", "disabled"):
                # Maigret on its current database: count from the imported snapshot of that same database
                # (data/platforms/imported/maigret.json); otherwise from the database bundled with the installed tool.
                db = None if tool == "maigret" and current else _installed_db(tool, module, self.cfg)
                self.adapters[f"{tool}_ids"] = frozenset(i for i, _ in P.tool_sites(tool, db, include_adult))
                self.adapters[f"{tool}_db"] = db
        self.adapters["maigret_top"] = P.maigret_top_ids(top, include_adult, self.adapters.get("maigret_db"))
        self.verification = J.verification()
        self.snap = snapshot()
        self.custom_sources: list[dict] = list(self.catalog.sources) if self.catalog else []
        self.user_platforms: tuple = self.catalog.platform_objects if self.catalog else ()
        self.fingerprint = self.catalog.fingerprint if self.catalog else ""
        self._country_conns: dict[str, list[str]] = {}
        for name, conn in REGISTRY.items():
            for cc in conn.manifest.supported_countries:
                if cc != "*" and self.states[name] not in INACTIVE:
                    self._country_conns.setdefault(cc, []).append(name)
        self._platform_states = None

    def country_connectors(self, code: str) -> list[str]:
        return sorted(self._country_conns.get(code, []))

    def custom_for(self, code: str) -> list[dict]:
        return self.catalog.for_jurisdiction(code) if self.catalog else []

    def platforms_for(self, code: str) -> list[P.Platform]:
        return P.for_country(code, self.user_platforms)

    def health(self, name: str) -> dict:
        return connector_health(name, self.cfg, self.health_records.get(name), self.snap)

    def platform_status(self, p: P.Platform) -> str:
        return P.search_status(p, self.states, self.adapters)


_DB_CACHE: dict = {}


def _installed_db(tool: str, module: str, cfg) -> str | None:
    """Path of the installed tool's own site database (cached per executable)."""
    from ..connectors.adapters.tools import find_tool, tool_package_file
    exe = find_tool(f"{tool.upper()}_PATH", (tool,), cfg)
    if not exe:
        return None
    if exe not in _DB_CACHE:
        f = tool_package_file(exe, module, "resources/data.json")
        _DB_CACHE[exe] = str(f) if f else None
    return _DB_CACHE[exe]


def _verif(ctx: CoverageContext, sid: str) -> dict | None:
    return (ctx.verification.get("sources") or {}).get(sid)


def _connector_brief(ctx: CoverageContext, name: str) -> dict:
    m = REGISTRY[name].manifest
    h = ctx.health(name)
    return {"connector": name, "title": m.title, "category": m.category, "category_title": m.category_title,
            "status": ctx.states[name], "access": m.access, "access_title": m.access_info["title"],
            "health": h["status"], "health_title": h["title"], "last_verified": h["last_verified"],
            "last_success": h["last_success"]}


def global_capabilities(code: str, ctx: CoverageContext) -> list[dict]:
    """Global connectors with data specific to this country (and what exactly)."""
    c = C.get(code)
    out = []
    disc = D.for_country(code)
    gd = next((s for s in disc if s.dataset == "gdelt"), None)
    if gd:
        out.append({"connector": "gdelt", "what": f"News from media outlets in {c.name if c else code} "
                    f"(GDELT sourcecountry:{gd.dataset_ref})", "status": ctx.states.get("gdelt")})
    tld = next((s for s in disc if s.dataset == "iana"), None)
    if tld:
        out.append({"connector": "rdap" if tld.metrics.get("rdap") else "dns",
                    "what": (f"Domain registration data for {tld.dataset_ref} via RDAP" if tld.metrics.get("rdap") else
                             f"DNS for {tld.dataset_ref} domains; the registry has no RDAP service (WHOIS/web only)"),
                    "status": ctx.states.get("rdap" if tld.metrics.get("rdap") else "dns")})
    rir = next((s for s in disc if s.dataset == "rir"), None)
    if rir:
        out.append({"connector": "ripestat", "what": f"IP/ASN data ({rir.dataset_ref}; {rir.metrics.get('asn', 0)} ASNs allocated)",
                    "status": ctx.states.get("ripestat")})
    os_sets = [s for s in disc if s.dataset == "opensanctions"]
    if os_sets:
        auto = [s for s in os_sets if s.connector]
        out.append({"connector": "opensanctions", "what": f"{len(os_sets)} official lists published by authorities of this "
                    f"country (sanctions, PEPs, debarment, wanted) — {len(auto)} included in OpenSanctions collections",
                    "status": ctx.states.get("opensanctions")})
    if code in phonenumbers.SUPPORTED_REGIONS:
        out.append({"connector": "phone_offline", "what": f"Phone-number analysis (+{phonenumbers.country_code_for_region(code)}, "
                    "offline numbering-plan metadata)", "status": ctx.states.get("phone_offline")})
    leis = D.lei_counts().get(code)
    if leis:
        out.append({"connector": "gleif", "what": f"{leis:,} legal entities with an LEI registered in this country",
                    "status": ctx.states.get("gleif")})
    return out


def _level(n_conn: int, n_curated: int, n_disc: int) -> str:
    if n_conn:
        return "connectors"
    if n_curated:
        return "links"
    if n_disc:
        return "discovered"
    return "none"


def country_row(code: str, ctx: CoverageContext) -> dict:
    c = C.get(code)
    j = J.jurisdictions().get(code)
    curated = list(j.sources) if j else []
    disc = [s for s in D.for_country(code) if not s.corroborates]
    custom = ctx.custom_for(code)
    custom_active = [s for s in custom if CAT.active(s)]     # disabled / excluded ones are listed, not counted
    conns = ctx.country_connectors(code)
    available = [n for n in conns if ctx.states[n] == "available"]
    by_group: dict[str, dict[str, int]] = {}
    empty = {"curated": 0, "discovered": 0, "user": 0}
    for s in curated:
        by_group.setdefault(s.group, dict(empty))["curated"] += 1
    for s in disc:
        by_group.setdefault(s.group, dict(empty))["discovered"] += 1
    custom_groups = [CAT.group_of(s, ctx.catalog) for s in custom_active] if ctx.catalog else []
    for g in custom_groups:
        by_group.setdefault(g, dict(empty))["user"] += 1
    regional = ctx.platforms_for(code)
    platform_searchable = sum(1 for p in regional if ctx.platform_status(p) == "searchable")
    records = sum(1 for s in curated if s.group in J.RECORD_GROUPS) + \
        sum(1 for s in disc if s.group in J.RECORD_GROUPS) + sum(1 for g in custom_groups if g in J.RECORD_GROUPS)
    tld = next((s for s in D.for_country(code) if s.dataset == "iana"), None)
    rir = next((s for s in D.for_country(code) if s.dataset == "rir"), None)
    checked = [v["checked_at"] for s in curated if (v := _verif(ctx, s.id)) and v.get("checked_at")]
    own = [s for s in curated if s.provenance == "curated"]
    regional_portals = [s for s in curated if s.provenance != "curated"]
    level = _level(len(conns), len(own) + len(custom_active), len(disc))
    reason = ""
    if level == "none":
        reason = NO_CONNECTOR_REASON
    elif level == "discovered":
        reason = "Only potential sources from maintained datasets" + \
            (f" and {len(regional_portals)} regional portal(s)" if regional_portals else "") + "; none reviewed or automated yet"
    elif level == "links":
        reason = "Official sources are known but none has a usable public API connector yet"
    return {
        "code": code, "name": c.name if c else (j.name if j else code), "region": c.region if c else "International",
        "subregion": c.subregion if c else "", "status": c.status if c else "", "sovereign": bool(c and c.sovereign),
        "level": level, "level_title": LEVEL_TITLES[level], "reason": reason,
        "connectors": len(conns), "connectors_available": len(available),
        "curated_sources": len(own), "regional_sources": len(regional_portals), "discovered_sources": len(disc),
        "custom_sources": len(custom), "custom_active": len(custom_active),
        "sources": len(curated) + len(disc) + len(custom),
        "regional_platforms": len(regional), "regional_platforms_searchable": platform_searchable,
        "records": records, "groups": by_group,
        "dns": {"tld": c.tld if c else "", "rdap": bool(tld and tld.metrics.get("rdap"))} if c else None,
        "rir": rir.dataset_ref if rir else "",
        "global_capabilities": len(global_capabilities(code, ctx)),
        "blocs": list(j.blocs) if j else [],
        "last_checked": max(checked) if checked else None,
    }


def country_detail(code: str, ctx: CoverageContext) -> dict:
    code = code.upper()
    row = country_row(code, ctx)
    j = J.jurisdictions().get(code)
    c = C.get(code)
    curated = []
    for s in (j.sources if j else []):
        v = _verif(ctx, s.id)
        d = s.to_dict()
        d["verification"] = v
        d["health"] = ctx.health(s.connector) if s.connector and s.connector in REGISTRY else link_health(v)
        curated.append(d)
    disc = []
    for s in D.for_country(code):
        d = s.to_dict()
        d["verification"] = (ctx.verification.get("discovered") or {}).get(D.verification_key(s))
        d["health"] = link_health(d["verification"])
        disc.append(d)
    bloc = [{"bloc": b.id, "bloc_title": b.title, **s.to_dict(), "verification": _verif(ctx, s.id)}
            for b, s in J.bloc_sources(code)]
    platforms = []
    for p in ctx.platforms_for(code):
        st = ctx.platform_status(p)
        platforms.append({"id": p.id, "name": p.name, "category": p.category,
                          "category_title": P.PLATFORM_CATEGORIES.get(p.category, p.category), "url": p.url,
                          "source": p.source, "check": p.check, "search_status": st,
                          "search_status_title": P.SEARCH_STATUS_TITLES.get(st, st)})
    platforms.sort(key=lambda p: ({"curated": 0, "user": 1}.get(p["source"], 2), p["search_status"] != "searchable",
                                  p["name"].lower()))
    potential = [d for d in disc if not d["corroborates"]]
    custom = [CAT.jurisdiction_entry(s, ctx.catalog, ctx.states, code) for s in ctx.custom_for(code)] \
        if ctx.catalog else []
    return {
        **row, "country": c.to_dict() if c else None,
        "legal_notes": j.legal_notes if j else "", "criminal_records_public": j.criminal_records_public if j else False,
        "judgments_anonymized": j.judgments_anonymized if j else None,
        "country_connectors": [_connector_brief(ctx, n) for n in ctx.country_connectors(code)],
        "global_capabilities_detail": global_capabilities(code, ctx),
        "curated": curated, "discovered": disc, "potential_sources": potential, "bloc_sources": bloc,
        "custom": custom, "platforms": platforms,
        "datasets": D.dataset_info(), "groups_vocabulary": J.SOURCE_GROUPS,
    }


def matrix(ctx: CoverageContext, include_territories: bool = True) -> dict:
    rows = [r for r in cached_rows(ctx) if r["sovereign"] or include_territories]
    rows.sort(key=lambda r: (not r["sovereign"], r["name"]))
    return {"rows": rows, "levels": LEVEL_TITLES, "groups": J.SOURCE_GROUPS, "regions": list(C.REGIONS),
            "summary": summary(ctx, rows), "provenance": {"countries": C.provenance(), "datasets": D.dataset_info()}}


_ROWS_CACHE: dict = {}


def cached_rows(ctx: CoverageContext, ttl: float = 120.0) -> list[dict]:
    """Country rows, cached briefly (they change only with configuration, the investigator's own sources, datasets or
    link checks). The key includes a content fingerprint of the user layer, so an added, edited or deleted source is
    counted at once, and rows computed without it are never served for a context that has it."""
    import time
    key = (tuple(sorted(ctx.states.items())), ctx.fingerprint)
    hit = _ROWS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    rows = [country_row(code, ctx) for code in C.countries()]
    _ROWS_CACHE.clear()
    _ROWS_CACHE[key] = (time.monotonic(), rows)
    return rows


def summary(ctx: CoverageContext, rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else cached_rows(ctx)
    sov = [r for r in rows if r["sovereign"]]
    levels = {k: sum(1 for r in sov if r["level"] == k) for k in LEVEL_TITLES}
    by_region: dict[str, dict] = {}
    for r in sov:
        g = by_region.setdefault(r["region"], {"region": r["region"], "countries": 0, **{k: 0 for k in LEVEL_TITLES}})
        g["countries"] += 1
        g[r["level"]] += 1
    cat = P.catalog(ctx.states, ctx.adapters, include_imported=False, extra=ctx.user_platforms)
    conn_counts: dict[str, int] = {}
    health_counts: dict[str, int] = {}
    commercial = {"configured": 0, "not_configured": 0, "catalog_only": 0}
    public_db = {"available": 0, "unavailable": 0}
    for name, conn in REGISTRY.items():
        st = ctx.states[name]
        conn_counts[st] = conn_counts.get(st, 0) + 1
        h = ctx.health(name)["status"]
        health_counts[h] = health_counts.get(h, 0) + 1
        m = conn.manifest
        if m.access in COMMERCIAL_ACCESS:
            if st == "available" or (st == "disabled" and m.requires_subscription):
                commercial["configured"] += 1
            elif st == "catalog_only":
                commercial["catalog_only"] += 1
            elif st not in ("excluded", "retired"):
                commercial["not_configured"] += 1
        if m.public_record:
            public_db["available" if st == "available" else "unavailable"] += 1
    jur_sources = J.all_sources()
    providers = [s for s in jur_sources if s.kind == "commercial_provider"]
    commercial["providers_listed"] = len({s.url for s in providers})
    commercial["countries_with_providers"] = len({s.country for s in providers})
    ver = ctx.verification.get("sources") or {}
    link_results: dict[str, int] = {}
    for s in jur_sources:
        r = (ver.get(s.id) or {}).get("result", "not_checked")
        link_results[r] = link_results.get(r, 0) + 1
    disc = D.all_discovered()
    return {
        "countries": {"total": len(sov), "territories": len(rows) - len(sov), "levels": levels,
                      "level_titles": LEVEL_TITLES, "with_country_connectors": levels["connectors"],
                      "links_only": levels["links"], "discovered_only": levels["discovered"], "none": levels["none"],
                      "by_region": sorted(by_region.values(), key=lambda g: C.REGIONS.index(g["region"]))},
        "platforms": {**{k: cat["counts"][k] for k in ("registered", "curated", "user", "imported", "searchable", "opt_in",
                                                       "api_only", "unavailable", "regional_total")},
                      "adapters": {k: ctx.adapters.get(k) for k in ("maigret", "sherlock")},
                      "adapter_sites": {k: len(ctx.adapters.get(f"{k}_ids") or ()) for k in ("maigret", "sherlock")},
                      "maigret_top_n": ctx.adapters.get("maigret_top_n"),
                      "maigret_database": "current" if not ctx.adapters.get("maigret_db") else "bundled",
                      "by_status": cat["counts"]["by_status"], "status_titles": P.SEARCH_STATUS_TITLES},
        "connectors": {"total": len(REGISTRY), "by_status": conn_counts, "by_health": health_counts,
                       "health_titles": HEALTH_TITLES},
        "public_databases": {"official_sources": len(jur_sources),
                             "automated": sum(1 for s in jur_sources if s.connector),
                             "links_only": sum(1 for s in jur_sources if not s.connector),
                             "connectors_available": public_db["available"],
                             "connectors_unavailable": public_db["unavailable"],
                             "discovered": len(disc), "link_checks": link_results,
                             "links_checked_at": ctx.verification.get("checked_at")},
        "commercial": commercial,
        "custom_sources": len(ctx.custom_sources),
        "custom_sources_active": sum(1 for s in ctx.custom_sources if CAT.active(s)),
        "disclaimer": SEARCH_DISCLAIMER,
    }
