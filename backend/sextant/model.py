"""Evidence model and investigation graph.

Provenance chain (never broken):

    Entity ──props──▶ PropValue ──obs ids──▶ Observation ──source──▶ Source ──url──▶ original URL
    Relationship ──evidence──▶ RelEvidence ──source──▶ Source

* **Observations are the source of truth.** An entity's properties are an aggregate of every
  observation about it (and of entities merged into it). Merging is therefore reversible: separating
  rebuilds both entities from their own observations.
* Every entity has at least one observation, the *sighting* (property ``@``), recording which source
  reported it and why.
* Automatic deduplication only happens on identical identity keys (``type:normalized value``).
  Anything that is merely *probably* the same stays separate and is linked by entity resolution
  (``SAME_AS`` with confidence and evidence), or merged by the investigator.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .ontology import ENTITY_TYPES, RELATION_TYPES, make_key, split_key

SIGHTING = "@"

# dated properties: they drive the timeline and the graph's year filter
DATE_PROPS = {"created_at": "Account created", "registered": "Domain registered", "expires": "Domain expires",
              "first_certificate": "First TLS certificate", "breach_date": "Breach", "date": "Record date",
              "published": "Published", "incorporated": "Incorporated", "dissolved": "Dissolved",
              "archive_first_capture": "First archived capture", "archive_last_capture": "Last archived capture",
              "birth_date": "Date of birth (as published)"}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def short_hash(*parts: Any, n: int = 20) -> str:
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return h[:n]


def vkey(value: Any) -> str:
    """Canonical string of a property value, used to merge equal values from different sources."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------- provenance

@dataclass
class Source:
    """One place information was retrieved from (an API response, a web page, a dataset, a local analysis)."""

    id: str
    connector: str
    kind: str                       # api | web_page | dataset | local | user | tool
    url: str | None = None
    title: str = ""
    transform: str = ""
    retrieved_at: str = field(default_factory=utcnow)
    last_retrieved: str = ""
    reliability: str = "medium"     # official | high | medium | low  (from the connector manifest)
    license: str | None = None
    request: str = ""               # what was sent (e.g. "username 'jdoe'"), shown in the provenance view
    raw_refs: list[str] = field(default_factory=list)   # sha256 digests of the raw responses (stored encrypted)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        s = cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})
        s.raw_refs = list(s.raw_refs or [])
        return s


@dataclass
class Observation:
    """A single fact: <entity>.<prop> = <value>, according to <source>."""

    id: str
    entity: str                     # the entity key the source reported (kept even after merges)
    prop: str
    value: Any
    source: str                     # Source.id
    run: str = ""                   # TransformRun.id ("" for user input)
    observed_at: str = field(default_factory=utcnow)
    confidence: float = 1.0         # how reliably the value was extracted (1.0 = structured API field)
    evidence: str = ""              # human-readable: where in the source this came from

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class RelEvidence:
    source: str                     # Source.id
    description: str
    run: str = ""
    confidence: float = 1.0
    observed_at: str = field(default_factory=utcnow)
    origin: str = ""                # original relationship key (used to undo merges)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RelEvidence":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Reason:
    """One item of an identity assessment: supporting (+), contradicting (−) or informational (0)."""

    description: str
    weight: float = 0.0
    feature: str = ""               # comparator id, e.g. "email_exact"
    sources: list[str] = field(default_factory=list)   # Source ids that back the compared values

    @property
    def kind(self) -> str:
        return "support" if self.weight > 0 else "contradiction" if self.weight < 0 else "info"

    def to_dict(self) -> dict:
        return {"description": self.description, "weight": round(self.weight, 2), "feature": self.feature,
                "kind": self.kind, "sources": self.sources}

    @classmethod
    def from_dict(cls, d: dict) -> "Reason":
        return cls(d.get("description", ""), float(d.get("weight", 0.0)), d.get("feature", ""), list(d.get("sources") or []))


# --------------------------------------------------------------------------- graph elements

ENTITY_STATUSES = ("seed", "candidate", "confirmed", "separated", "ignored")


@dataclass
class Entity:
    type: str
    value: str                      # normalized identity value
    label: str = ""
    id: str = ""
    status: str = "candidate"       # seed | candidate | confirmed | separated | ignored
    depth: int = 0
    first_seen: str = field(default_factory=utcnow)
    last_seen: str = ""
    discovered_by: str = ""         # TransformRun.id that first reported it
    # aggregated from observations: prop -> [{"v": value, "obs": [ids], "conf": float}]
    props: dict[str, list[dict]] = field(default_factory=dict)
    # resolution / ranking output (recomputed)
    confidence: float = 0.0         # P(pertains to the target)
    tier: str = ""                  # high | medium | low
    value_score: float = 0.0        # investigative value — drives expansion priority
    match_label: str = ""
    reasons: list[Reason] = field(default_factory=list)

    @property
    def key(self) -> str:
        return make_key(self.type, self.value)

    @property
    def locked(self) -> bool:
        return self.status in ("seed", "confirmed", "separated", "ignored")

    @property
    def active(self) -> bool:
        return self.status not in ("ignored", "separated")

    def get(self, prop: str, default: Any = None) -> Any:
        vals = self.props.get(prop)
        return vals[0]["v"] if vals else default

    def values(self, prop: str) -> list[Any]:
        return [pv["v"] for pv in self.props.get(prop, [])]

    def all_list(self, prop: str) -> list[Any]:
        """Flattened values for list-valued properties (several sources may each report a list)."""
        out: list[Any] = []
        for v in self.values(prop):
            for item in v if isinstance(v, list) else [v]:
                if item not in out:
                    out.append(item)
        return out

    def years(self) -> list[int] | None:
        """[earliest, latest] year among the entity's dated facts (account created, domain registered, record date…)."""
        ys = [int(str(pv["v"])[:4]) for prop in DATE_PROPS if prop != "birth_date" for pv in self.props.get(prop, ())
              if str(pv["v"])[:4].isdigit() and 1800 < int(str(pv["v"])[:4]) < 2200]
        if self.type == "publication" and str(self.get("year") or "")[:4].isdigit():
            ys.append(int(str(self.get("year"))[:4]))
        return [min(ys), max(ys)] if ys else None

    def summary(self) -> dict:
        return {"id": self.id, "key": self.key, "type": self.type, "value": self.value, "label": self.label or self.value,
                "status": self.status, "depth": self.depth, "confidence": round(self.confidence, 3), "tier": self.tier,
                "value_score": round(self.value_score, 3), "match_label": self.match_label, "years": self.years()}


@dataclass
class Relationship:
    source: str                     # entity key
    target: str                     # entity key
    type: str
    confidence: float = 1.0         # confidence that this link is real (not identity)
    evidence: list[RelEvidence] = field(default_factory=list)
    status: str = "active"          # active | rejected | confirmed
    props: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    first_seen: str = field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.source}|{self.type}|{self.target}"

    def add_evidence(self, ev: RelEvidence) -> bool:
        for cur in self.evidence:
            if cur.source == ev.source and cur.description == ev.description and cur.origin == ev.origin:
                return False
        self.evidence.append(ev)
        self.confidence = max(self.confidence, ev.confidence) if self.evidence[:-1] else ev.confidence
        return True


@dataclass
class Check:
    """Outcome of asking one site/platform about one identifier — the "does it exist there?" question,
    kept separate from "is it the target?"."""

    connector: str
    transform: str
    platform: str
    identifier_type: str
    identifier: str
    status: str                     # found | not_found | blocked | unknown | skipped | error
    detail: str = ""
    url: str | None = None
    account_key: str | None = None
    platform_title: str = ""
    category: str = ""
    checked_at: str = field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.connector}|{self.platform}|{self.identifier_type}|{self.identifier.casefold()}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Check":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


RUN_STATUSES = ("queued", "running", "found", "empty", "not_found", "blocked", "rate_limited", "needs_credentials",
                "error", "timeout", "cancelled", "budget_exhausted", "skipped")


@dataclass
class TransformRun:
    id: str
    task_id: str
    transform: str
    connector: str
    input_key: str
    input_label: str = ""
    status: str = "running"
    message: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str | None = None
    requests: int = 0
    new_entities: int = 0
    new_relationships: int = 0
    sent: str = ""                  # exactly what was disclosed to whom
    destinations: list[str] = field(default_factory=list)
    depth: int = 0
    trigger: str = "auto"           # auto | user

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TransformRun":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Task:
    id: str
    transform: str
    entity_key: str
    priority: float
    depth: int
    reason: str = ""
    status: str = "queued"          # queued | running | done | skipped | cancelled
    trigger: str = "auto"
    created_at: str = field(default_factory=utcnow)
    run_id: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


# --------------------------------------------------------------------------- graph

IdFn = Callable[[str], str]


def default_id(key: str) -> str:
    return short_hash(key, n=16)


class Dirty:
    """What changed since the last flush to the database (incremental persistence)."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.entities: set[str] = set()
        self.deleted_entities: set[str] = set()
        self.relationships: set[str] = set()
        self.deleted_relationships: set[str] = set()
        self.observations: set[str] = set()
        self.sources: set[str] = set()
        self.checks: set[str] = set()
        self.meta = False

    def __bool__(self) -> bool:
        return bool(self.entities or self.deleted_entities or self.relationships or self.deleted_relationships
                    or self.observations or self.sources or self.checks or self.meta)

    def absorb(self, older: "Dirty") -> None:
        """Takes back the marks of a flush that failed. A flush applies deletions before upserts and skips keys
        no longer in the graph, so a key marked both ways ends up in whatever state the graph now holds."""
        for name in ("entities", "deleted_entities", "relationships", "deleted_relationships", "observations",
                     "sources", "checks"):
            getattr(self, name).update(getattr(older, name))
        self.meta = self.meta or older.meta


class Graph:
    def __init__(self, id_fn: IdFn = default_id) -> None:
        self.id_fn = id_fn
        self.entities: dict[str, Entity] = {}
        self.relationships: dict[str, Relationship] = {}
        self.observations: dict[str, Observation] = {}
        self.sources: dict[str, Source] = {}
        self.checks: dict[str, Check] = {}
        self.aliases: dict[str, str] = {}             # merged key -> surviving key
        self.merge_log: list[dict] = []               # undo information for investigator merges
        self.target_key: str | None = None
        self._out: dict[str, set[str]] = defaultdict(set)
        self._in: dict[str, set[str]] = defaultdict(set)
        self._obs_by_entity: dict[str, list[str]] = defaultdict(list)   # ORIGINAL entity key -> observation ids
        self._by_id: dict[str, str] = {}
        self.dirty = Dirty()

    # ------------------------------------------------------------------ lookup
    def resolve(self, key: str) -> str:
        seen = set()
        while key in self.aliases and key not in seen:
            seen.add(key)
            key = self.aliases[key]
        return key

    def get(self, key: str) -> Entity | None:
        return self.entities.get(self.resolve(key))

    def by_id(self, entity_id: str) -> Entity | None:
        key = self._by_id.get(entity_id)
        return self.entities.get(key) if key else None

    def rel_by_id(self, rel_id: str) -> Relationship | None:
        return next((r for r in self.relationships.values() if r.id == rel_id), None)

    @property
    def target(self) -> Entity | None:
        return self.entities.get(self.target_key) if self.target_key else None

    def of_type(self, *types: str) -> list[Entity]:
        return [e for e in self.entities.values() if e.type in types]

    def out_edges(self, key: str, include_rejected: bool = False) -> list[Relationship]:
        rels = (self.relationships[k] for k in self._out.get(key, ()))
        return [r for r in rels if include_rejected or r.status != "rejected"]

    def in_edges(self, key: str, include_rejected: bool = False) -> list[Relationship]:
        rels = (self.relationships[k] for k in self._in.get(key, ()))
        return [r for r in rels if include_rejected or r.status != "rejected"]

    def edges(self, key: str, include_rejected: bool = False) -> list[Relationship]:
        return self.out_edges(key, include_rejected) + self.in_edges(key, include_rejected)

    def neighbors(self, key: str) -> list[Entity]:
        out = []
        for r in self.edges(key):
            other = r.target if r.source == key else r.source
            e = self.entities.get(other)
            if e is not None:
                out.append(e)
        return out

    def observations_of(self, key: str) -> list[Observation]:
        """All observations that make up the entity (its own + those of entities merged into it)."""
        key = self.resolve(key)
        keys = [key] + [k for k, v in self.aliases.items() if self.resolve(v) == key]
        return [self.observations[o] for k in keys for o in self._obs_by_entity.get(k, ()) if o in self.observations]

    def sources_of(self, key: str) -> list[Source]:
        ids: list[str] = []
        for o in self.observations_of(key):
            if o.source not in ids:
                ids.append(o.source)
        return [self.sources[i] for i in ids if i in self.sources]

    # ------------------------------------------------------------------ adding
    def add_source(self, src: Source) -> Source:
        cur = self.sources.get(src.id)
        if cur is None:
            self.sources[src.id] = src
            self.dirty.sources.add(src.id)
            return src
        if src.retrieved_at and src.retrieved_at > (cur.last_retrieved or cur.retrieved_at):
            cur.last_retrieved = src.retrieved_at
            self.dirty.sources.add(src.id)
        new_refs = [r for r in src.raw_refs if r not in cur.raw_refs]
        if new_refs:
            cur.raw_refs = (cur.raw_refs + new_refs)[-8:]
            self.dirty.sources.add(src.id)
        return cur

    def ensure_entity(self, type_id: str, value: str, label: str = "", *, depth: int = 0,
                      run: str = "", status: str = "candidate") -> tuple[Entity, bool]:
        if type_id not in ENTITY_TYPES:
            raise ValueError(f"Unknown entity type {type_id!r}")
        key = self.resolve(make_key(type_id, value))
        ent = self.entities.get(key)
        if ent is not None:
            if depth < ent.depth:
                ent.depth = depth
                self.dirty.entities.add(key)
            if not ent.label and label:
                ent.label = label
            return ent, False
        t, v = split_key(key)
        ent = Entity(t, v, label=label or v, depth=depth, discovered_by=run, status=status)
        ent.id = self.id_fn(key)
        self.entities[key] = ent
        self._by_id[ent.id] = key
        self.dirty.entities.add(key)
        return ent, True

    def add_observation(self, obs: Observation) -> bool:
        """Adds a fact. Returns False when the same fact from the same source was already known."""
        cur = self.observations.get(obs.id)
        if cur is not None:
            if obs.observed_at > cur.observed_at:
                cur.observed_at = obs.observed_at
                self.dirty.observations.add(obs.id)
            return False
        self.observations[obs.id] = obs
        self._obs_by_entity[obs.entity].append(obs.id)
        self.dirty.observations.add(obs.id)
        ent = self.get(obs.entity)
        if ent is not None:
            self._aggregate(ent, obs)
            ent.last_seen = max(ent.last_seen or "", obs.observed_at)
            self.dirty.entities.add(ent.key)
        return True

    def _aggregate(self, ent: Entity, obs: Observation) -> None:
        if obs.prop == SIGHTING:
            return
        values = ent.props.setdefault(obs.prop, [])
        k = vkey(obs.value)
        for pv in values:
            if pv["k"] == k:
                if obs.id not in pv["obs"]:
                    pv["obs"].append(obs.id)
                pv["conf"] = max(pv["conf"], obs.confidence)
                return
        values.append({"v": obs.value, "k": k, "obs": [obs.id], "conf": obs.confidence})
        values.sort(key=lambda pv: (-pv["conf"], -len(pv["obs"])))

    def rebuild_props(self, key: str) -> None:
        ent = self.get(key)
        if ent is None:
            return
        ent.props = {}
        for obs in sorted(self.observations_of(ent.key), key=lambda o: o.observed_at):
            self._aggregate(ent, obs)
        self.dirty.entities.add(ent.key)

    def add_relationship(self, source: str, target: str, type_id: str, ev: RelEvidence,
                         props: dict | None = None) -> Relationship | None:
        if type_id not in RELATION_TYPES:
            raise ValueError(f"Unknown relationship type {type_id!r}")
        s, t = self.resolve(source), self.resolve(target)
        if s == t or s not in self.entities or t not in self.entities:
            return None
        rel = Relationship(s, t, type_id, ev.confidence, [], props=dict(props or {}))
        if not ev.origin:
            ev.origin = rel.key
        cur = self.relationships.get(rel.key)
        if cur is not None:
            if cur.add_evidence(ev) or props:
                if props:
                    for k, v in props.items():
                        cur.props.setdefault(k, v)
                self.dirty.relationships.add(cur.key)
            return cur
        rel.add_evidence(ev)
        rel.id = self.id_fn("rel|" + rel.key)
        self._index_rel(rel)
        self.dirty.relationships.add(rel.key)
        return rel

    def _index_rel(self, rel: Relationship) -> None:
        self.relationships[rel.key] = rel
        self._out[rel.source].add(rel.key)
        self._in[rel.target].add(rel.key)

    def _unindex_rel(self, rel: Relationship) -> None:
        self.relationships.pop(rel.key, None)
        self._out[rel.source].discard(rel.key)
        self._in[rel.target].discard(rel.key)
        self.dirty.deleted_relationships.add(rel.key)
        self.dirty.relationships.discard(rel.key)

    def add_check(self, chk: Check) -> None:
        cur = self.checks.get(chk.key)
        # a later definitive answer (found / not found) replaces an earlier inconclusive one
        if cur is None or cur.status not in ("found", "not_found") or chk.status in ("found", "not_found"):
            self.checks[chk.key] = chk
            self.dirty.checks.add(chk.key)

    # ------------------------------------------------------------------ investigator operations
    def set_status(self, key: str, status: str) -> Entity:
        if status not in ENTITY_STATUSES or status == "seed":
            raise ValueError(f"Invalid status {status!r}")
        ent = self.get(key)
        if ent is None:
            raise KeyError(key)
        if ent.status == "seed" and status != "candidate":
            raise ValueError("Seeds are the investigation's input; start a new investigation to change them.")
        ent.status = status
        self.dirty.entities.add(ent.key)
        return ent

    def merge(self, keep_key: str, other_key: str, note: str = "") -> Entity:
        """Investigator merge: ``other`` becomes an alias of ``keep``. Fully reversible with ``separate``."""
        keep_key, other_key = self.resolve(keep_key), self.resolve(other_key)
        if keep_key == other_key:
            raise ValueError("Cannot merge an entity with itself.")
        keep, other = self.entities[keep_key], self.entities[other_key]
        if keep.type != other.type:
            raise ValueError("Only entities of the same type can be merged.")
        if other.status == "seed" and keep.status != "seed":
            keep, other = other, keep
            keep_key, other_key = other_key, keep_key
        moved: list[dict] = []
        for rel in list(self.edges(other_key, include_rejected=True)):
            snapshot = {"rel": _rel_to_dict(rel), "new_key": None, "existed": False}
            self._unindex_rel(rel)
            s = keep_key if rel.source == other_key else rel.source
            t = keep_key if rel.target == other_key else rel.target
            if s == t:
                moved.append(snapshot)
                continue
            new_key = f"{s}|{rel.type}|{t}"
            snapshot["new_key"] = new_key
            existing = self.relationships.get(new_key)
            if existing is not None:
                snapshot["existed"] = True
                for ev in rel.evidence:
                    existing.add_evidence(ev)
                self.dirty.relationships.add(new_key)
            else:
                nr = Relationship(s, t, rel.type, rel.confidence, list(rel.evidence), rel.status, dict(rel.props),
                                  first_seen=rel.first_seen)
                nr.id = self.id_fn("rel|" + nr.key)
                self._index_rel(nr)
                self.dirty.relationships.add(nr.key)
            moved.append(snapshot)
        self.merge_log.append({"keep": keep_key, "merged": other_key, "entity": _entity_to_dict(other),
                               "relationships": moved, "note": note, "at": utcnow()})
        self.aliases[other_key] = keep_key
        del self.entities[other_key]
        self._by_id.pop(other.id, None)
        self.dirty.deleted_entities.add(other_key)
        self.dirty.entities.discard(other_key)
        keep.depth = min(keep.depth, other.depth)
        self.rebuild_props(keep_key)
        for chk in self.checks.values():
            if chk.account_key == other_key:
                chk.account_key = keep_key
                self.dirty.checks.add(chk.key)
        self.dirty.meta = True
        return keep

    def separate(self, merged_key: str) -> Entity:
        """Undo an investigator merge: restore the merged entity and its relationships."""
        idx = next((i for i in range(len(self.merge_log) - 1, -1, -1) if self.merge_log[i]["merged"] == merged_key), None)
        if idx is None:
            raise KeyError(f"{merged_key} was not merged")
        entry = self.merge_log.pop(idx)
        keep_key = self.resolve(entry["keep"])
        self.aliases.pop(merged_key, None)
        restored = _entity_from_dict(entry["entity"])
        self.entities[merged_key] = restored
        self._by_id[restored.id] = merged_key
        self.dirty.entities.add(merged_key)
        self.dirty.deleted_entities.discard(merged_key)
        for snap in reversed(entry["relationships"]):
            orig = _rel_from_dict(snap["rel"])
            new_key = snap["new_key"]
            if new_key and new_key in self.relationships:
                cur = self.relationships[new_key]
                if snap["existed"]:
                    origins = {ev.origin for ev in orig.evidence}
                    cur.evidence = [ev for ev in cur.evidence if ev.origin not in origins]
                    self.dirty.relationships.add(new_key)
                else:
                    self._unindex_rel(cur)
            self._index_rel(orig)
            self.dirty.relationships.add(orig.key)
            self.dirty.deleted_relationships.discard(orig.key)
        self.rebuild_props(keep_key)
        self.rebuild_props(merged_key)
        self.dirty.meta = True
        return restored

    def reject_relationship(self, rel_id: str) -> Relationship:
        rel = self.rel_by_id(rel_id)
        if rel is None:
            raise KeyError(rel_id)
        rel.status = "rejected"
        self.dirty.relationships.add(rel.key)
        return rel

    # ------------------------------------------------------------------ restore from storage
    def restore(self, entities: Iterable[Entity], relationships: Iterable[Relationship],
                observations: Iterable[Observation], sources: Iterable[Source], checks: Iterable[Check],
                aliases: dict[str, str], merge_log: list[dict], target_key: str | None) -> None:
        for s in sources:
            self.sources[s.id] = s
        for e in entities:
            e.props = {}
            self.entities[e.key] = e
            self._by_id[e.id] = e.key
        self.aliases = dict(aliases or {})
        self.merge_log = list(merge_log or [])
        self.target_key = target_key
        for o in sorted(observations, key=lambda o: o.observed_at):
            self.observations[o.id] = o
            self._obs_by_entity[o.entity].append(o.id)
            ent = self.get(o.entity)
            if ent is not None:
                self._aggregate(ent, o)
        for r in relationships:
            self._index_rel(r)
        for c in checks:
            self.checks[c.key] = c
        self.dirty.clear()

    # ------------------------------------------------------------------ views
    def stats(self) -> dict:
        by_type: dict[str, int] = defaultdict(int)
        for e in self.entities.values():
            by_type[e.type] += 1
        high = sum(1 for e in self.entities.values()
                   if e.status != "seed" and e.active and (e.status == "confirmed" or e.confidence >= 0.75)
                   and ENTITY_TYPES[e.type].resolvable)
        return {"entities": len(self.entities), "relationships": len(self.relationships),
                "observations": len(self.observations), "sources": len(self.sources),
                "high_confidence": high, "by_type": dict(by_type)}


def _entity_to_dict(e: Entity) -> dict:
    d = asdict(e)
    d["reasons"] = [r.to_dict() for r in e.reasons]
    d.pop("props", None)
    return d


def _entity_from_dict(d: dict) -> Entity:
    e = Entity(**{k: d.get(k) for k in Entity.__dataclass_fields__ if k in d and k not in ("reasons", "props")})
    e.reasons = [Reason.from_dict(r) for r in d.get("reasons") or []]
    return e


def _rel_to_dict(r: Relationship) -> dict:
    d = asdict(r)
    return d


def _rel_from_dict(d: dict) -> Relationship:
    r = Relationship(**{k: d.get(k) for k in Relationship.__dataclass_fields__ if k in d and k != "evidence"})
    r.evidence = [RelEvidence.from_dict(e) for e in d.get("evidence") or []]
    return r


entity_to_dict = _entity_to_dict
entity_from_dict = _entity_from_dict
rel_to_dict = _rel_to_dict
rel_from_dict = _rel_from_dict
