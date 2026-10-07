"""TransformResult — what a transform hands back to the engine.

A transform never touches the investigation graph. It describes what it found: sources, entities,
facts (observations), relationships and per-site checks. The engine validates the result, assigns
ids and depth, attaches provenance, deduplicates, and merges it. This keeps connectors pure and
testable with recorded fixtures, and makes it impossible for a connector to record a fact without
saying where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model import SIGHTING, short_hash, utcnow
from ..ontology import ENTITY_TYPES, RELATION_TYPES, make_key, normalize_value

MAX_STR = 5000
MAX_DATA_URL = 120_000          # small re-encoded thumbnails (data:image/…) are kept whole
MAX_LIST = 200


def clean_value(value: Any, depth: int = 0) -> Any:
    """Bounds untrusted values (strings, lists, nested dicts) before they enter the graph."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return value if len(value) <= MAX_DATA_URL else None
        return value.replace("\x00", "")[:MAX_STR]
    if depth > 4:
        return None
    if isinstance(value, (list, tuple, set)):
        out = [clean_value(v, depth + 1) for v in list(value)[:MAX_LIST]]
        return [v for v in out if v not in (None, "", [], {})]
    if isinstance(value, dict):
        return {str(k)[:100]: clean_value(v, depth + 1) for k, v in list(value.items())[:MAX_LIST]
                if v not in (None, "", [], {})}
    return str(value)[:MAX_STR]


@dataclass
class SourceDraft:
    id: str
    kind: str
    url: str | None
    title: str
    request: str
    retrieved_at: str
    raw_refs: tuple[str, ...] = ()      # filled by the engine from the context's fetch log


@dataclass
class EntityDraft:
    key: str
    type: str
    value: str
    label: str
    source: str
    evidence: str
    confidence: float


@dataclass
class ObservationDraft:
    entity: str
    prop: str
    value: Any
    source: str
    confidence: float
    evidence: str


@dataclass
class RelationshipDraft:
    source: str
    target: str
    type: str
    source_id: str
    description: str
    confidence: float
    props: dict = field(default_factory=dict)


@dataclass
class CheckDraft:
    platform: str
    status: str
    identifier: str
    identifier_type: str
    detail: str = ""
    url: str | None = None
    account: str | None = None
    platform_title: str = ""
    category: str = ""


class TransformResult:
    """Builder used by transforms (``ctx.out``)."""

    def __init__(self, transform: str, connector: str, input_key: str, input_type: str, input_display: str) -> None:
        self.transform = transform
        self.connector = connector
        self.input = input_key
        self.input_type = input_type
        self.input_display = input_display
        self.sources: dict[str, SourceDraft] = {}
        self.entities: dict[str, EntityDraft] = {}
        self.observations: list[ObservationDraft] = []
        self.relationships: list[RelationshipDraft] = []
        self.checks: list[CheckDraft] = []
        self.status: str = ""          # set explicitly or inferred by the engine
        self.message: str = ""
        self.rejected: list[str] = []  # values that failed normalization (kept for debugging/tests)

    # ------------------------------------------------------------------ provenance
    def source(self, url: str | None = None, *, title: str = "", kind: str = "api", request: str = "") -> str:
        """Registers where the following facts come from. Returns the source id to pass to facts."""
        sid = short_hash(self.connector, kind, url or "", title if not url else "", self.input if not url else "")
        if sid not in self.sources:
            self.sources[sid] = SourceDraft(sid, kind, url, title[:300], request[:300], utcnow())
        return sid

    def _need_source(self, source: str | None) -> str:
        if not source:
            raise ValueError(f"{self.transform}: every fact needs a source (call out.source(...) first)")
        if source not in self.sources:
            raise ValueError(f"{self.transform}: unknown source id {source!r}")
        return source

    # ------------------------------------------------------------------ entities and facts
    def entity(self, type_id: str, value: Any, *, source: str, label: str | None = None,
               props: dict[str, Any] | None = None, evidence: str = "", confidence: float = 1.0) -> str | None:
        """Declares an entity (with a sighting observation) and optional properties.
        Returns its key, or None when the value is not valid for the type (silently dropped)."""
        if type_id not in ENTITY_TYPES:
            raise ValueError(f"{self.transform}: unknown entity type {type_id!r}")
        source = self._need_source(source)
        if value is None or str(value).strip() == "":
            return None
        norm = normalize_value(type_id, str(value))
        if not norm:
            self.rejected.append(f"{type_id}:{str(value)[:80]}")
            return None
        key = make_key(type_id, norm)
        lbl = (label or str(value)).strip()[:300] or norm
        if key not in self.entities:
            self.entities[key] = EntityDraft(key, type_id, norm, lbl, source, evidence[:500], confidence)
        self.observations.append(ObservationDraft(key, SIGHTING, lbl, source, confidence, evidence[:500]))
        for name, val in (props or {}).items():
            self.prop(key, name, val, source=source, confidence=confidence, evidence=evidence)
        return key

    def prop(self, entity: str, name: str, value: Any, *, source: str, confidence: float = 1.0, evidence: str = "") -> None:
        source = self._need_source(source)
        value = clean_value(value)
        if value in (None, "", [], {}):
            return
        if name == SIGHTING or not name or len(name) > 60:
            raise ValueError(f"{self.transform}: invalid property name {name!r}")
        self.observations.append(ObservationDraft(entity, name, value, source, confidence, evidence[:500]))

    def props(self, entity: str, values: dict[str, Any], *, source: str, confidence: float = 1.0, evidence: str = "") -> None:
        for name, value in values.items():
            self.prop(entity, name, value, source=source, confidence=confidence, evidence=evidence)

    def link(self, a: str | None, b: str | None, rel_type: str, *, source: str, evidence: str = "",
             confidence: float = 1.0, props: dict | None = None) -> None:
        if not a or not b or a == b:
            return
        if rel_type not in RELATION_TYPES:
            raise ValueError(f"{self.transform}: unknown relationship type {rel_type!r}")
        source = self._need_source(source)
        self.relationships.append(RelationshipDraft(a, b, rel_type, source, evidence[:500], max(0.0, min(1.0, confidence)),
                                                    clean_value(props or {}) or {}))

    def check(self, platform: str, status: str, *, detail: str = "", url: str | None = None, account: str | None = None,
              platform_title: str = "", category: str = "", identifier: str | None = None,
              identifier_type: str | None = None) -> None:
        if status not in ("found", "not_found", "blocked", "unknown", "skipped", "error"):
            raise ValueError(f"invalid check status {status!r}")
        self.checks.append(CheckDraft(platform, status, identifier or self.input_display,
                                      identifier_type or self.input_type, detail[:300], url, account,
                                      platform_title, category))

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message[:500]

    # ------------------------------------------------------------------ introspection
    @property
    def found_anything(self) -> bool:
        return any(k != self.input for k in self.entities) or bool(self.relationships)

    def summary(self) -> dict:
        return {"sources": len(self.sources), "entities": len(self.entities), "observations": len(self.observations),
                "relationships": len(self.relationships), "checks": len(self.checks), "status": self.status,
                "message": self.message}
