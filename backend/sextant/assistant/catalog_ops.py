"""Catalog changes the assistant can propose, and how an approved proposal is applied.

Every change goes through the canonical catalog (``registry/catalog.py``: ``CatalogStore``), with the same validation
and duplicate checks as the Add-source form, so a source the assistant adds appears in Data Sources, Jurisdictions,
the Coverage matrix, country pages, the audit and search exactly like one you typed. Nothing here writes on its own:

* the copilot turns a tool call into a *proposal* (op + payload + a readable summary + duplicates found);
* a proposal is applied when you click "Apply" — or at once when it is non-destructive and you switched on
  "Allow the AI to apply non-destructive catalog changes automatically", and no likely duplicate was found;
* destructive operations (deleting a source, category, platform or note, removing a jurisdiction or platform link)
  always wait for your explicit confirmation, whatever the setting says.
"""

from __future__ import annotations

from ..discovery import profiles as PROF
from ..registry import catalog as CAT
from ..registry import jurisdictions as J
from ..registry import platforms as P

NON_DESTRUCTIVE = frozenset({"add_source", "update_source", "add_category", "link_source", "add_platform", "add_note"})
DESTRUCTIVE = frozenset({"delete_source", "delete_category", "delete_platform", "unlink_source", "delete_note"})
OPS = NON_DESTRUCTIVE | DESTRUCTIVE | {"run_action"}
SOURCE_INPUT_FIELDS = ("title", "url", "category", "source_type", "access", "kind", "jurisdictions", "platforms",
                       "connector", "inputs", "official", "name_search", "description", "person_data", "notes", "review",
                       "provider_class", "pricing", "auth", "terms_url", "data_types", "access_models", "api_available",
                       "web_app", "documentation_url", "coverage", "limitations", "last_verified")
PLATFORM_FIELDS = ("name", "url", "category", "profile_url", "countries", "region", "notes", "status", "public_profile")


def _jname(code: str) -> str:
    return CAT.jurisdiction_codes().get(code, code)


def editable(cur: dict) -> dict:
    return {k: cur.get(k) for k in SOURCE_INPUT_FIELDS if k in cur}


def source_summary(d: dict, snap: CAT.Snapshot) -> list[list[str]]:
    cats = CAT.category_vocab(snap)
    rows = [["Name", d.get("title", "")], ["URL", d.get("url", "")],
            ["Category", cats.get(d.get("category"), {}).get("title", d.get("category") or "")],
            ["Jurisdiction", ", ".join(_jname(c) for c in d.get("jurisdictions") or []) or "Global / none"],
            ["Type", CAT.SOURCE_TYPES.get(d.get("source_type") or "reference", {}).get("title", d.get("source_type") or "")],
            ["Access", CAT.ACCESS.get(d.get("access") or "unknown", d.get("access") or "")],
            ["Provider", CAT.PROVIDER_CLASSES.get(d.get("provider_class") or "public", "")],
            ["Official", "yes" if d.get("official") else "no"]]
    if d.get("kind"):
        rows.append(["Official source type", J.SOURCE_KINDS.get(d["kind"], d["kind"])])
    if d.get("connector"):
        rows.append(["Automated by connector", d["connector"]])
    if d.get("platforms"):
        rows.append(["Platforms", ", ".join(CAT.platform_title(p, snap) for p in d["platforms"])])
    if d.get("access_models"):
        rows.append(["Access models", ", ".join(d["access_models"])])
    rows.append(["Review", "Not verified"])
    return [r for r in rows if r[1] not in (None, "")]


def prepare(op: str, args: dict, store: CAT.CatalogStore) -> dict:
    """Validates a requested change and returns the proposal body: {op, title, summary, payload, destructive,
    duplicates}. Raises ValueError/KeyError with a message the model can act on."""
    snap = store.snapshot()
    if op == "add_source":
        d = {k: args.get(k) for k in SOURCE_INPUT_FIELDS if args.get(k) not in (None, "", [])}
        d.setdefault("review", "not_verified")
        CAT.validate_source(d, snap)
        dups = CAT.find_duplicates(d, snap)
        return {"op": op, "title": f"Add source “{d['title']}”", "summary": source_summary(d, snap),
                "payload": {**d, "allow_duplicate": bool(args.get("allow_duplicate"))}, "destructive": False,
                "duplicates": dups}
    if op == "update_source":
        sid = str(args.get("source_id") or "")
        cur = snap.by_id.get(sid)
        if cur is None:
            raise KeyError("Only your own sources can be edited (built-in sources are part of this version). Add a "
                           "source of your own instead, linking the built-in connector if it automates it.")
        changes = {k: v for k, v in (args.get("changes") or {}).items() if k in SOURCE_INPUT_FIELDS}
        if not changes:
            raise ValueError("No editable field given")
        merged = {**editable(cur), **changes}
        CAT.validate_source(merged, snap)
        diff = [[k, f"{cur.get(k)!s} → {v!s}"] for k, v in changes.items() if cur.get(k) != v]
        return {"op": op, "title": f"Edit source “{cur['title']}”", "summary": diff or [["(no change)", ""]],
                "payload": {"source_id": sid, "data": merged}, "destructive": False, "duplicates": []}
    if op in ("link_source", "unlink_source"):
        sid = str(args.get("source_id") or "")
        cur = snap.by_id.get(sid)
        if cur is None:
            raise KeyError("Jurisdictions and platforms can be linked only to your own sources. For a built-in source, "
                           "add a source of your own that references it (its connector), in the right jurisdiction.")
        jur = [str(c).upper() for c in cur["jurisdictions"]]
        plats = list(cur["platforms"])
        add_j = [str(c).upper() for c in args.get("add_jurisdictions") or []]
        rem_j = [str(c).upper() for c in args.get("remove_jurisdictions") or []]
        add_p = [str(p) for p in args.get("add_platforms") or []]
        rem_p = [str(p) for p in args.get("remove_platforms") or []]
        new_j = [c for c in jur if c not in rem_j] + [c for c in add_j if c not in jur]
        new_p = [p for p in plats if p not in rem_p] + [p for p in add_p if p not in plats]
        merged = {**editable(cur), "jurisdictions": new_j, "platforms": new_p}
        CAT.validate_source(merged, snap)
        destructive = bool(rem_j or rem_p)
        summary = [["Source", cur["title"]]]
        if add_j:
            summary.append(["Add jurisdiction", ", ".join(_jname(c) for c in add_j)])
        if rem_j:
            summary.append(["Remove jurisdiction", ", ".join(_jname(c) for c in rem_j)])
        if add_p:
            summary.append(["Add platform", ", ".join(CAT.platform_title(p, snap) for p in add_p)])
        if rem_p:
            summary.append(["Remove platform", ", ".join(CAT.platform_title(p, snap) for p in rem_p)])
        return {"op": "unlink_source" if destructive else "link_source",
                "title": f"Change the links of “{cur['title']}”", "summary": summary,
                "payload": {"source_id": sid, "data": merged}, "destructive": destructive, "duplicates": []}
    if op == "add_category":
        title = str(args.get("title") or "").strip()
        group = str(args.get("group") or "other")
        clash = CAT.category_by_title(title, snap)
        if clash:
            raise ValueError(f"A category named “{clash['title']}” already exists (id {clash['id']}): use it")
        if group not in J.SOURCE_GROUPS:
            raise ValueError(f"Group must be one of {', '.join(J.SOURCE_GROUPS)}")
        return {"op": op, "title": f"Create category “{title}”",
                "summary": [["Name", title], ["Group", J.SOURCE_GROUPS[group]],
                            ["Description", str(args.get("description") or "")]],
                "payload": {"title": title, "group": group, "description": str(args.get("description") or "")[:500]},
                "destructive": False, "duplicates": []}
    if op == "add_platform":
        d = {k: args.get(k) for k in PLATFORM_FIELDS if args.get(k) not in (None, "", [])}
        CAT.validate_platform(d)
        dups = CAT.platform_duplicates(CAT.host_of(d.get("url") or ""), snap)
        return {"op": op, "title": f"Add platform “{d.get('name')}”",
                "summary": [["Name", d.get("name", "")], ["URL", d.get("url", "")],
                            ["Category", P.PLATFORM_CATEGORIES.get(d.get("category") or "other", "")],
                            ["Profile URL template", d.get("profile_url") or "(none: listed only)"],
                            ["Countries", ", ".join(d.get("countries") or []) or "Global"],
                            ["Effect", "Profile links to it are recognised in input, on pages and in search results; "
                                       "with a template it also becomes a web-discovery target."]],
                "payload": {**d, "allow_duplicate": bool(args.get("allow_duplicate"))}, "destructive": False,
                "duplicates": dups}
    if op in ("delete_source", "delete_category", "delete_platform"):
        oid = str(args.get("id") or "")
        if op == "delete_source":
            cur = snap.by_id.get(oid)
            label = cur["title"] if cur else None
        elif op == "delete_category":
            cur = snap.category_by_id.get(oid)
            label = cur["title"] if cur else None
        else:
            cur = snap.platform_by_id.get(oid)
            label = cur["name"] if cur else None
        if label is None:
            raise KeyError("Only items you added can be deleted (built-in sources can be switched off in Data Sources)")
        return {"op": op, "title": f"Delete “{label}”", "summary": [["Delete", label], ["Undo", "Not possible"]],
                "payload": {"id": oid}, "destructive": True, "duplicates": []}
    raise ValueError(f"Unknown catalog operation {op!r}")


def apply(op: str, payload: dict, store: CAT.CatalogStore) -> dict:
    payload = dict(payload)
    if op == "add_source":
        allow = bool(payload.pop("allow_duplicate", False))
        s = store.create_source(payload, allow_duplicate=allow)
        return {"source_id": s["id"], "title": s["title"], "jurisdictions": s["jurisdictions"]}
    if op in ("update_source", "link_source", "unlink_source"):
        s = store.update_source(payload["source_id"], payload["data"], allow_duplicate=True)
        return {"source_id": s["id"], "title": s["title"], "jurisdictions": s["jurisdictions"], "platforms": s["platforms"]}
    if op == "add_category":
        c = store.put_category(payload)
        return {"category_id": c["id"], "title": c["title"]}
    if op == "add_platform":
        allow = bool(payload.pop("allow_duplicate", False))
        p = store.put_platform(payload, allow_duplicate=allow)
        return {"platform_id": p["id"], "name": p["name"]}
    if op == "delete_source":
        return {"deleted": store.delete_source(payload["id"])}
    if op == "delete_category":
        used = store.delete_category(payload["id"])
        if used:
            raise ValueError(f"{len(used)} of your sources still use this category; move them first")
        return {"deleted": True}
    if op == "delete_platform":
        return {"deleted": store.delete_platform(payload["id"])}
    raise ValueError(f"Unknown catalog operation {op!r}")


def search(snap: CAT.Snapshot, cfg, *, query: str = "", jurisdiction: str = "", category: str = "", limit: int = 30) -> list[dict]:
    """The catalog as one list: built-in connectors, official sources per jurisdiction, and your own sources."""
    from ..registry import connectors as REG
    q = CAT.title_key(query)
    code = jurisdiction.upper()
    out: list[dict] = []

    def hit(*texts) -> bool:
        return not q or all(w in CAT.title_key(" ".join(str(t or "") for t in texts)) for w in q.split())
    for n, c in REG.REGISTRY.items():
        m = c.manifest
        if category and m.category != category:
            continue
        if code and code not in m.supported_countries and "*" not in m.supported_countries:
            continue
        if hit(m.title, m.description, m.category, " ".join(m.capabilities)):
            out.append({"origin": "system", "kind": "connector", "id": n, "title": m.title, "category": m.category,
                        "url": m.website, "status": REG.connector_status(c, cfg)[0],
                        "countries": list(m.supported_countries)[:12]})
    for s in J.all_sources():
        if code and s.country != code:
            continue
        if category and CAT.category_for_kind(s.kind) != category:
            continue
        if hit(s.title, s.kind, s.notes):
            out.append({"origin": "system", "kind": "official_source", "id": s.id, "title": s.title, "category":
                        CAT.category_for_kind(s.kind), "official_kind": s.kind, "url": s.url, "countries": [s.country],
                        "connector": s.connector or None})
    for s in snap.sources:
        if code and code not in s["jurisdictions"]:
            continue
        if category and s["category"] != category:
            continue
        if hit(s["title"], s.get("description"), s.get("notes"), s["url"]):
            out.append({"origin": "user", "kind": "your_source", "id": s["id"], "title": s["title"], "category": s["category"],
                        "url": s["url"], "countries": s["jurisdictions"], "enabled": s["enabled"], "review": s["review"]})
    return out[:limit]


def platforms(snap: CAT.Snapshot, cfg, query: str, limit: int = 20) -> list[dict]:
    from ..registry import connectors as REG
    q = CAT.title_key(query)
    states = {n: REG.connector_status(c, cfg)[0] for n, c in REG.REGISTRY.items()}
    out = []
    for p in (*P.curated(), *snap.platform_objects):
        if q and q not in CAT.title_key(f"{p.name} {p.id} {' '.join(p.hosts)}"):
            continue
        st = P.search_status(p, states, {"include_adult": False})
        out.append({"id": p.id, "name": p.name, "origin": "user" if p.source == "user" else "system", "category": p.category,
                    "hosts": list(p.hosts), "profile_url": p.profile_url, "check": p.check, "search_status": st,
                    "search_status_title": P.SEARCH_STATUS_TITLES.get(st, st), "public_profile": p.public_profile,
                    "web_discovery_target": p.id in {t.platform for t in PROF.targets()}
                    or (p.source == "user" and bool(p.profile_url)),
                    "notes": p.notes[:200]})
        if len(out) >= limit:
            break
    return out


__all__ = ["prepare", "apply", "search", "platforms", "NON_DESTRUCTIVE", "DESTRUCTIVE", "OPS"]
