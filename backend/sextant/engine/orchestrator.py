"""Investigation orchestrator — the autonomous transform engine.

    input ─▶ seeds ─▶ plan applicable transforms ─▶ priority queue ─▶ workers (rate-limited, budgeted)
                          ▲                                               │
                          │                                     TransformResult (sources, entities,
                          │                                     facts, relationships, checks)
                          │                                               │
              replan entities whose value ◀── resolution & ranking ◀── merge into graph (provenance)
              crossed the expansion threshold     (adaptive cadence)

* Tasks are ``(transform, entity)`` pairs, deduplicated for the life of the investigation.
* Priority = investigative value of the entity, inherent usefulness of the transform, and closeness
  to the input. Investigator requests jump the queue.
* The queue keeps one heap per connector, so a saturated or slow source never blocks the others.
* Budgets (entities, requests, time, runs, per-connector calls) stop *scheduling*; running transforms
  finish and their results are kept. Remaining tasks stay queued, so "Continue" with higher limits
  picks up where the run stopped.
* Everything is persisted incrementally (dirty tracking) every couple of seconds.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..model import Graph, Observation, RelEvidence, Source, Task, TransformRun, short_hash, utcnow
from ..ontology import ENTITY_TYPES, normalize_value
from ..discovery import router as ROUTER
from ..registry import connectors as REG
from ..registry.catalog import store_for
from ..registry.platforms import user_platforms_in_scope
from ..resolution.scorer import link_same_owner, resolve
from ..sdk.context import TransformContext
from ..sdk.errors import TransformError, VerificationRequired
from ..sdk.http import FetchError, SafeHttpClient, limiter_for
from ..sdk.result import TransformResult
from ..sdk.transform import EntityView, InvestigationInfo, Transform
from ..storage.db import Database, DbCache
from .countries import infer_countries
from .coverage import CoverageTracker, global_coverage
from .events import EventLog
from .input import ParsedInput
from .limits import Limits, resolve_limits
from .merge import merge_result

log = logging.getLogger("sextant.engine")

USER_SOURCE_ID = "user-input"
FLUSH_EVERY = 2.0
SAME_OWNER_EVERY = 10.0
# Back-off: a provider that refuses this many runs in a row (403/bot challenge, or 429 after the client's own
# Retry-After-aware retries) gets no further automatic requests in the investigation. Sextant never works around it.
REFUSAL_STATUSES = ("blocked", "rate_limited")
REFUSALS_BEFORE_BACKOFF = 3
FETCH_STATUS = {"rate_limited": "rate_limited", "timeout": "timeout", "budget": "budget_exhausted",
                "blocked_url": "error", "network": "error", "too_many_redirects": "error"}
TERMINAL = ("completed", "limit_reached", "cancelled", "failed")


# --------------------------------------------------------------------------- queue

class TaskQueue:
    """Priority queue with one heap per connector (fair, capacity-aware popping)."""

    def __init__(self) -> None:
        self._heaps: dict[str, list] = defaultdict(list)
        self._seq = itertools.count()
        self.size = 0

    def push(self, connector: str, task: Task) -> None:
        heapq.heappush(self._heaps[connector], (-task.priority, next(self._seq), task))
        self.size += 1

    def pop(self, can_run: Callable[[str], bool]) -> tuple[str, Task] | None:
        best: tuple[str, tuple] | None = None
        for conn, heap in self._heaps.items():
            if heap and can_run(conn) and (best is None or heap[0][:2] < best[1][:2]):
                best = (conn, heap[0])
        if best is None:
            return None
        heapq.heappop(self._heaps[best[0]])
        self.size -= 1
        return best[0], best[1][2]

    def remove(self, connector: str) -> list[Task]:
        """Takes every queued task of one connector out of the queue."""
        heap = self._heaps.pop(connector, [])
        self.size -= len(heap)
        return [entry[2] for entry in heap]

    def drain(self) -> list[Task]:
        out = [entry[2] for heap in self._heaps.values() for entry in heap]
        self._heaps.clear()
        self.size = 0
        return out

    def __len__(self) -> int:
        return self.size


# --------------------------------------------------------------------------- runtime state

@dataclass
class Runtime:
    id: str
    graph: Graph
    info: InvestigationInfo
    limits: Limits
    options: dict
    title: str
    status: str = "created"
    phase: str = ""
    error: str | None = None
    stop_reason: str = ""
    tasks: dict[str, Task] = field(default_factory=dict)
    runs: dict[str, TransformRun] = field(default_factory=dict)
    planned: set[tuple[str, str]] = field(default_factory=set)
    planned_value: dict[str, float] = field(default_factory=dict)
    calls: Counter = field(default_factory=Counter)
    running: Counter = field(default_factory=Counter)
    counters: dict = field(default_factory=dict)
    events: EventLog = field(default_factory=EventLog)
    coverage: CoverageTracker = field(default_factory=CoverageTracker)
    queue: TaskQueue = field(default_factory=TaskQueue)
    dirty_runs: set[str] = field(default_factory=set)
    dirty_tasks: set[str] = field(default_factory=set)
    pending_raw: dict = field(default_factory=dict)     # digest -> raw response to store as evidence
    pending_health: list = field(default_factory=list)  # (connector, status, at) outcomes to record
    refusals: Counter = field(default_factory=Counter)  # consecutive blocked / rate-limited runs per connector
    backed_off: dict = field(default_factory=dict)      # connector -> why the engine stopped sending it requests
    started_at: str = ""
    finished_at: str = ""
    deadline: float = 0.0
    elapsed_before: float = 0.0
    loop_started: float = 0.0
    requests: int = 0
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    loop_task: asyncio.Task | None = None
    max_depth_override: int = 0
    auto_expand: bool = False          # automatic expansion (INVESTIGATE / Continue); off in manual mode
    needs_resolve: bool = True
    data: dict = field(default_factory=dict)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.resume_event.set()

    @property
    def max_depth(self) -> int:
        return max(self.limits.max_depth, self.max_depth_override)

    @property
    def elapsed(self) -> float:
        return self.elapsed_before + (time.monotonic() - self.loop_started if self.loop_started else 0.0)


# --------------------------------------------------------------------------- engine

class Engine:
    def __init__(self, db: Database, settings: Settings, data_dir: Path,
                 http_factory: Callable[..., SafeHttpClient] | None = None) -> None:
        self.db = db
        self.settings = settings
        self.data_dir = data_dir
        self.http_factory = http_factory or (lambda **kw: SafeHttpClient(**kw))
        self.active: dict[str, Runtime] = {}
        REG.load_all()

    # ------------------------------------------------------------------ creation
    def create(self, parsed: ParsedInput, *, depth: str = "2", overrides: dict | None = None,
               options: dict | None = None, purpose: str = "") -> str:
        if parsed.errors and not (parsed.seeds or parsed.target_name):
            raise ValueError("; ".join(parsed.errors))
        limits = resolve_limits(depth, overrides)
        if limits.username_guesses and not self.settings.get_bool("generate_username_guesses"):
            limits = Limits.from_dict({**limits.to_dict(), "username_guesses": False})
        options = dict(options or {})
        if "budget_usd" not in options:
            default_budget = self.settings.get_float("default_budget_usd", 0.0, 1_000_000.0)
            options["budget_usd"] = default_budget or None
        data = {"title": parsed.title, "purpose": purpose[:2000], "input": parsed.to_dict(), "limits": limits.to_dict(),
                "options": options, "seed_summary": [f"{s.type}: {s.label}" for s in parsed.seeds][:12]}
        inv_id = self.db.create_investigation(data, parsed.target_type, limits.depth)
        g = Graph(self.db.id_fn(inv_id))
        self._seed_graph(g, parsed)
        data["target_key"] = g.target_key
        data["info"] = self._info_dict(g, parsed, options)
        self.db.update_investigation(inv_id, data=data)
        resolve(g)
        self.db.flush_graph(inv_id, g)
        return inv_id

    def _seed_graph(self, g: Graph, parsed: ParsedInput) -> None:
        g.add_source(Source(id=USER_SOURCE_ID, connector="investigator", kind="user", title="Entered by the investigator",
                            reliability="official"))

        def obs(key: str, prop: str, value: Any) -> None:
            g.add_observation(Observation(id=short_hash(key, prop, str(value), USER_SOURCE_ID), entity=key, prop=prop,
                                          value=value, source=USER_SOURCE_ID, evidence="Investigation input"))

        def link(a: str | None, b: str, rel: str) -> None:
            if a:
                g.add_relationship(a, b, rel, RelEvidence(USER_SOURCE_ID, "Provided by the investigator", "", 1.0))

        target_key = None
        if parsed.target_type in ("person", "organization"):
            ttype = parsed.target_type
            label = parsed.target_name or ("Unknown person" if ttype == "person" else "Unknown organization")
            if not parsed.target_name and parsed.seeds:
                label += f" ({parsed.seeds[0].label})"
            ent, _ = g.ensure_entity(ttype, "target", label, depth=0, status="seed")
            target_key = ent.key
            obs(target_key, "@", label)
            if parsed.target_name:
                obs(target_key, "full_name" if ttype == "person" else "name", parsed.target_name)
            for a in parsed.aliases:
                obs(target_key, "aliases", a)
            if parsed.birth_year and ttype == "person":
                obs(target_key, "birth_year", parsed.birth_year)          # local_only: never sent anywhere
            if ttype == "person":
                for n in [parsed.target_name, *parsed.aliases]:
                    nv = normalize_value("name", n) if n else None
                    if nv:
                        ne, _ = g.ensure_entity("name", nv, n, depth=0, status="seed")
                        obs(ne.key, "@", n)
                        link(target_key, ne.key, "HAS_NAME")
        g.target_key = target_key
        rel_for = {"email": "USES", "phone": "USES", "username": "USES", "domain": "USES", "account": "OWNS",
                   "website": "OWNS", "location": "LOCATED_IN", "organization": "ASSOCIATED_WITH", "ip": "USES",
                   "asn": "ASSOCIATED_WITH"}
        for s in parsed.seeds:
            ent, _ = g.ensure_entity(s.type, s.value, s.label, depth=0, status="seed")
            obs(ent.key, "@", s.label)
            for k, v in (s.attrs or {}).items():
                if v not in (None, "", [], {}) and k not in ("search_variants", "e164", "country_code"):
                    obs(ent.key, k, v)
            if s.type == "organization":
                obs(ent.key, "name", s.label)
            link(target_key, ent.key, rel_for.get(s.type, "ASSOCIATED_WITH"))

    def _info_dict(self, g: Graph, parsed: ParsedInput, options: dict) -> dict:
        seeds: dict[str, list[str]] = defaultdict(list)
        for s in parsed.seeds:
            seeds[s.type].append(s.attrs.get("username") if s.type == "username" else s.value)
        countries = [c["code"] for c in infer_countries(parsed, options.get("countries") or [])]
        return {"target_type": parsed.target_type, "target_names": [n for n in [parsed.target_name, *parsed.aliases] if n],
                "locations": [s.label for s in parsed.seeds if s.type == "location"], "countries": countries,
                "default_region": options.get("default_region") or self.settings.get("default_region") or None,
                "seeds": dict(seeds)}

    # ------------------------------------------------------------------ loading
    def runtime(self, inv_id: str) -> Runtime:
        rt = self.active.get(inv_id)
        if rt is not None:
            return rt
        inv = self.db.get_investigation(inv_id)
        if inv is None:
            raise KeyError(inv_id)
        data = inv["data"] or {}
        g = self.db.load_graph(inv_id, data.get("target_key"), data.get("aliases") or {}, data.get("merge_log") or [])
        info_d = data.get("info") or {}
        options = data.setdefault("options", {})          # the same dict: budget changes are persisted with data
        limits = Limits.from_dict(data.get("limits") or {})
        rt = Runtime(id=inv_id, graph=g, limits=limits, options=options, title=data.get("title") or "Investigation",
                     info=self._make_info(inv_id, g, info_d, options, limits), status=inv["status"], data=data,
                     coverage=CoverageTracker.from_dict(data.get("coverage_raw")),
                     elapsed_before=float(data.get("elapsed") or 0.0), started_at=data.get("started_at") or "",
                     finished_at=data.get("finished_at") or "", stop_reason=data.get("stop_reason") or "",
                     error=data.get("error"), max_depth_override=int(data.get("max_depth_override") or 0),
                     auto_expand=bool(data.get("auto_expand")),
                     counters=dict(data.get("counters") or {}))
        rt.planned_value = {}
        for r in self.db.load_runs(inv_id):
            rt.runs[r.id] = r
            rt.calls[r.connector] += 1
        for t in self.db.load_tasks(inv_id):
            rt.tasks[t.id] = t
            rt.planned.add((t.transform, t.entity_key))
            if t.status in ("queued", "running"):
                t.status = "queued"
                tr = REG.TRANSFORMS.get(t.transform)
                if tr is not None:
                    rt.queue.push(tr.connector, t)
                    rt.coverage.enqueue(tr.connector)
        if rt.status == "running":           # the process stopped mid-run
            rt.status = "paused"
            rt.phase = "Interrupted — press Continue to resume"
        self.active[inv_id] = rt
        return rt

    def _make_info(self, inv_id: str, g: Graph, d: dict, options: dict, limits: Limits) -> InvestigationInfo:
        opts = {"respect_robots": options.get("respect_robots", self.settings.get_bool("respect_robots_txt")),
                "include_adult": options.get("include_adult", self.settings.get_bool("include_adult_sites")),
                "platforms": options.get("platforms") or None,
                "contact_email": self.settings.get("contact_email") or None,
                "username_guesses": limits.username_guesses or bool(options.get("test_hypotheses")),
                "username_guess_count": options.get("username_guess_count"),
                "search_everything": bool(options.get("search_everything")),
                "search_query_cap": limits.search_query_cap,
                "depth_preset": str(limits.depth),
                "discovery_query_cap": options.get("discovery_query_cap"),
                "platforms_found": ()}
        return InvestigationInfo(id=inv_id, target_type=d.get("target_type", "person"), target_key=g.target_key,
                                 target_names=tuple(d.get("target_names") or ()), locations=tuple(d.get("locations") or ()),
                                 countries=tuple(d.get("countries") or ()), default_region=d.get("default_region"),
                                 seeds={k: tuple(v) for k, v in (d.get("seeds") or {}).items()}, options=opts)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, inv_id: str, *, auto: bool = True) -> Runtime:
        rt = self.runtime(inv_id)
        if rt.loop_task and not rt.loop_task.done():
            return rt
        if not rt.started_at:
            rt.started_at = utcnow()
        rt.auto_expand = rt.auto_expand or auto
        if auto:
            for e in list(rt.graph.entities.values()):
                if e.status == "seed":
                    self._plan(rt, e.key, trigger="auto")
        self._launch(rt)
        return rt

    def _launch(self, rt: Runtime) -> None:
        rt.status, rt.phase, rt.error = "running", "Running transforms", None
        rt.stop_reason = ""
        rt.loop_started = time.monotonic()
        rt.deadline = rt.loop_started + max(0.0, rt.limits.max_minutes * 60 - rt.elapsed_before)
        rt.resume_event.set()
        rt.loop_task = asyncio.create_task(self._loop(rt), name=f"investigation-{rt.id}")
        rt.events.emit("status", self.status_payload(rt))

    async def pause(self, inv_id: str) -> None:
        rt = self.runtime(inv_id)
        if rt.status == "running":
            rt.resume_event.clear()
            rt.status, rt.phase = "paused", "Paused — running transforms finish, nothing new starts"
            rt.events.emit("status", self.status_payload(rt))
            await self._flush(rt)

    async def resume(self, inv_id: str) -> None:
        rt = self.runtime(inv_id)
        if rt.status == "paused" and rt.loop_task and not rt.loop_task.done():
            rt.status, rt.phase = "running", "Running transforms"
            rt.resume_event.set()
            rt.wake.set()
            rt.events.emit("status", self.status_payload(rt))
        elif rt.status in ("paused", "limit_reached", "completed", "cancelled", "failed"):
            await self.continue_(inv_id)

    async def continue_(self, inv_id: str, overrides: dict | None = None) -> Runtime:
        """Resume after a limit, an interruption or completion (optionally with higher limits)."""
        rt = self.runtime(inv_id)
        if rt.loop_task and not rt.loop_task.done():
            return rt
        if overrides:
            rt.limits = resolve_limits(rt.limits.depth if rt.limits.depth != "custom" else "4",
                                       {**rt.limits.to_dict(), **overrides})
        else:
            # same limits again = "another round": the budgets are extended by one more allowance each
            used = int(rt.data.get("requests") or 0)
            base = resolve_limits(rt.limits.depth if rt.limits.depth != "custom" else "2")
            rt.limits = Limits.from_dict({**rt.limits.to_dict(),
                                          "max_minutes": rt.elapsed / 60 + base.max_minutes,
                                          "max_requests": used + base.max_requests,
                                          "max_runs": len(rt.runs) + base.max_runs,
                                          "max_entities": max(rt.limits.max_entities, len(rt.graph.entities) + base.max_entities // 2)})
        rt.auto_expand = True
        for e in list(rt.graph.entities.values()):
            self._plan(rt, e.key, trigger="auto")
        self._launch(rt)
        return rt

    async def stop(self, inv_id: str) -> None:
        rt = self.runtime(inv_id)
        if rt.loop_task and not rt.loop_task.done():
            rt.loop_task.cancel()
            try:
                await rt.loop_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ manual / Maltego-style transforms
    def applicable_transforms(self, inv_id: str, entity_id: str) -> list[dict]:
        rt = self.runtime(inv_id)
        e = rt.graph.by_id(entity_id)
        if e is None:
            raise KeyError(entity_id)
        view = EntityView.of(e)
        out = []
        for t in REG.transforms_for(e.type):
            conn = REG.REGISTRY[t.connector]
            status, note = self._connector_available(rt, conn)
            try:
                applicable = t.accepts(view, rt.info)
            except Exception:  # pragma: no cover - defensive
                applicable = False
            done = (t.name, e.key) in rt.planned
            out.append({**t.spec(), "connector_title": conn.manifest.title, "category": conn.manifest.category,
                        "category_title": conn.manifest.category_title, "status": status, "status_note": note,
                        "applicable": applicable, "already_run": done, "access": conn.manifest.access,
                        "cost": conn.manifest.cost, "sends": t.disclosure(view),
                        "destinations": list(conn.manifest.disclosure.destinations)})
        out.sort(key=lambda d: (d["status"] != "available", not d["applicable"], d["category"], d["title"]))
        return out

    async def run_transforms(self, inv_id: str, entity_id: str, transforms: list[str] | str, *,
                             expand_levels: int = 0) -> dict:
        """Investigator-triggered transforms: a list of names, "all" (every applicable transform) or "auto"
        (what auto-investigation would choose, ignoring the value threshold for this entity)."""
        rt = self.runtime(inv_id)
        e = rt.graph.by_id(entity_id)
        if e is None:
            raise KeyError(entity_id)
        queued, skipped = [], []
        if transforms in ("all", "auto"):
            names = [t.name for t in REG.transforms_for(e.type) if transforms == "all" or t.auto]
        else:
            names = list(transforms)
        if expand_levels:
            # "auto-investigate from here": switch on automatic expansion, bounded to N more levels
            rt.max_depth_override = max(rt.max_depth, e.depth + expand_levels)
            rt.auto_expand = True
        for name in names:
            t = REG.TRANSFORMS.get(name)
            if t is None or e.type not in t.input_types:
                skipped.append({"transform": name, "reason": "not applicable to this entity type"})
                continue
            reason = self._enqueue(rt, t, e.key, trigger="user", force=True)
            (queued if reason is None else skipped).append({"transform": name, "reason": reason})
        if queued and not (rt.loop_task and not rt.loop_task.done()):
            if not rt.started_at:
                rt.started_at = utcnow()
            self._launch(rt)
        rt.wake.set()
        return {"queued": [q["transform"] for q in queued], "skipped": skipped}

    # ------------------------------------------------------------------ planning
    def _connector_available(self, rt: Runtime, conn) -> tuple[str, str]:
        m = conn.manifest
        status, note = REG.connector_status(conn, self.settings)
        if m.requires_subscription and status == "available" and not self.settings.get_bool("allow_paid_sources"):
            return "disabled", "Paid sources are switched off in Settings"
        opts = rt.options
        if m.name in (opts.get("connectors_disabled") or []):
            return "disabled", "Disabled for this investigation"
        cats = opts.get("categories")
        if cats and m.category not in cats:
            return "disabled", "Category not selected for this investigation"
        if status == "disabled" and m.name in (opts.get("connectors_enabled") or []):
            return "available", "Enabled for this investigation"
        if status == "disabled" and self.settings.connector_prefs(m.name).get("enabled") is None:
            # country-specific sources switch on when the investigation points to their country (any country)
            hit = [c for c in m.supported_countries if c != "*" and c in rt.info.countries]
            if hit:
                return "available", f"Pre-selected: the investigation points to {', '.join(hit)}"
            if opts.get("search_everything") and not m.requires_subscription and tuple(m.supported_countries) == ("*",):
                return "available", "Enabled by SEARCH EVERYTHING"
        return status, note

    def _auto_allowed(self, rt: Runtime, conn) -> bool:
        if conn.manifest.name in (rt.options.get("connectors_enabled") or []):
            return True
        if REG.auto_allowed(conn, self.settings):
            return True
        if rt.options.get("search_everything") and not conn.manifest.requires_subscription:
            return True
        return bool(rt.options.get("auto_paid")) and conn.manifest.requires_subscription

    def _plan(self, rt: Runtime, key: str, trigger: str = "auto") -> int:
        e = rt.graph.get(key)
        if e is None or not e.active or not ENTITY_TYPES[e.type].expandable:
            return 0
        if e.status != "seed" and e.depth >= rt.max_depth:
            return 0
        rt.planned_value[e.key] = e.value_score
        n = 0
        for t in REG.transforms_for(e.type):
            if (t.name, e.key) in rt.planned:
                continue
            if self._enqueue(rt, t, e.key, trigger=trigger) is None:
                n += 1
        return n

    def _enqueue(self, rt: Runtime, t: Transform, key: str, *, trigger: str, force: bool = False) -> str | None:
        """Queues (transform, entity) unless a rule says no. Returns None when queued, else the reason."""
        e = rt.graph.get(key)
        if e is None:
            return "entity no longer exists"
        if (t.name, e.key) in rt.planned and not force:
            return "already planned"
        conn = REG.REGISTRY[t.connector]
        status, note = self._connector_available(rt, conn)
        if status != "available":
            if not force:
                rt.coverage.skip(conn.manifest.name, status)
                rt.planned.add((t.name, e.key))       # don't re-evaluate on every replan
            return f"{REG.STATUS_TITLES.get(status, status)}{': ' + note if note else ''}"
        if conn.manifest.name in rt.backed_off and not force:
            rt.coverage.skip(conn.manifest.name, "provider_refused")
            rt.planned.add((t.name, e.key))
            return rt.backed_off[conn.manifest.name]
        view = EntityView.of(e)
        if not force:
            if not t.auto or not (self._auto_allowed(rt, conn) or note.startswith(("Pre-selected", "Enabled by"))):
                rt.coverage.skip(conn.manifest.name, "manual_only_in_auto_mode")
                rt.planned.add((t.name, e.key))
                return "not run automatically"
            if t.seeds_only and e.status != "seed":
                return "input-only transform"
            if e.status != "seed" and not self._approved_hypothesis(rt, e) and not self._discovery_pivot(rt, e) and \
                    e.value_score < max(rt.limits.expand_threshold, t.min_value):
                return "below expansion threshold"            # not marked planned: may qualify later
        try:
            ok = t.accepts(view, rt.info)
        except Exception:
            log.exception("accepts() failed for %s", t.name)
            ok = False
        if not ok:
            rt.planned.add((t.name, e.key))
            return "not applicable"
        routed = self._routed_elsewhere(rt, conn, trigger)
        if routed:
            rt.planned.add((t.name, e.key))
            return routed
        hold = self._budget_hold(rt, t, conn, e, trigger)
        if hold:
            return hold
        over = self._over_cost_cap(rt, conn.manifest)
        if over:
            rt.coverage.skip(conn.manifest.name, "cost_cap")
            rt.planned.add((t.name, e.key))
            return over
        cap = conn.manifest.max_calls_per_investigation or rt.limits.connector_call_cap
        if trigger != "user" and rt.calls[conn.manifest.name] + rt.coverage.queued[conn.manifest.name] >= cap:
            rt.coverage.skip(conn.manifest.name, "connector_cap")
            rt.planned.add((t.name, e.key))
            return f"per-investigation cap of {cap} calls reached"
        depth_factor = 1.0 - min(e.depth, 6) / 7
        priority = (2.0 if trigger == "user" else 0.0) + 0.55 * (1.0 if e.status == "seed" else e.value_score) \
            + 0.3 * t.priority + 0.15 * depth_factor
        task = Task(id=uuid.uuid4().hex[:12], transform=t.name, entity_key=e.key, priority=round(priority, 4),
                    depth=e.depth, reason=f"{'Requested by investigator' if trigger == 'user' else 'Auto'}: "
                                          f"{t.title} on {ENTITY_TYPES[e.type].title.lower()} “{e.label[:60]}”",
                    trigger=trigger)
        rt.tasks[task.id] = task
        rt.planned.add((t.name, e.key))
        rt.queue.push(conn.manifest.name, task)
        rt.coverage.enqueue(conn.manifest.name)
        rt.dirty_tasks.add(task.id)
        rt.events.emit("task", {"id": task.id, "transform": t.name, "entity": e.id, "status": "queued",
                                "priority": task.priority})
        return None

    # ------------------------------------------------------------------ main loop
    async def _loop(self, rt: Runtime) -> None:
        g = rt.graph
        cache = DbCache(self.db, rt.id)
        searx = self.settings.credential("SEARXNG_URL") or ""
        from urllib.parse import urlsplit
        private = frozenset({(urlsplit(searx).hostname or "").lower()}) - {""}
        base_requests = int(rt.data.get("requests") or 0)

        def budget(connector: str) -> None:
            if base_requests + http.total_requests >= rt.limits.max_requests:
                raise FetchError("budget", "The investigation's request budget is exhausted")

        http = self.http_factory(cache=cache, allow_private_hosts=private, budget=budget,
                                 patient=self.settings.get_bool("enhanced_automation"))
        rt.requests_total = lambda: base_requests + http.total_requests  # type: ignore[attr-defined]
        concurrency = self.settings.get_int("max_concurrency", 1, 16)
        active: dict[asyncio.Task, Task] = {}
        last_flush = last_resolve = last_owner = time.monotonic()
        resolve_cost = 0.05
        try:
            while True:
                paused = not rt.resume_event.is_set()
                if paused and not active:
                    await self._flush(rt)
                    await rt.resume_event.wait()
                    continue
                limit = self._limit_hit(rt, base_requests + http.total_requests)
                while not paused and not limit and len(active) < concurrency:
                    picked = rt.queue.pop(lambda c: rt.running[c] < REG.REGISTRY[c].manifest.max_concurrency)
                    if picked is None:
                        break
                    conn_name, task = picked
                    e = g.get(task.entity_key)
                    if e is None or not e.active or task.status != "queued":
                        task.status, task.note = "skipped", "entity ignored, separated or merged away"
                        rt.coverage.drop_queued(conn_name)
                        rt.dirty_tasks.add(task.id)
                        continue
                    rt.running[conn_name] += 1
                    rt.coverage.start(conn_name)
                    task.status = "running"
                    rt.dirty_tasks.add(task.id)
                    active[asyncio.create_task(self._execute(rt, task, http))] = task
                if not active and not paused and (not len(rt.queue) or limit):
                    if rt.needs_resolve:
                        # plan follow-ups on what was just found — even at a limit, so "Continue" can pick them up
                        self._rescore(rt)
                        last_resolve = time.monotonic()
                        if len(rt.queue) and not limit:
                            continue
                    if limit and len(rt.queue):
                        rt.stop_reason = limit
                    break
                rt.wake.clear()
                waiters = set(active) | {asyncio.ensure_future(rt.wake.wait())}
                done, _ = await asyncio.wait(waiters, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                for fut in waiters - set(active):
                    if not fut.done():
                        fut.cancel()
                for fut in done:
                    if fut in active:
                        task = active.pop(fut)
                        conn = REG.TRANSFORMS[task.transform].connector
                        rt.running[conn] = max(0, rt.running[conn] - 1)
                        if fut.exception() is not None:          # _execute catches everything; defensive
                            log.error("task %s crashed: %r", task.id, fut.exception())
                now = time.monotonic()
                if rt.needs_resolve and now - last_resolve >= max(1.0, resolve_cost * 4):
                    t0 = time.monotonic()
                    self._rescore(rt)
                    resolve_cost = time.monotonic() - t0
                    last_resolve = time.monotonic()
                if now - last_owner >= SAME_OWNER_EVERY:
                    if link_same_owner(g):
                        rt.needs_resolve = True
                    last_owner = now
                if now - last_flush >= FLUSH_EVERY:
                    rt.data["requests"] = base_requests + http.total_requests
                    await self._flush(rt)
                    last_flush = now
                await rt.events.notify()
            self._final_scoring(rt)
            rt.status = "limit_reached" if rt.stop_reason else "completed"
            rt.phase = (f"Stopped: {rt.stop_reason}. {len(rt.queue)} transforms still queued — Continue to resume."
                        if rt.stop_reason else "Completed")
        except asyncio.CancelledError:
            rt.status, rt.phase = "cancelled", "Stopped by the investigator — partial results kept"
            raise
        except Exception as exc:
            log.exception("Investigation %s failed", rt.id)
            rt.status, rt.error = "failed", f"Internal error: {exc.__class__.__name__}"
            rt.phase = "Failed — partial results kept"
        finally:
            # stop in-flight transforms on every exit path: left running, they outlive the HTTP client closed below
            # and keep changing the graph after the final flush
            for fut in active:
                fut.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            rt.running.clear()
            rt.elapsed_before = rt.elapsed
            rt.loop_started = 0.0
            rt.finished_at = utcnow()
            rt.data["requests"] = base_requests + http.total_requests
            try:
                await http.aclose()
            except Exception:  # pragma: no cover
                pass
            for t in rt.tasks.values():
                if t.status == "running":
                    t.status = "queued"
                    rt.dirty_tasks.add(t.id)
            await self._flush(rt, final=True)
            rt.events.emit("status", self.status_payload(rt))
            await rt.events.notify()

    def _limit_hit(self, rt: Runtime, requests: int) -> str:
        if time.monotonic() >= rt.deadline:
            return f"time limit of {rt.limits.max_minutes:g} minutes reached"
        if len(rt.graph.entities) >= rt.limits.max_entities:
            return f"maximum of {rt.limits.max_entities} entities reached"
        if requests >= rt.limits.max_requests:
            return f"request budget of {rt.limits.max_requests} reached"
        if len(rt.runs) >= rt.limits.max_runs:
            return f"maximum of {rt.limits.max_runs} transform runs reached"
        return ""

    def _final_scoring(self, rt: Runtime) -> None:
        link_same_owner(rt.graph)
        for key in resolve(rt.graph):
            e = rt.graph.entities.get(key)
            if e is not None:
                rt.events.emit("score", e.summary())
        rt.needs_resolve = False

    def _user_platforms(self) -> tuple:
        """The investigator's own platforms: profile links to them are accounts, like links to built-in platforms."""
        return store_for(self.db).recent().platform_objects

    def _rescore(self, rt: Runtime) -> None:
        g = rt.graph
        rt.info.options["platforms_found"] = tuple(sorted({str(a.get("platform")) for a in g.of_type("account")
                                                           if a.active and a.get("platform")}))
        with user_platforms_in_scope(self._user_platforms()):
            changed = resolve(g)
        rt.needs_resolve = False
        for key in changed:
            e = g.entities.get(key)
            if e is not None:
                rt.events.emit("score", e.summary())
        if rt.status == "running" and rt.auto_expand:
            for e in list(g.entities.values()):
                last = rt.planned_value.get(e.key)
                if e.active and (last is None or e.value_score >= last + 0.1 or e.status == "confirmed"):
                    if e.status == "seed" or e.value_score >= rt.limits.expand_threshold or e.status == "confirmed" \
                            or self._approved_hypothesis(rt, e) or self._discovery_pivot(rt, e, claim=True):
                        self._plan(rt, e.key)

    # ------------------------------------------------------------------ one transform run
    async def _execute(self, rt: Runtime, task: Task, http: SafeHttpClient) -> None:
        g = rt.graph
        t = REG.TRANSFORMS[task.transform]
        conn = REG.REGISTRY[t.connector]
        m = conn.manifest
        e = g.get(task.entity_key)
        view = EntityView.of(e)
        run = TransformRun(id=uuid.uuid4().hex[:12], task_id=task.id, transform=t.name, connector=m.name,
                           input_key=e.key, input_label=e.label[:200], sent=t.disclosure(view),
                           destinations=list(m.disclosure.destinations), depth=e.depth, trigger=task.trigger)
        task.run_id = run.id
        rt.runs[run.id] = run
        rt.calls[m.name] += 1
        rt.dirty_runs.add(run.id)
        rt.events.emit("run", run.to_dict())
        out = TransformResult(t.name, m.name, e.key, e.type, view.display)
        ctx = TransformContext(transform=t.name, manifest=m, entity=view, info=rt.info, http=http,
                               limiter=self._limiter(m),
                               credentials=self.settings.credential, settings=self.settings.get, out=out, run_id=run.id,
                               data_dir=self.data_dir, counters=rt.counters,
                               journal=rt.data.setdefault("search_journal", []))
        before = http.requests.get(m.name, 0)
        status, message = "", ""
        challenge: str | None = None
        try:
            with user_platforms_in_scope(self._user_platforms()):
                await asyncio.wait_for(t.transform(view, ctx), timeout=m.timeout)
        except TransformError as exc:
            status, message = exc.status, exc.message or str(exc)
            if isinstance(exc, VerificationRequired):
                challenge = exc.url or m.website
        except FetchError as exc:
            status, message = FETCH_STATUS.get(exc.kind, "error"), str(exc)
        except asyncio.TimeoutError:
            status, message = "timeout", f"No answer within {m.timeout:g} s"
        except asyncio.CancelledError:
            run.status, run.message, run.finished_at = "cancelled", "Stopped", utcnow()
            rt.coverage.finish(m.name, "cancelled")
            task.status = "queued"
            rt.dirty_tasks.add(task.id)
            rt.dirty_runs.add(run.id)
            raise
        except Exception as exc:
            log.exception("Transform %s failed", t.name)
            status, message = "error", f"Connector error: {exc.__class__.__name__}"
        try:
            for digest, rec in ctx.raw_evidence().items():
                rt.pending_raw[digest] = {"url": rec.url, "method": rec.method, "status": rec.status,
                                          "content_type": rec.content_type, "size": rec.size, "retrieved_at": rec.at,
                                          "connector": m.name, "body": rec.body or b""}
        except Exception:  # evidence capture must never break a run
            log.exception("Raw evidence capture failed for %s", t.name)
        try:
            stats = merge_result(g, out, run=run, manifest=m, input_depth=e.depth)
        except Exception as exc:  # a malformed result must not kill the investigation
            log.exception("Merging the result of %s failed", t.name)
            stats = None
            status, message = "error", f"Invalid connector result: {exc.__class__.__name__}"
        if not status:
            status = out.status or ("found" if out.found_anything else "empty")
            message = out.message
        elif out.message and not message:
            message = out.message
        run.status, run.message, run.finished_at = status, message[:500], utcnow()
        run.requests = http.requests.get(m.name, 0) - before
        if stats is not None:
            run.new_entities = len(stats.new_entities)
            run.new_relationships = len(stats.new_relationships)
            for key in stats.new_entities:
                ne = g.entities.get(key)
                if ne is not None:
                    rt.events.emit("entity", ne.summary())
            for rk in stats.new_relationships:
                r = g.relationships.get(rk)
                if r is not None:
                    rt.events.emit("relationship", self.rel_summary(g, r))
            if stats.touched_entities or stats.new_relationships:
                rt.needs_resolve = True
        task.status = "done"
        rt.dirty_tasks.add(task.id)
        rt.dirty_runs.add(run.id)
        status, message, challenge = ctx.verification_outcome(status, message, challenge)
        run.status, run.message = status, message[:500]
        if challenge:
            self._verification_required(rt, m, e, challenge, message)
        self._search_failover(rt, t, conn, e, status, message, task.trigger)
        rt.coverage.finish(m.name, status)
        self._track_refusals(rt, m, status)
        rt.pending_health.append((m.name, status, run.finished_at))
        rt.events.emit("run", run.to_dict())
        rt.wake.set()

    @staticmethod
    def tried(rt: Runtime, transform: str, key: str) -> bool:
        """Whether this transform actually ran or is queued on this entity (``planned`` also holds pairs it skipped)."""
        return any(t.transform == transform and t.entity_key == key and t.status in ("queued", "running", "done")
                   for t in rt.tasks.values())

    @staticmethod
    def _approved_hypothesis(rt: Runtime, e) -> bool:
        """A name-derived handle the investigator approved for testing: checked despite its low value (it has none)."""
        return e.type == "username" and bool(e.get("hypothesis")) and bool(rt.options.get("test_hypotheses"))

    def _limiter(self, m):
        """Per-source token bucket; enhanced automation paces every source at half its published rate."""
        if self.settings.get_bool("enhanced_automation"):
            return limiter_for(f"{m.name}:patient", m.rate_limit.per_second / 2, max(1, m.rate_limit.burst // 2))
        return limiter_for(m.name, m.rate_limit.per_second, m.rate_limit.burst)

    # ------------------------------------------------------------------ search routing, discovery pivots, budget
    def search_providers(self, rt: Runtime) -> list[str]:
        """Available web-search providers in routing order (discovery/router.py) for this investigation."""
        return [p for p in ROUTER.available(self.settings)
                if self._connector_available(rt, REG.REGISTRY[p])[0] == "available"]

    def search_primary(self, rt: Runtime) -> str | None:
        return ROUTER.primary(self.search_providers(rt), rt.data.get("search_failed") or [])

    def _routed_elsewhere(self, rt: Runtime, conn, trigger: str) -> str | None:
        """Automatic search runs go to one provider (the primary), not to every configured one."""
        if trigger in ("user", "failover") or not ROUTER.is_search_provider(conn):
            return None
        if str(self.settings.get("search_provider_mode") or "primary") == "all" or rt.options.get("search_everything"):
            return None
        primary = self.search_primary(rt)
        if primary and primary != conn.manifest.name:
            rt.coverage.skip(conn.manifest.name, "routed")
            return f"Searches go to the primary provider ({REG.REGISTRY[primary].manifest.title})"
        return None

    def _search_failover(self, rt: Runtime, t, conn, e, status: str, message: str, trigger: str) -> None:
        """When the primary search provider fails a run, the same search runs on the next available provider."""
        if not ROUTER.is_search_provider(conn) or status not in ROUTER.FAILOVER_STATUSES:
            return
        failed = rt.data.setdefault("search_failed", [])
        if conn.manifest.name not in failed:
            failed.append(conn.manifest.name)
        nxt = ROUTER.primary(self.search_providers(rt), failed)
        method = t.name.split(".", 1)[1]
        rt.data.setdefault("search_failovers", []).append(
            {"at": utcnow(), "from": conn.manifest.name, "to": nxt, "transform": method, "status": status,
             "message": message[:200]})
        if nxt:
            nt = REG.TRANSFORMS.get(f"{nxt}.{method}")
            if nt is not None and e.active:
                self._enqueue(rt, nt, e.key, trigger="failover", force=True)

    def _discovery_pivot(self, rt: Runtime, e, claim: bool = False) -> bool:
        """A handle discovered in a search for the target's name (and echoed by the result) is searched across the
        other platforms even though it has no identity evidence yet: that is how an unguessable handle is followed.
        Bounded per investigation (setting discovery_pivot_cap)."""
        if e.type != "username" or not e.get("pivot") or e.get("hypothesis") or not e.active or e.depth >= rt.max_depth:
            return False
        pivots = rt.data.setdefault("discovery_pivots", [])
        if e.key in pivots:
            return True
        if not claim:
            return False
        cap = self.settings.get_int("discovery_pivot_cap", 0, 50)
        if len(pivots) >= cap:
            return False
        pivots.append(e.key)
        return True

    def _budget_hold(self, rt: Runtime, t, conn, e, trigger: str) -> str | None:
        """The investigation budget: a billable run that would push the estimated cost past the limit is held for the
        investigator's decision (Continue raises the limit, Stop keeps it)."""
        from ..costs import ledger as LEDGER
        if ROUTER.is_search_provider(conn) or conn.manifest.cost_per_call_usd or conn.manifest.access in ("metered", "paid", "enterprise"):
            add = LEDGER.task_estimate(t, conn, self.settings)
            hold = LEDGER.check(rt, self.db, self.settings, add)
            if hold is None:
                return None
            h = rt.data.setdefault("budget_hold", {**hold, "at": utcnow(), "pending": []})
            h.update({k: v for k, v in hold.items() if k in ("after_usd", "limit_usd", "reason")})
            item = [t.name, e.key]
            if item not in h["pending"] and len(h["pending"]) < 500:
                h["pending"].append(item)
            rt.coverage.skip(conn.manifest.name, "cost_budget")
            rt.events.emit("status", self.status_payload(rt))
            sym = "≈$"
            return (f"Held for your decision: this may raise the estimated cost from {sym}{hold['current_usd']:.4f} to "
                    f"{sym}{hold['after_usd']:.4f} (limit {sym}{hold['limit_usd']:.4f})")
        return None

    async def resolve_budget_hold(self, inv_id: str, *, proceed: bool, new_budget: float | None = None) -> dict:
        """Continue: raise the limit (to ``new_budget`` or just enough for the held work) and queue the held work.
        Stop: drop the held work and keep the limit."""
        rt = self.runtime(inv_id)
        hold = rt.data.pop("budget_hold", None)
        if not hold:
            return {"queued": [], "message": "Nothing is waiting for a budget decision"}
        if not proceed:
            return {"queued": [], "message": "Kept the limit; the held work was not run", "dropped": len(hold["pending"])}
        if hold.get("reason") == "ask_before":
            rt.options["confirmed_above"] = True
        if new_budget is not None:
            rt.options["budget_usd"] = max(0.0, float(new_budget))
        elif hold.get("reason") == "budget":
            rt.options["budget_usd"] = None
        rt.data.setdefault("options_changes", []).append({"at": utcnow(), "budget_usd": rt.options.get("budget_usd")})
        queued = []
        for name, key in hold["pending"]:
            tr = REG.TRANSFORMS.get(name)
            if tr is not None and rt.graph.get(key) is not None and self._enqueue(rt, tr, key, trigger="auto", force=True) is None:
                queued.append(name)
        if queued and not (rt.loop_task and not rt.loop_task.done()):
            self._launch(rt)
        rt.wake.set()
        return {"queued": queued, "budget_usd": rt.options.get("budget_usd")}

    def paid_spend(self, rt: Runtime) -> float:
        """Estimated cost of the paid calls made or queued in this investigation (calls × published price per call)."""
        total = 0.0
        for name in set(rt.calls) | {n for n, q in rt.coverage.queued.items() if q}:
            conn = REG.REGISTRY.get(name)
            if conn is not None and conn.manifest.cost_per_call_usd:
                total += (rt.calls[name] + rt.coverage.queued.get(name, 0)) * conn.manifest.cost_per_call_usd
        return round(total, 4)

    def _over_cost_cap(self, rt: Runtime, m) -> str | None:
        cap = self.settings.get_float("max_paid_cost_usd", 0.0, 1_000_000.0)
        if not cap or not m.requires_subscription or not m.cost_per_call_usd:
            return None
        if self.paid_spend(rt) + m.cost_per_call_usd > cap:
            return f"Paid-cost limit of ${cap:g} per investigation reached (Settings)"
        return None

    def _verification_required(self, rt: Runtime, m, e, url: str, message: str) -> None:
        """Remembers the page that asked for a human, so the investigator can open it, mark the source manual-only, or
        try another configured source. With enhanced automation the other sources are tried at once."""
        rt.data.setdefault("verification", {})[m.name] = {
            "connector": m.name, "title": m.title, "url": url, "at": utcnow(), "entity": e.id,
            "entity_label": e.label[:120], "category": m.category, "message": message[:300]}
        if self.settings.get_bool("enhanced_automation"):
            self.fallback(rt, m.name, e.key)

    async def test_hypotheses(self, inv_id: str, count: int) -> dict:
        """The investigator approved testing name-derived handles: generate up to ``count`` and check them, one level
        deep. Accounts found this way start with no identity evidence."""
        rt = self.runtime(inv_id)
        t = rt.graph.target
        if t is None or t.type != "person":
            raise ValueError("Name-derived handles need a person with a full name")
        rt.options.update(test_hypotheses=True, username_guess_count=count)
        rt.info.options.update(username_guesses=True, username_guess_count=count)
        return await self.run_transforms(inv_id, t.id, ["username_guesses.generate"], expand_levels=2)

    async def run_fallback(self, inv_id: str, connector: str, entity_id: str) -> dict:
        """"Try another configured source": the same lookup through the other free sources of the category."""
        rt = self.runtime(inv_id)
        e = rt.graph.by_id(entity_id)
        if e is None or connector not in REG.REGISTRY:
            raise KeyError(entity_id)
        queued = self.fallback(rt, connector, e.key)
        if queued and not (rt.loop_task and not rt.loop_task.done()):
            if not rt.started_at:
                rt.started_at = utcnow()
            self._launch(rt)
        return {"queued": queued}

    def fallback(self, rt: Runtime, connector: str, key: str) -> list[str]:
        """Queues the same lookup through the other free, available sources of the same category that have not run on
        this entity yet. Never a paid source, never the refusing one."""
        src = REG.REGISTRY[connector].manifest
        e = rt.graph.get(key)
        if e is None:
            return []
        queued = []
        for t in REG.transforms_for(e.type):
            conn = REG.REGISTRY[t.connector]
            m = conn.manifest
            if m.name == connector or m.category != src.category or m.requires_subscription or self.tried(rt, t.name, key):
                continue
            try:
                if not t.accepts(EntityView.of(e), rt.info):
                    continue
            except Exception:  # pragma: no cover - defensive
                continue
            if self._enqueue(rt, t, key, trigger="fallback", force=True) is None:
                queued.append(t.name)
        if queued:
            rt.wake.set()
        return queued

    @staticmethod
    def _track_refusals(rt: Runtime, m, status: str) -> None:
        """Respect a provider that refuses: after REFUSALS_BEFORE_BACKOFF consecutive blocked or rate-limited runs, its
        queued tasks are skipped and it gets no further automatic requests in this investigation, with the reason shown
        in the coverage view. The investigator can still run one of its transforms by hand; a success lifts the stop."""
        name = m.name
        if status in REFUSAL_STATUSES:
            rt.refusals[name] += 1
        elif status in ("found", "empty", "not_found"):
            rt.refusals[name] = 0
            rt.backed_off.pop(name, None)
            if rt.coverage.notes.get(name):
                rt.coverage.note(name, "")
            return
        if rt.refusals[name] < REFUSALS_BEFORE_BACKOFF or name in rt.backed_off:
            return
        why = (f"{m.title} {'rate-limited' if status == 'rate_limited' else 'refused'} {rt.refusals[name]} requests in a "
               "row, so Sextant stopped sending it requests in this investigation (nothing is retried around a refusal)")
        rt.backed_off[name] = why
        rt.coverage.note(name, why)
        for task in rt.queue.remove(name):
            task.status, task.note = "skipped", why
            rt.coverage.drop_queued(name)
            rt.coverage.skip(name, "provider_refused")
            rt.dirty_tasks.add(task.id)
        log.info("Backing off from %s: %s", name, why)

    # ------------------------------------------------------------------ persistence & views
    async def _flush(self, rt: Runtime, final: bool = False) -> None:
        # Everything the write needs is read and encoded here, on the event loop; the worker thread only runs SQL.
        # Transforms and investigator actions keep changing the graph meanwhile, so it must never be read from there.
        # The lock keeps overlapping flushes (loop, pause, user edits) from landing out of order.
        async with rt.flush_lock:
            g = rt.graph
            rt.data.update({
                "aliases": g.aliases, "merge_log": g.merge_log, "target_key": g.target_key, "stop_reason": rt.stop_reason,
                "elapsed": rt.elapsed, "started_at": rt.started_at, "finished_at": rt.finished_at, "error": rt.error,
                "coverage_raw": rt.coverage.to_dict(), "stats": g.stats(), "limits": rt.limits.to_dict(),
                "max_depth_override": rt.max_depth_override, "counters": rt.counters, "auto_expand": rt.auto_expand,
            })
            if final:
                rt.data["coverage"] = self.coverage(rt.id)
            runs = [rt.runs[i] for i in rt.dirty_runs if i in rt.runs]
            tasks = [rt.tasks[i] for i in rt.dirty_tasks if i in rt.tasks]
            rt.dirty_runs.clear()
            rt.dirty_tasks.clear()
            status = rt.status
            referenced = {r for src in g.sources.values() for r in src.raw_refs}
            raw = {d: v for d, v in rt.pending_raw.items() if d in referenced}
            rt.pending_raw = {}
            health, rt.pending_health = rt.pending_health, []
            pending = self.db.prepare_graph(rt.id, g)
            data_enc = self.db.enc(rt.data)

            def write() -> None:
                if pending is not None:
                    self.db.write_graph(pending)
                self.db.save_runs(rt.id, runs)
                self.db.save_tasks(rt.id, tasks)
                self.db.update_investigation(rt.id, status=status, data_enc=data_enc)
                if raw:
                    self.db.save_raw(rt.id, raw)
                if health:
                    self.db.record_health(health)

            job = asyncio.ensure_future(asyncio.to_thread(write))
            try:
                await asyncio.shield(job)
            finally:
                if not job.done():          # cancelled mid-write: the thread can't be stopped, so hold the lock till it ends
                    await asyncio.wait([job])
                if job.cancelled() or job.exception() is not None:
                    if pending is not None:
                        g.dirty.absorb(pending.dirty)
                    rt.dirty_runs.update(r.id for r in runs)
                    rt.dirty_tasks.update(t.id for t in tasks)
        rt.events.emit("stats", {"stats": g.stats(), "status": self.status_payload(rt)})

    def status_payload(self, rt: Runtime) -> dict:
        req = rt.requests_total() if rt.loop_started and hasattr(rt, "requests_total") else int(rt.data.get("requests") or 0)
        return {"id": rt.id, "status": rt.status, "phase": rt.phase, "error": rt.error, "stop_reason": rt.stop_reason,
                "queued": len(rt.queue), "running": sum(rt.running.values()), "runs": len(rt.runs),
                "requests": req, "elapsed": round(rt.elapsed, 1), "limits": rt.limits.to_dict(),
                "max_depth": rt.max_depth, "stats": rt.graph.stats()}

    def coverage(self, inv_id: str) -> dict:
        rt = self.runtime(inv_id)
        rep = rt.coverage.report(self.settings, {e.type for e in rt.graph.entities.values()})
        try:
            rep["global"] = global_coverage(rt, self.settings, rep["connectors"], self.db.health_records(),
                                            store_for(self.db).snapshot())
        except Exception:  # the global block is a summary; never break the coverage view
            log.exception("Global coverage summary failed")
        return rep

    @staticmethod
    def rel_summary(g: Graph, r) -> dict:
        s, t = g.entities.get(r.source), g.entities.get(r.target)
        return {"id": r.id, "source": s.id if s else "", "target": t.id if t else "", "type": r.type,
                "confidence": round(r.confidence, 3), "status": r.status, "evidence": len(r.evidence)}

    # ------------------------------------------------------------------ investigator decisions
    async def decide(self, inv_id: str, entity_id: str, action: str, note: str = "") -> dict:
        """CONFIRM (pertains to the target) · SEPARATE (a different person/entity) · IGNORE (irrelevant) · RESET."""
        rt = self.runtime(inv_id)
        e = rt.graph.by_id(entity_id)
        if e is None:
            raise KeyError(entity_id)
        status = {"confirm": "confirmed", "separate": "separated", "ignore": "ignored", "reset": "candidate"}.get(action)
        if status is None:
            raise ValueError("action must be confirm, separate, ignore or reset")
        rt.graph.set_status(e.key, status)
        if action != "confirm" and rt.graph.target_key:
            for r in rt.graph.in_edges(e.key):
                if r.source == rt.graph.target_key and any(ev.source == USER_SOURCE_ID and "Confirmed" in ev.description
                                                           for ev in r.evidence):
                    r.status = "rejected"
                    rt.graph.dirty.relationships.add(r.key)
        if action == "confirm" and rt.graph.target_key and e.type in ("account", "domain", "website", "email", "phone"):
            rt.graph.add_relationship(rt.graph.target_key, e.key, "OWNS" if e.type in ("account", "domain", "website") else "USES",
                                      RelEvidence(USER_SOURCE_ID, "Confirmed by the investigator", "", 1.0))
        self.db.log_decision(inv_id, action, {"entity": e.key, "label": e.label, "note": note[:500]})
        return await self._after_user_change(rt, [e.key])

    async def merge(self, inv_id: str, keep_id: str, other_id: str, note: str = "") -> dict:
        rt = self.runtime(inv_id)
        keep, other = rt.graph.by_id(keep_id), rt.graph.by_id(other_id)
        if keep is None or other is None:
            raise KeyError("entity")
        merged_key = other.key
        kept = rt.graph.merge(keep.key, other.key, note)
        self.db.log_decision(inv_id, "merge", {"keep": kept.key, "merged": merged_key, "note": note[:500]})
        return await self._after_user_change(rt, [kept.key])

    async def unmerge(self, inv_id: str, entity_id: str, merged_key: str) -> dict:
        rt = self.runtime(inv_id)
        restored = rt.graph.separate(merged_key)
        self.db.log_decision(inv_id, "unmerge", {"restored": restored.key})
        return await self._after_user_change(rt, [restored.key])

    async def reject_relationship(self, inv_id: str, rel_id: str) -> dict:
        rt = self.runtime(inv_id)
        rel = rt.graph.reject_relationship(rel_id)
        self.db.log_decision(inv_id, "unlink", {"relationship": rel.key})
        return await self._after_user_change(rt, [])

    async def add_entity(self, inv_id: str, type_id: str, value: str, label: str = "", link_from: str | None = None,
                         rel_type: str = "ASSOCIATED_WITH") -> dict:
        rt = self.runtime(inv_id)
        norm = normalize_value(type_id, value)
        if not norm:
            raise ValueError(f"“{value}” is not a valid {ENTITY_TYPES[type_id].title.lower()}")
        e, _ = rt.graph.ensure_entity(type_id, norm, label or value, depth=0, status="seed")
        rt.graph.add_observation(Observation(id=short_hash(e.key, "@", USER_SOURCE_ID), entity=e.key, prop="@",
                                             value=label or value, source=USER_SOURCE_ID, evidence="Added by the investigator"))
        if link_from:
            src = rt.graph.by_id(link_from)
            if src is not None:
                rt.graph.add_relationship(src.key, e.key, rel_type, RelEvidence(USER_SOURCE_ID, "Added by the investigator"))
        self.db.log_decision(inv_id, "add_entity", {"entity": e.key})
        await self._after_user_change(rt, [e.key])
        return e.summary()

    async def _after_user_change(self, rt: Runtime, keys: list[str]) -> dict:
        self._rescore(rt)
        if rt.status == "running" and rt.auto_expand:
            for k in keys:
                self._plan(rt, k)
        for k in keys:
            e = rt.graph.get(k)
            if e is not None:
                rt.events.emit("entity", e.summary())
        rt.events.emit("graph_changed", {})
        await self._flush(rt)
        await rt.events.notify()
        return {"ok": True, "stats": rt.graph.stats()}

    async def delete(self, inv_id: str) -> None:
        if inv_id in self.active:
            await self.stop(inv_id)
            self.active.pop(inv_id, None)
        await asyncio.to_thread(self.db.delete_investigation, inv_id)
