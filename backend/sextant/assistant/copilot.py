"""The investigation copilot: a persistent, tool-using assistant attached to one investigation.

It is an orchestration layer over the real application, never a second engine:

* **Actions** are the next best actions of ``analysis.next_actions``, run on the investigator's click or by the AI:
  search a discovered pivot, discover profiles on the web, read a candidate's full profile, test name-derived handles,
  try another configured source, run a configured paid source (only with explicit confirmation), continue.
* **Chat** (optional, needs an AI provider): the model sees and changes the application only through ``TOOLS``:
  read-only tools (overview, identities, identifiers, evidence, searches performed, discovery stages, costs, audits,
  catalog and platform lookups, URL inspection, source discovery, health) run at once; investigation actions run at once
  unless they are large or above the preview threshold (then a preview waits for "Run"); catalog changes become
  proposals you apply with one click (or at once, if you allowed non-destructive changes); deletions always wait for you.
* **Memory**: every chat turn, tool call, action, proposal and its outcome is stored in ``assistant_log`` (encrypted,
  deleted with the investigation), notes in ``investigation_notes``. A conversation continues across messages and
  across providers: another provider's turns are replayed as plain text.
* **Providers**: the primary provider answers; when it is unreachable or refuses the key, the next ready provider of
  the failover order takes over (never a disabled one), and each answer says which provider and model handled it.
  Token usage and cost (actual when the provider reports it, else estimated from the pricing table) are recorded per
  request and counted in the investigation's costs and budget.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from ..costs import ledger as LEDGER
from ..costs import pricing as PR
from ..registry import connectors as REG
from ..registry.catalog import store_for
from . import analysis as A
from . import catalog_ops as OPS
from . import identifiers as IDS
from .providers import PROVIDERS, ProviderError, build, fallbacks, make, primary_id

MAX_STEPS = 10
RESULT_CHARS = 12_000
PREVIEW_CALLS = 20

SYSTEM = """You are the investigation copilot of Sextant, a local OSINT application used for lawful investigations \
(due diligence, journalism, security research, fraud and sanctions screening). You stay attached to one investigation; \
earlier turns of this conversation are context, but numbers change: read them again with the tools.

You act only through the tools. They read the evidence graph and run the application's own capabilities: configured \
sources, web profile discovery, platform checks, lookups, audits, the source catalog, notes and costs. Never describe an \
action as done unless a tool did it. Rules:

1. Never state a fact, account, URL, source, price or result that a tool did not return. Cite what supports each claim \
(entity ids, sources, queries, ranks, signals). Keep apart what was FOUND (a source reported it), INFERRED (the \
resolver concluded it), GENERATED (name-derived handles: hypotheses), UNKNOWN and NOT VERIFIED.
2. An identity match needs several independent signals. A shared name, a similar handle, a search rank, a similar photo \
or the same city or country alone is never enough: say "Not enough evidence to determine whether these are the same \
person" when that is the honest answer. Use the explanation (for / against) the tools give.
3. Before saying no username or profile exists, check discovery_report: if web discovery did not run or was unavailable, \
say so ("Web discovery unavailable — API-only search was used") and offer to run it, instead of concluding absence.
4. To find usernames, use discover_profiles (the name or a handle on each platform through web search) and \
search_identifier on discovered handles; the handle in a profile URL does not need to be guessable from the name. \
"Check Pinterest" means discover_profiles with that platform.
5. Prefer the action with the highest expected value and say why; mention the estimated cost when it is not free. \
Large or costly actions return a preview the investigator confirms: tell them it is waiting for them.
6. Catalog changes (add/edit sources, categories, platforms, jurisdiction or platform links) go through the catalog \
tools. For a URL, call inspect_source_url first, then add_source with the draft (fix category, jurisdiction and type to \
what the investigator asked). To find new sources use discover_sources and show the candidates; never add them all. \
A proposal waits for the investigator unless the result says it was applied; deletions always wait. Built-in sources \
cannot be edited: add a source of your own instead.
7. Paid sources run only when the investigator confirms them in the interface. Commercial providers listed in the \
catalog cannot be queried unless a connector is configured with the investigator's own licence.
8. Never look for or infer private contact details beyond what sources publish, never suggest bypassing CAPTCHAs, \
logins, rate limits or other access controls, and never suggest scraping logged-in pages. A source that asks for \
verification is manual.
9. Only the investigator confirms or separates an identity. Keep notes (add_note) of important identifiers and \
decisions so they persist.
Answer concisely, in the language of the investigator."""

_S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req),  # noqa: E731
                            "additionalProperties": False}
_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}, "maxItems": 60}
SOURCE_PROPS = {
    "title": _STR, "url": _STR, "category": {"type": "string", "description": "a category id from catalog_vocab"},
    "source_type": {"type": "string", "enum": ["reference", "manual", "official_api", "public_api", "dataset", "local"]},
    "access": {"type": "string", "enum": ["free_web", "free_api", "free_download", "registration", "paid", "on_request",
                                          "restricted", "unknown"]},
    "kind": {"type": "string", "description": "official source type id (court, judgments, company_registry…) or empty"},
    "jurisdictions": _STRS, "platforms": _STRS, "connector": _STR, "official": {"type": "boolean"},
    "description": _STR, "notes": _STR,
    "provider_class": {"type": "string", "enum": ["public", "commercial", "private", "manual"]},
    "pricing": _STR, "auth": _STR, "terms_url": _STR, "documentation_url": _STR, "coverage": _STR, "limitations": _STR,
    "data_types": _STRS, "access_models": {"type": "array", "items": {"type": "string", "enum": [
        "free", "free_tier", "trial", "per_query", "subscription", "enterprise", "manual"]}},
    "api_available": {"type": "string", "enum": ["yes", "no", "unknown"]}, "web_app": {"type": "boolean"},
    "allow_duplicate": {"type": "boolean", "description": "only after the investigator said it is not a duplicate"},
}

TOOLS = [
    # ---------------------------------------------------------------- reading the investigation
    {"name": "investigation_overview",
     "description": "Status, counts, headline, strongest candidate identities, discovered identifiers, discovery stages, "
                    "costs and remaining budget, countries, search and AI providers. Call this first.",
     "schema": _S({})},
    {"name": "candidate_identities",
     "description": "Candidate identities (groups of profiles): each member with its verdict, confidence word, evidence for "
                    "and against (strong/medium/weak), and where it came from.",
     "schema": _S({"limit": {"type": "integer", "minimum": 1, "maximum": 60}})},
    {"name": "entity_details",
     "description": "One entity: properties with sources, relationships with the evidence for each edge, the identity "
                    "assessment and its explanation. Local-only data is never included.",
     "schema": _S({"entity_id": _STR}, ["entity_id"])},
    {"name": "identifiers",
     "description": "The identifier layer: every name, handle, email, phone, domain, URL, organization, address and "
                    "platform profile with its state (Provided, Generated, Discovered, Observed, Possible, Verified, "
                    "Rejected), source, search rank/query/provider, confidence and whether it was pivoted on.",
     "schema": _S({"state": {"type": "string"}, "type": {"type": "string"}, "limit": {"type": "integer", "maximum": 300}})},
    {"name": "platform_table",
     "description": "Platform | candidate | status | evidence | confidence for every platform checked, plus platforms that "
                    "can only be checked by hand.", "schema": _S({"only_found": {"type": "boolean"}})},
    {"name": "username_candidates",
     "description": "Handles generated from the person's name (hypotheses, not evidence) and whether each was tested.",
     "schema": _S({})},
    {"name": "discovery_report",
     "description": "The identity-discovery pipeline stage by stage (name → web search → profile URLs → handles → pivots → "
                    "other platforms → correlation → ranking), the discovered handles with rank/query/provider, whether "
                    "web discovery was available, and the queries run. Use it to explain what was or was not found.",
     "schema": _S({"journal_limit": {"type": "integer", "maximum": 200}})},
    {"name": "searches_performed",
     "description": "What was actually searched: every source run (what was sent, where, outcome) and every web query.",
     "schema": _S({"limit": {"type": "integer", "minimum": 1, "maximum": 200}})},
    {"name": "next_actions",
     "description": "Recommended next steps with reason, expected value, estimated cost and whether you may run them.",
     "schema": _S({})},
    {"name": "cost_report",
     "description": "This investigation's cost per provider and category (estimated / actual / pending / unknown), the "
                    "chain of billable steps, budget and remaining budget, and the AI usage.", "schema": _S({})},
    {"name": "estimate_cost",
     "description": "Estimated cost and number of calls of a next action (by id) before running it.",
     "schema": _S({"action_id": _STR}, ["action_id"])},
    {"name": "list_notes", "description": "The investigation's notes.", "schema": _S({})},
    # ---------------------------------------------------------------- acting in the investigation
    {"name": "run_action",
     "description": "Runs one executable next action by id (from next_actions). Paid sources need the investigator's "
                    "confirmation; large or costly actions return a preview the investigator confirms.",
     "schema": _S({"action_id": _STR, "why": _STR}, ["action_id", "why"])},
    {"name": "search_identifier",
     "description": "Runs the configured sources on an identifier ALREADY in the graph (entity id): platform checks for a "
                    "handle, DNS/RDAP/certificate transparency/website analysis for a domain, email and phone sources, "
                    "registers for an organization. Optional categories restrict it (e.g. [\"dns\",\"rdap\",\"certificates\","
                    "\"websites\"]); include_web adds the web-search provider.",
     "schema": _S({"entity_id": _STR, "why": _STR, "categories": _STRS, "include_web": {"type": "boolean"}},
                  ["entity_id", "why"])},
    {"name": "discover_profiles",
     "description": "Web profile discovery: searches the configured web-search provider for the target's name (or a handle "
                    "in the graph) on each platform, reads profile URLs in the results and adds the handles they carry "
                    "as discovered identifiers. platforms restricts it (ids or names, e.g. [\"pinterest\",\"tiktok\"]).",
     "schema": _S({"entity_id": {"type": "string", "description": "a person or username entity; default the target"},
                   "platforms": _STRS, "why": _STR}, ["why"])},
    {"name": "add_identifier",
     "description": "Adds an identifier the INVESTIGATOR typed in their message (exact value) to the graph as provided "
                    "input, so it can be searched. Refused for values not in the investigator's message.",
     "schema": _S({"type": {"type": "string", "enum": ["username", "email", "phone", "domain", "account", "organization",
                                                        "website"]}, "value": _STR, "why": _STR}, ["type", "value", "why"])},
    {"name": "wait_for_results",
     "description": "Waits up to the given seconds for queued searches to finish, then reports what is new.",
     "schema": _S({"seconds": {"type": "integer", "minimum": 1, "maximum": 45}}, ["seconds"])},
    {"name": "add_note",
     "description": "Adds a structured note to the investigation (e.g. an important identifier with its source and status).",
     "schema": _S({"title": _STR, "body": _STR, "identifiers": _STRS,
                   "status": {"type": "string", "enum": ["candidate", "lead", "confirmed", "rejected", "todo", "info"]}},
                  ["title", "body"])},
    {"name": "update_note", "description": "Changes a note (by id).",
     "schema": _S({"note_id": _STR, "title": _STR, "body": _STR, "identifiers": _STRS, "status": _STR}, ["note_id"])},
    {"name": "delete_note", "description": "Proposes deleting a note (the investigator confirms).",
     "schema": _S({"note_id": _STR}, ["note_id"])},
    # ---------------------------------------------------------------- the catalog
    {"name": "jurisdiction_audit",
     "description": "Audits a jurisdiction (ISO code, e.g. SE): coverage per area, automated sources, manual official "
                    "sources, your sources, gaps and dataset-backed suggestions. Read-only; never invents sources.",
     "schema": _S({"country": _STR}, ["country"])},
    {"name": "catalog_search",
     "description": "Searches the source catalog (built-in connectors, official sources per jurisdiction, your own "
                    "sources) by words, jurisdiction and/or category.",
     "schema": _S({"query": _STR, "jurisdiction": _STR, "category": _STR, "limit": {"type": "integer", "maximum": 60}})},
    {"name": "catalog_vocab",
     "description": "Valid category ids (built-in and yours), groups, source types, access models, official source "
                    "types and jurisdiction codes, for add_source.", "schema": _S({})},
    {"name": "platform_lookup",
     "description": "Platforms in the registry matching words: whether Sextant can check them, how, and whether web "
                    "discovery targets them.", "schema": _S({"query": _STR}, ["query"])},
    {"name": "inspect_source_url",
     "description": "Reads one web address (a single GET) and drafts a source definition: likely name, category, "
                    "jurisdiction, official/commercial/public, API hints, and whether it already exists in the catalog.",
     "schema": _S({"url": _STR, "jurisdiction": _STR, "category": _STR}, ["url"])},
    {"name": "discover_sources",
     "description": "Finds candidate sources not yet in the catalog through the configured web-search provider: from a "
                    "request (\"commercial contact data providers covering Sweden\"), a jurisdiction's coverage gaps "
                    "and/or a category. Returns ranked, deduplicated, classified candidates with drafts. Adds nothing.",
     "schema": _S({"request": _STR, "jurisdiction": _STR, "category": _STR})},
    {"name": "add_source",
     "description": "Proposes adding a source to the catalog (validated, duplicate-checked). Applied when the investigator "
                    "approves (or at once if they allowed automatic non-destructive changes).",
     "schema": _S(SOURCE_PROPS, ["title", "url", "category"])},
    {"name": "update_source",
     "description": "Proposes changing fields of one of the investigator's own sources.",
     "schema": _S({"source_id": _STR, "changes": {"type": "object"}}, ["source_id", "changes"])},
    {"name": "link_source",
     "description": "Proposes adding/removing jurisdictions or platforms of one of the investigator's own sources. "
                    "Removals always need confirmation.",
     "schema": _S({"source_id": _STR, "add_jurisdictions": _STRS, "remove_jurisdictions": _STRS, "add_platforms": _STRS,
                   "remove_platforms": _STRS}, ["source_id"])},
    {"name": "add_category",
     "description": "Proposes a new source category (group: government, corporate, public_records, judicial, professional, "
                    "regulatory, procurement, news, documents, archives, web, social, domains, infrastructure, commercial, "
                    "other).", "schema": _S({"title": _STR, "group": _STR, "description": _STR}, ["title", "group"])},
    {"name": "add_platform",
     "description": "Proposes a platform. With a profile URL template ({username}) its profile links are recognised "
                    "everywhere and it becomes a web-discovery target.",
     "schema": _S({"name": _STR, "url": _STR, "category": _STR, "profile_url": _STR, "countries": _STRS, "region": _STR,
                   "notes": _STR, "allow_duplicate": {"type": "boolean"}}, ["name", "url"])},
    {"name": "delete_catalog_item",
     "description": "Proposes deleting one of the investigator's own sources, categories or platforms (always confirmed).",
     "schema": _S({"kind": {"type": "string", "enum": ["source", "category", "platform"]}, "id": _STR}, ["kind", "id"])},
    {"name": "connector_health",
     "description": "Status and health of sources (configured, keys missing, last success, failures), by name or category.",
     "schema": _S({"name": _STR, "category": _STR})},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
ALIASES = {"jurisdiction_coverage": "jurisdiction_audit"}


# --------------------------------------------------------------------------- actions

def action_preview(engine, rt, a: dict, cfg) -> dict:
    """What running an action would do: sources, platforms, calls and estimated cost."""
    conns, calls, platforms = set(), 0, 0
    for tn in a.get("transforms") or []:
        t = REG.TRANSFORMS.get(tn)
        if t is None:
            continue
        conns.add(t.connector)
        calls += max(1, int(getattr(t, "estimated_requests", 1) or 1))
        if tn.endswith(".profile_discovery"):
            platforms = max(platforms, int(getattr(t, "estimated_requests", 1) or 1))
    if a.get("kind") == "test_candidates":
        platforms = len(a.get("handles") or [])
    external = sorted({REG.REGISTRY[c].manifest.title for c in conns if REG.REGISTRY[c].manifest.disclosure.destinations})
    est = a.get("estimated_cost_usd")
    money = LEDGER.money(cfg)
    text = (f"You're about to run {len(conns)} source(s)"
            + (f", searching up to {platforms} platform(s)" if platforms else "")
            + (f" through {len(external)} external provider(s)" if external else "") + ".")
    return {"text": text, "sources": sorted(conns), "external_providers": external, "potential_calls": calls,
            "platforms": platforms, "estimated_cost_usd": est,
            "estimated_cost_display": None if est is None else f"{money['symbol']}{est * money['rate']:.4f}"}


def _needs_preview(prev: dict, cfg) -> bool:
    limit = cfg.get_float("ai_action_preview_usd", 0.0, 1_000_000.0)
    est = prev.get("estimated_cost_usd") or 0.0
    return prev["potential_calls"] > PREVIEW_CALLS or (est > 0 and est >= limit)


async def run_action(engine, cfg, db, inv_id: str, action_id: str, *, confirm_paid: bool = False, user_platforms=(),
                     by: str = "investigator", why: str = "", preview_ok: bool = False) -> dict:
    rt = engine.runtime(inv_id)
    acts = {a["id"]: a for a in A.next_actions(rt, engine, cfg, user_platforms)}
    a = acts.get(action_id)
    if a is None:
        raise KeyError("This action is no longer available (the investigation has moved on); reload the assistant")
    if not a["executable"]:
        raise ValueError("This step is for you to do (a link to open or a decision to take); nothing can be run for it")
    if a["kind"] == "paid" and (by != "investigator" or not confirm_paid):
        return {"needs_confirmation": True, "action": a,
                "message": "Paid source: confirm it in the Assistant tab (estimated cost shown there)."}
    if by == "assistant" and not preview_ok:
        prev = action_preview(engine, rt, a, cfg)
        if _needs_preview(prev, cfg):
            p = propose(db, inv_id, {"op": "run_action", "title": a["title"], "destructive": False, "duplicates": [],
                                     "summary": [["Action", a["title"]], ["Why", why or a["reason"]], ["Preview", prev["text"]],
                                                 ["Potential calls", str(prev["potential_calls"])],
                                                 ["Estimated cost", prev["estimated_cost_display"] or "Free / unknown"]],
                                     "payload": {"action_id": a["id"], "why": why}, "preview": prev})
            return {"needs_confirmation": True, "proposal": p, "message": "Large or costly action: a preview is waiting for "
                    "the investigator (Run / Cancel)."}
    if a["kind"] in ("pivot", "expand", "paid", "discover"):
        result = await engine.run_transforms(inv_id, a["entity_id"], a["transforms"],
                                             expand_levels=1 if a["kind"] in ("pivot", "discover") else 0)
    elif a["kind"] == "test_candidates":
        result = await engine.test_hypotheses(inv_id, int(a["count"]))
    elif a["kind"] == "fallback":
        result = await engine.run_fallback(inv_id, a["connector"], a["entity_id"])
    elif a["kind"] == "continue":
        await engine.continue_(inv_id, None)
        result = {"continued": True}
    else:
        raise ValueError("Nothing to run for this step")
    db.assistant_append(inv_id, "action", {"action_id": a["id"], "action_kind": a["kind"], "title": a["title"],
                                           "reason": a["reason"], "by": by, "why": why[:500], "result": result,
                                           "estimated_cost_usd": a.get("estimated_cost_usd")})
    return {"ok": True, "action": a, "result": result}


# --------------------------------------------------------------------------- proposals

def propose(db, inv_id: str, body: dict, *, auto_result: dict | None = None) -> dict:
    """Stores a proposal (pending, or applied automatically when ``auto_result`` is given)."""
    p = {"proposal_id": uuid.uuid4().hex[:10], "status": "applied" if auto_result is not None else "pending",
         "auto": auto_result is not None, "result": auto_result, **body}
    db.assistant_append(inv_id, "proposal", p)
    return {k: v for k, v in p.items() if k != "payload"} | {"payload_preview": _payload_preview(body)}


def _payload_preview(body: dict) -> dict:
    pl = body.get("payload") or {}
    return {k: v for k, v in pl.items() if k in ("title", "url", "category", "jurisdictions", "name", "id", "source_id",
                                                 "action_id")}


def proposals(db, inv_id: str) -> list[dict]:
    """Proposals with their current status (the latest status row wins)."""
    items: dict[str, dict] = {}
    for row in db.assistant_log(inv_id, ("proposal", "proposal_status")):
        if row["kind"] == "proposal":
            items[row["proposal_id"]] = {**{k: v for k, v in row.items() if k not in ("payload",)},
                                         "payload_preview": _payload_preview(row)}
        elif row.get("proposal_id") in items:
            items[row["proposal_id"]].update(status=row.get("status"), result=row.get("result"),
                                             decided_at=row.get("at"), error=row.get("error"))
    return list(items.values())


def _proposal_body(db, inv_id: str, pid: str) -> dict | None:
    for row in db.assistant_log(inv_id, ("proposal",)):
        if row.get("proposal_id") == pid:
            return row
    return None


async def decide(engine, cfg, db, inv_id: str, pid: str, decision: str, user_platforms=()) -> dict:
    """The investigator applies or cancels a proposal."""
    body = _proposal_body(db, inv_id, pid)
    if body is None:
        raise KeyError("Unknown proposal")
    current = next((p for p in proposals(db, inv_id) if p["proposal_id"] == pid), None)
    if current and current["status"] != "pending":
        raise ValueError(f"This proposal is already {current['status']}")
    if decision == "cancel":
        db.assistant_append(inv_id, "proposal_status", {"proposal_id": pid, "status": "cancelled", "by": "investigator"})
        return {"proposal_id": pid, "status": "cancelled"}
    op = body["op"]
    try:
        if op == "run_action":
            result = await run_action(engine, cfg, db, inv_id, body["payload"]["action_id"], user_platforms=user_platforms,
                                      by="investigator", why=body["payload"].get("why", ""), preview_ok=True)
        elif op == "delete_note":
            result = {"deleted": db.note_delete(inv_id, body["payload"]["note_id"])}
        else:
            result = OPS.apply(op, body["payload"], store_for(db))
    except (KeyError, ValueError) as exc:
        db.assistant_append(inv_id, "proposal_status", {"proposal_id": pid, "status": "failed", "by": "investigator",
                                                        "error": str(exc).strip("'\"")[:300]})
        raise ValueError(str(exc).strip("'\"")) from None
    db.assistant_append(inv_id, "proposal_status", {"proposal_id": pid, "status": "applied", "by": "investigator",
                                                    "result": result})
    return {"proposal_id": pid, "status": "applied", "result": result}


def _catalog_change(op: str, args: dict, *, cfg, db, inv_id: str) -> dict:
    store = store_for(db)
    body = OPS.prepare(op, args, store)
    likely = [d for d in body.get("duplicates") or [] if d.get("strength") == "likely"]
    auto = cfg.get_bool("ai_auto_apply_catalog") and not body["destructive"] and body["op"] in OPS.NON_DESTRUCTIVE \
        and not likely
    if auto:
        result = OPS.apply(body["op"], body["payload"], store)
        p = propose(db, inv_id, body, auto_result=result)
        return {"applied": True, "proposal": p, "result": result,
                "message": "Applied (you allowed automatic non-destructive catalog changes). It now appears in Data "
                           "Sources, the jurisdiction pages, the coverage matrix and the audit."}
    p = propose(db, inv_id, body)
    msg = "Waiting for the investigator: shown with Apply / Cancel."
    if likely:
        msg = ("Possible duplicate of an existing record (see duplicates): shown to the investigator, who can cancel or "
               "add it anyway.")
    elif body["destructive"]:
        msg = "Destructive change: it always waits for the investigator's explicit confirmation."
    return {"applied": False, "proposal": p, "duplicates": body.get("duplicates"), "message": msg}


# --------------------------------------------------------------------------- chat tools

def _cap(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= RESULT_CHARS else s[:RESULT_CHARS] + "… (truncated)"


def _platform_ids(names: list[str], user_platforms=()) -> list[str]:
    from ..registry import platforms as P
    out = []
    index = {}
    for p in (*P.curated(), *user_platforms):
        index[p.id.casefold()] = p.id
        index[p.name.casefold()] = p.id
        index[p.name.split(" (")[0].casefold()] = p.id
    for n in names:
        pid = index.get(str(n).strip().casefold())
        if pid is None:
            raise ValueError(f"Unknown platform {n!r} (use platform_lookup)")
        if pid not in out:
            out.append(pid)
    return out


async def call_tool(name: str, args: dict, *, engine, cfg, db, inv_id: str, user_platforms=(),
                    turn: dict | None = None) -> tuple[str, bool]:
    """Runs one tool; returns (JSON text, is_error)."""
    name = ALIASES.get(name, name)
    rt = engine.runtime(inv_id)
    g = rt.graph
    turn = turn if turn is not None else {}
    try:
        if name == "investigation_overview":
            d = A.analyze(rt, engine, cfg, user_platforms)
            costs = d["costs"]
            disc = d["discovery"]
            from ..assistant.providers import config as ai_config
            return _cap({k: d[k] for k in ("query", "status", "mode", "headline", "counts", "verification")} | {
                "countries": list(rt.info.countries),
                "top_identities": [
                    {"letter": i["letter"], "title": i["title"], "verdict": i["verdict_title"],
                     "grouped_because": i["grouped_because"], "members": [
                         {"id": m["id"], "label": m["label"], "verdict": m["verdict_title"],
                          "confidence": m["explanation"]["confidence"], "signals": m["independent_signals"],
                          "note": m["note"]} for m in i["members"][:6]]} for i in d["identities"][:8]],
                "identifiers": {"counts": d["identifiers"]["counts"], "recent": [
                    {k: r[k] for k in ("id", "type", "value", "state", "source", "search_rank", "platform")}
                    for r in sorted(d["identifiers"]["items"], key=lambda r: r["discovered_at"] or "", reverse=True)
                    if r["state"] != "provided"][:15]},
                "discovery": {"stages": [{"n": s["n"], "step": s["step"], "status": s["status"], "detail": s["detail"]}
                                         for s in disc["stages"]], "web": disc["web_discovery"]},
                "costs": {"totals": costs["totals"], "budget": {k: costs["budget"][k] for k in
                                                                ("limit_usd", "spent_usd", "remaining_usd", "hold")},
                          "currency": costs["money"]},
                "ai": {k: v for k, v in ai_config(cfg).items() if k in ("provider", "model", "failover")},
                "notes": len(db.notes(inv_id))}), False
        if name == "candidate_identities":
            return _cap(A.identities(g)[: int(args.get("limit") or 20)]), False
        if name == "entity_details":
            from ..api.views import entity_detail
            e = g.by_id(str(args.get("entity_id") or ""))
            if e is None:
                return _cap({"error": "No entity with this id"}), True
            d = entity_detail(g, e)
            d["properties"] = [p for p in d["properties"] if not p["local_only"]]
            d["assessment"] = A.assess(e, A._under_hypothesis(g, e))
            d["origin"] = A.origin_of(g, e)
            return _cap(d), False
        if name == "identifiers":
            rows = IDS.rows(rt, int(args.get("limit") or 150))
            if args.get("state"):
                rows = [r for r in rows if r["state"] == str(args["state"]).lower()]
            if args.get("type"):
                rows = [r for r in rows if r["type"] == args["type"] or r["entity_type"] == args["type"]]
            return _cap({"items": rows, "states": IDS.STATES}), False
        if name == "platform_table":
            rows = A.platform_table(g, user_platforms)
            if args.get("only_found"):
                rows = [r for r in rows if r["check"] == "found"]
            return _cap(rows[:150]), False
        if name == "username_candidates":
            return _cap(A.generated(rt)), False
        if name == "discovery_report":
            from ..discovery import stages as STAGES
            rep = STAGES.report(rt, cfg, A.identities(g))
            rep["journal"] = rep["journal"][-int(args.get("journal_limit") or 60):]
            return _cap(rep), False
        if name == "searches_performed":
            s = A.searched(rt, int(args.get("limit") or 60))
            s["web_queries"] = [{k: j.get(k) for k in ("at", "connector", "query", "purpose", "status", "results", "profiles")}
                                for j in (rt.data.get("search_journal") or [])[-int(args.get("limit") or 60):]]
            return _cap(s), False
        if name == "next_actions":
            return _cap(A.next_actions(rt, engine, cfg, user_platforms)), False
        if name == "cost_report":
            c = LEDGER.investigation_costs(rt, db, cfg, engine)
            c["chain"] = c["chain"][-60:]
            return _cap(c), False
        if name == "estimate_cost":
            acts = {a["id"]: a for a in A.next_actions(rt, engine, cfg, user_platforms)}
            a = acts.get(str(args.get("action_id") or ""))
            if a is None:
                return _cap({"error": "Unknown action id (call next_actions)"}), True
            return _cap(action_preview(engine, rt, a, cfg)), False
        if name == "list_notes":
            return _cap(db.notes(inv_id)), False
        if name == "run_action":
            res = await run_action(engine, cfg, db, inv_id, str(args.get("action_id") or ""), user_platforms=user_platforms,
                                   by="assistant", why=str(args.get("why") or ""))
            if res.get("proposal"):
                turn.setdefault("proposals", []).append(res["proposal"])
            return _cap(res), bool(res.get("needs_confirmation")) and not res.get("proposal")
        if name == "search_identifier":
            e = g.by_id(str(args.get("entity_id") or ""))
            if e is None:
                return _cap({"error": "Only identifiers already in the graph can be searched (no such entity id)"}), True
            cats = [str(c) for c in args.get("categories") or []]
            todo = A._free_transforms(engine, rt, e)
            if cats:
                todo = [t for t in todo if REG.REGISTRY[REG.TRANSFORMS[t].connector].manifest.category in cats]
            if args.get("include_web"):
                primary = engine.search_primary(rt)
                if primary:
                    todo += [t.name for t in REG.transforms_for(e.type) if t.connector == primary
                             and not engine.tried(rt, t.name, e.key)]
            if not todo:
                return _cap({"result": "No available source left to run on this identifier"
                             + (f" in categories {cats}" if cats else "")}), False
            res = await engine.run_transforms(inv_id, e.id, todo)
            db.assistant_append(inv_id, "action", {"action_id": f"search:{e.id}", "action_kind": "search",
                                                   "title": f"Search {e.label}", "reason": str(args.get("why") or "")[:500],
                                                   "by": "assistant", "result": res})
            return _cap({"queued": res["queued"], "skipped": res["skipped"][:20],
                         "note": "Queued. Use wait_for_results to see what they find."}), False
        if name == "discover_profiles":
            primary = engine.search_primary(rt)
            if not primary:
                from ..discovery import router as ROUTER
                return _cap({"error": ROUTER.unavailable_message(cfg) or "No web-search provider is available"}), True
            eid = str(args.get("entity_id") or "")
            e = g.by_id(eid) if eid else g.target
            if e is None or e.type not in ("person", "username"):
                return _cap({"error": "discover_profiles needs the target person or a username already in the graph"}), True
            plats = _platform_ids([str(p) for p in args.get("platforms") or []], user_platforms)
            tname = f"{primary}.profile_discovery"
            t = REG.TRANSFORMS[tname]
            est = LEDGER.task_estimate(t, REG.REGISTRY[primary], cfg)
            hold = LEDGER.check(rt, db, cfg, est)
            if hold:
                return _cap({"error": "Budget: this search would raise the estimated cost from "
                                      f"${hold['current_usd']:.4f} to ${hold['after_usd']:.4f} (limit ${hold['limit_usd']:.4f}). "
                                      "Ask the investigator to raise the budget."}), True
            if plats:
                rt.info.options.setdefault("discovery_focus", {})[e.key] = plats
            # a focused check ("check Pinterest") stays focused; a general discovery also follows what it finds
            res = await engine.run_transforms(inv_id, e.id, [tname], expand_levels=0 if plats else 1)
            db.assistant_append(inv_id, "action", {"action_id": f"discover:{e.id}", "action_kind": "discover",
                                                   "title": f"Web profile discovery for {e.label}"
                                                   + (f" on {', '.join(plats)}" if plats else ""),
                                                   "reason": str(args.get("why") or "")[:500], "by": "assistant",
                                                   "result": res, "estimated_cost_usd": round(est, 4)})
            return _cap({"queued": res["queued"], "skipped": res["skipped"], "provider": REG.REGISTRY[primary].manifest.title,
                         "platforms": plats or "the highest-priority platforms", "estimated_cost_usd": round(est, 4),
                         "note": "Queued. Use wait_for_results, then identifiers / discovery_report."}), False
        if name == "add_identifier":
            value = str(args.get("value") or "").strip()
            said = str(turn.get("user_text") or "")
            if not value or value.casefold() not in said.casefold():
                return _cap({"error": "Only identifiers the investigator typed in this message can be added; this value "
                                      "is not in their message"}), True
            res = await engine.add_entity(inv_id, str(args.get("type")), value, "", g.target.id if g.target else None,
                                          "USES" if args.get("type") in ("username", "email", "phone", "domain") else
                                          "ASSOCIATED_WITH")
            db.log_decision(inv_id, "add_entity", {"type": args.get("type"), "label": value, "via": "assistant",
                                                   "note": str(args.get("why") or "")[:300]})
            return _cap({"added": res}), False
        if name == "wait_for_results":
            before = {e.key for e in g.entities.values()}
            deadline = time.monotonic() + min(int(args.get("seconds") or 10), 45)
            while time.monotonic() < deadline and (len(rt.queue) or any(rt.running.values())):
                await asyncio.sleep(0.5)
            new = [e for e in g.entities.values() if e.key not in before]
            return _cap({"still_queued": len(rt.queue), "running": sum(rt.running.values()),
                         "budget_hold": rt.data.get("budget_hold"),
                         "new_entities": [{"id": e.id, "type": e.type, "label": e.label, **A.origin_of(g, e)}
                                          for e in new[:60]], "new_count": len(new)}), False
        if name == "add_note":
            note = {"title": str(args.get("title") or "")[:200], "body": str(args.get("body") or "")[:4000],
                    "identifiers": [str(x)[:200] for x in args.get("identifiers") or []][:30],
                    "status": str(args.get("status") or "info")[:20], "by": "assistant"}
            n = db.note_put(inv_id, uuid.uuid4().hex[:10], note)
            turn.setdefault("notes", []).append(n["id"])
            return _cap({"note": n}), False
        if name == "update_note":
            cur = next((n for n in db.notes(inv_id) if n["id"] == args.get("note_id")), None)
            if cur is None:
                return _cap({"error": "Unknown note id"}), True
            data = {k: cur.get(k) for k in ("title", "body", "identifiers", "status", "by")}
            for k in ("title", "body", "identifiers", "status"):
                if args.get(k) not in (None, ""):
                    data[k] = args[k]
            return _cap({"note": db.note_put(inv_id, cur["id"], data)}), False
        if name == "delete_note":
            cur = next((n for n in db.notes(inv_id) if n["id"] == args.get("note_id")), None)
            if cur is None:
                return _cap({"error": "Unknown note id"}), True
            p = propose(db, inv_id, {"op": "delete_note", "title": f"Delete note “{cur.get('title')}”", "destructive": True,
                                     "summary": [["Note", cur.get("title") or ""]], "payload": {"note_id": cur["id"]},
                                     "duplicates": []})
            turn.setdefault("proposals", []).append(p)
            return _cap({"proposal": p, "message": "Deleting a note always waits for the investigator."}), False
        # ------------------------------------------------------------ catalog
        if name == "jurisdiction_audit":
            from ..registry import catalog_audit as AUDIT
            from ..registry import coverage_model as CM
            code = str(args.get("country") or "").upper()
            snap = store_for(db).snapshot()
            try:
                a = AUDIT.audit(code, CM.CoverageContext(cfg, db.health_records(), snap), snap,
                                db.get_setting(f"discovery:{code}"))
            except KeyError:
                return _cap({"error": f"Unknown jurisdiction {code!r}"}), True
            db.assistant_append(inv_id, "action", {"action_id": f"audit:{code}", "action_kind": "audit",
                                                   "title": f"Jurisdiction audit: {a['name']}", "reason": "", "by": "assistant",
                                                   "result": {"gaps": len(a.get("gaps") or [])}})
            return _cap({k: a[k] for k in ("name", "coverage", "gaps", "global_connectors", "improvements", "legal")}
                        | {"recommendations": (a.get("recommendations") or [])[:15],
                           "sources": [{k: s.get(k) for k in ("title", "url", "origin", "automation", "evidence", "nature")}
                                       for s in a["sources"]][:80]}), False
        if name == "catalog_search":
            snap = store_for(db).snapshot()
            return _cap(OPS.search(snap, cfg, query=str(args.get("query") or ""), jurisdiction=str(args.get("jurisdiction") or ""),
                                   category=str(args.get("category") or ""), limit=int(args.get("limit") or 30))), False
        if name == "catalog_vocab":
            from ..registry import catalog as CAT
            from ..registry.access_class import ACCESS_MODELS
            v = CAT.vocab(store_for(db).snapshot())
            return _cap({"categories": [{"id": c["id"], "title": c["title"], "group": c["group"], "origin": c["origin"]}
                                        for c in v["categories"]], "groups": v["groups"], "source_types": list(v["source_types"]),
                         "access": v["access"], "official_kinds": v["kinds"], "provider_classes": v["provider_classes"],
                         "access_models": ACCESS_MODELS,
                         "jurisdictions": "ISO-2 codes (SE, RO, DE…), EU, INT"}), False
        if name == "platform_lookup":
            return _cap(OPS.platforms(store_for(db).snapshot(), cfg, str(args.get("query") or ""))), False
        if name == "inspect_source_url":
            from ..registry import source_discovery as SD
            http = engine.http_factory()
            try:
                r = await SD.inspect_url(str(args.get("url") or ""), http, store_for(db).snapshot(),
                                         jurisdiction=str(args.get("jurisdiction") or ""), category=str(args.get("category") or ""))
            finally:
                await http.aclose()
            return _cap(r), False
        if name == "discover_sources":
            from ..registry import source_discovery as SD
            http = engine.http_factory()
            try:
                r = await SD.discover(request=str(args.get("request") or ""), jurisdiction=str(args.get("jurisdiction") or ""),
                                      category=str(args.get("category") or ""), cfg=cfg, db=db, http=http,
                                      snap=store_for(db).snapshot(), inv_id=inv_id)
            finally:
                await http.aclose()
            turn["discovery"] = {"queries": r.get("queries"), "candidates": len(r.get("candidates") or [])}
            r["candidates"] = [{k: c[k] for k in ("id", "title", "url", "rank", "query", "classification", "draft", "score")}
                               for c in r.get("candidates") or []]
            return _cap(r), not r.get("ok", True)
        if name in ("add_source", "update_source", "link_source", "add_category", "add_platform", "delete_catalog_item"):
            op = name if name != "delete_catalog_item" else f"delete_{args.get('kind')}"
            res = _catalog_change(op, dict(args), cfg=cfg, db=db, inv_id=inv_id)
            turn.setdefault("proposals", []).append(res["proposal"])
            return _cap(res), False
        if name == "connector_health":
            from ..registry.health import connector_health, snapshot as health_snapshot
            records, snap_h = db.health_records(), health_snapshot()
            out = []
            for n, c in REG.REGISTRY.items():
                if args.get("name") and n != args["name"]:
                    continue
                if args.get("category") and c.manifest.category != args["category"]:
                    continue
                st, note = REG.connector_status(c, cfg)
                out.append({"name": n, "title": c.manifest.title, "status": st, "note": note,
                            "health": connector_health(n, cfg, records.get(n), snap_h)})
                if len(out) >= 40:
                    break
            return _cap(out), False
        return _cap({"error": f"Unknown tool {name}"}), True
    except (KeyError, ValueError) as exc:
        return _cap({"error": str(exc).strip("'\"")}), True


# --------------------------------------------------------------------------- chat

def _pid(provider) -> str:
    return getattr(provider, "provider_id", None) or provider.id


def transcript(db, inv_id: str, provider) -> list[dict]:
    """The conversation so far for ``provider``: its own turns replayed natively (append-only), other providers' turns as
    plain text, so the conversation continues when the provider changes."""
    pid = provider if isinstance(provider, str) else _pid(provider)
    as_text = getattr(provider, "assistant_text", None) if not isinstance(provider, str) else None
    out: list[dict] = []
    for row in db.assistant_log(inv_id, ("chat",)):
        if row.get("provider") == pid:
            out.extend(row.get("native") or [])
        elif as_text is not None and row.get("user") and row.get("answer"):
            out.append(provider.user_message(row["user"]))
            out.append(as_text(f"[answered by {row.get('provider')} / {row.get('model')}] {row['answer']}"))
    return out


def history(db, inv_id: str) -> list[dict]:
    """What the investigator sees: messages, tool calls, actions and proposals, in order (no provider internals)."""
    status = {p["proposal_id"]: p for p in proposals(db, inv_id)}
    out = []
    for row in db.assistant_log(inv_id):
        if row["kind"] == "chat":
            out.append({"seq": row["seq"], "at": row["at"], "kind": "chat", "provider": row["provider"],
                        "model": row.get("model"), "user": row.get("user"), "answer": row.get("answer"),
                        "tool_calls": row.get("tool_calls") or [], "stop": row.get("stop"), "note": row.get("note"),
                        "destination": row.get("destination"), "handled_by": row.get("handled_by"),
                        "attempts": row.get("attempts") or [], "usage": row.get("usage"),
                        "proposals": row.get("proposals") or []})
        elif row["kind"] == "proposal":
            out.append({"seq": row["seq"], "at": row["at"], "kind": "proposal", **status.get(row["proposal_id"], {})})
        elif row["kind"] == "proposal_status":
            continue
        else:
            out.append({"seq": row["seq"], "at": row["at"], "kind": row["kind"],
                        **{k: row.get(k) for k in ("action_id", "action_kind", "title", "reason", "by", "why", "result",
                                                   "estimated_cost_usd")}})
    return out


def digest(rt, engine, cfg, db) -> str:
    """A compact state header added to each message, so "the username we just found on Pinterest" is unambiguous."""
    g = rt.graph
    rows = [r for r in IDS.rows(rt, 300) if r["state"] not in ("provided",)]
    rows.sort(key=lambda r: r["discovered_at"] or "", reverse=True)
    idents = "; ".join(f"{r['type']} {r['value']} [{r['state']}"
                       + (f", {r['platform']}" if r.get("platform") else "")
                       + (f", rank {r['search_rank']}" if r.get("search_rank") else "") + f", id {r['id']}]"
                       for r in rows[:12]) or "none yet"
    budget = LEDGER.budget_of(rt)
    spent = LEDGER.spent(rt, db, cfg)
    primary = engine.search_primary(rt)
    return (f"(Investigation {rt.id} · {rt.status} · target: {g.target.label if g.target else '—'} · countries: "
            f"{', '.join(rt.info.countries) or 'none inferred'} · estimated cost so far ${spent:.4f}"
            + (f" of ${budget:.2f} budget" if budget is not None else " (no budget limit)")
            + f" · web search: {REG.REGISTRY[primary].manifest.title if primary else 'unavailable'}"
            + f" · recent identifiers: {idents}. Use the tools to read the current state.)")


def _record_usage(db, cfg, inv_id: str, provider, reply) -> dict:
    pid = _pid(provider)
    u = reply.usage or {}
    model = reply.model or provider.model
    est = PR.token_cost(PR.price_for_model(pid, model, cfg), u) if u else None
    local = provider.destination in ("127.0.0.1", "localhost", "::1")
    if est is None and local:
        est = 0.0                                # a model on this computer has no per-token charge
    actual = u.get("cost")
    status = "actual" if actual is not None else "estimated" if est is not None else "unknown"
    rec = {"investigation_id": inv_id, "provider": pid if pid in PROVIDERS else pid, "service": "ai_assistant",
           "model": model, **{k: u.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "cached_tokens",
                                                        "reasoning_tokens", "total_tokens")},
           "requests": 1, "estimated_cost": est, "actual_cost": actual, "cost_status": status,
           "data": {"destination": provider.destination, **({"note": "local model: no per-token charge"} if local else {})}}
    if u or pid in PROVIDERS:
        db.usage_add(rec)
    return {k: rec[k] for k in ("model", "prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens",
                                "total_tokens", "estimated_cost", "actual_cost", "cost_status")}


def _candidates(cfg) -> list[tuple[str, object, str]]:
    """(provider id, provider or None, why not usable) in the order to try: primary, then the failover chain."""
    out: list[tuple[str, object, str]] = []
    try:
        p = make(cfg)
        out.append((_pid(p), p, ""))
    except ProviderError as exc:
        out.append((primary_id(cfg), None, str(exc)))
    for pid, why in fallbacks(cfg, exclude=out[0][0]):
        if why:
            out.append((pid, None, why))
            continue
        try:
            out.append((pid, build(cfg, pid), ""))
        except ProviderError as exc:
            out.append((pid, None, str(exc)))
    return out


async def chat(engine, cfg, db, inv_id: str, text: str, user_platforms=(), *, confirm_budget: bool = False) -> dict:
    rt = engine.runtime(inv_id)                                  # 404 before calling anyone
    cands = _candidates(cfg)
    if not any(p is not None for _, p, _ in cands):
        raise ProviderError(cands[0][2] or "No AI provider is configured (Settings → AI providers)", retryable=False)
    attempts: list[dict] = []
    turn_ctx: dict = {"user_text": text}
    row: dict | None = None
    for pid, provider, why in cands:
        if provider is None:
            if pid not in ("none",):
                attempts.append({"provider": pid, "error": why, "skipped": True})
            continue
        # budget: one model call at the forecast assumption, checked before anything is sent
        price = PR.price_for_model(_pid(provider), provider.model, cfg)
        per_call = PR.token_cost(price, {"prompt_tokens": LEDGER.AI_TURN_ASSUMPTION["prompt_tokens"],
                                         "completion_tokens": LEDGER.AI_TURN_ASSUMPTION["completion_tokens"]}) or 0.0
        hold = None if confirm_budget else LEDGER.check(rt, db, cfg, per_call * 3)
        if hold:
            answer = (f"Budget check: answering may raise the estimated cost from ${hold['current_usd']:.4f} to about "
                      f"${hold['after_usd']:.4f} (limit ${hold['limit_usd']:.4f}). Raise the budget or confirm to continue.")
            row = {"user": text.strip(), "answer": answer, "tool_calls": [], "stop": "budget", "note": "budget",
                   "model": provider.model, "destination": provider.destination, "native": [], "budget_hold": hold,
                   "handled_by": None, "attempts": attempts, "proposals": []}
            db.assistant_append(inv_id, "chat", row, provider=_pid(provider))
            return {k: v for k, v in row.items() if k != "native"}
        native = transcript(db, inv_id, provider)
        turn: list[dict] = [provider.user_message(f"{text.strip()}\n\n{digest(rt, engine, cfg, db)}")]
        calls: list[dict] = []
        usage_rows: list[dict] = []
        answer, stop, note = "", "end", ""
        failed: ProviderError | None = None
        for _ in range(MAX_STEPS):
            try:
                reply = await provider.complete(SYSTEM, native + turn, TOOLS)
            except ProviderError as exc:
                failed = exc
                break
            usage_rows.append(_record_usage(db, cfg, inv_id, provider, reply))
            turn.extend(reply.native)
            answer, stop, note = reply.text, reply.stop, reply.note
            if reply.stop != "tool_use":
                break
            results = []
            for c in reply.tool_calls:
                out, err = await call_tool(c.name, c.input, engine=engine, cfg=cfg, db=db, inv_id=inv_id,
                                           user_platforms=user_platforms, turn=turn_ctx)
                results.append((c, out, err))
                calls.append({"tool": c.name, "input": c.input, "error": err, "result_preview": out[:400]})
            turn.extend(provider.tool_results(results))
        else:
            note = "Stopped after the maximum number of tool steps for one message."
        if failed is not None:
            attempts.append({"provider": _pid(provider), "model": provider.model, "error": str(failed)})
            if not calls and failed.retryable:
                continue                                  # nothing ran yet: the next provider may answer
            answer, stop, note = "", "error", str(failed)
        if stop == "tool_use":
            # the transcript must not end on unanswered tool calls: keep only complete turns
            answer = answer or "I ran out of steps before finishing; ask me to continue."
        keep = turn if stop not in ("error", "refusal") and failed is None else []
        total = {"prompt_tokens": sum(u["prompt_tokens"] for u in usage_rows),
                 "completion_tokens": sum(u["completion_tokens"] for u in usage_rows),
                 "estimated_cost": round(sum(u["estimated_cost"] or 0 for u in usage_rows), 6)
                 if any(u["estimated_cost"] is not None for u in usage_rows) else None,
                 "actual_cost": round(sum(u["actual_cost"] or 0 for u in usage_rows), 6)
                 if any(u["actual_cost"] is not None for u in usage_rows) else None, "requests": len(usage_rows)}
        title = PROVIDERS.get(_pid(provider), {}).get("title", _pid(provider))
        row = {"user": text.strip(), "answer": answer, "tool_calls": calls, "stop": stop, "note": note,
               "model": provider.model, "destination": provider.destination, "native": keep,
               "handled_by": {"provider": _pid(provider), "title": title,
                              "model": next((u["model"] for u in reversed(usage_rows) if u.get("model")), provider.model),
                              "fallback": bool(attempts)},
               "attempts": attempts, "usage": total, "proposals": turn_ctx.get("proposals") or [],
               "notes": turn_ctx.get("notes") or []}
        db.assistant_append(inv_id, "chat", row, provider=_pid(provider))
        return {k: v for k, v in row.items() if k != "native"}
    # every provider failed before doing anything
    msg = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts) or "No AI provider answered"
    row = {"user": text.strip(), "answer": "", "tool_calls": [], "stop": "error", "note": msg, "model": "",
           "destination": "", "native": [], "handled_by": None, "attempts": attempts, "proposals": []}
    db.assistant_append(inv_id, "chat", row, provider=cands[0][0])
    return {k: v for k, v in row.items() if k != "native"}


__all__ = ["TOOLS", "SYSTEM", "chat", "call_tool", "run_action", "history", "transcript", "proposals", "decide",
           "action_preview", "digest"]
