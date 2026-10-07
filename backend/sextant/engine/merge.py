"""Merging a TransformResult into the investigation graph.

This is the only place where connector output becomes graph state. Every fact gets a stable
observation id (entity + property + value + source), every relationship carries its evidence, and
every new entity records its depth and the run that discovered it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import Check, Graph, Observation, RelEvidence, Source, TransformRun, short_hash, utcnow, vkey
from ..sdk.manifest import Manifest
from ..sdk.result import TransformResult


@dataclass
class MergeStats:
    new_entities: list[str] = field(default_factory=list)
    touched_entities: set[str] = field(default_factory=set)
    new_relationships: list[str] = field(default_factory=list)
    new_observations: int = 0
    new_checks: int = 0


def merge_result(g: Graph, out: TransformResult, *, run: TransformRun, manifest: Manifest, input_depth: int) -> MergeStats:
    stats = MergeStats()
    now = utcnow()
    for d in out.sources.values():
        g.add_source(Source(id=d.id, connector=manifest.name, kind=d.kind, url=d.url, title=d.title or manifest.title,
                            transform=out.transform, retrieved_at=d.retrieved_at, reliability=manifest.reliability,
                            license=manifest.license or None, request=d.request or run.sent,
                            raw_refs=list(d.raw_refs)))
    input_key = g.resolve(out.input)
    known: set[str] = {input_key}
    for d in out.entities.values():
        if g.resolve(d.key) == input_key:
            continue
        ent, is_new = g.ensure_entity(d.type, d.value, d.label, depth=input_depth + 1, run=run.id)
        known.add(ent.key)
        stats.touched_entities.add(ent.key)
        if is_new:
            stats.new_entities.append(ent.key)
    for o in out.observations:
        target = g.resolve(o.entity)
        if target not in known and target not in g.entities:
            continue
        oid = short_hash(o.entity, o.prop, vkey(o.value), o.source)
        if g.add_observation(Observation(id=oid, entity=o.entity, prop=o.prop, value=o.value, source=o.source, run=run.id,
                                         observed_at=now, confidence=o.confidence, evidence=o.evidence)):
            stats.new_observations += 1
            stats.touched_entities.add(target)
    for r in out.relationships:
        before = len(g.relationships)
        rel = g.add_relationship(r.source, r.target, r.type, RelEvidence(r.source_id, r.description, run.id, r.confidence,
                                                                         now), r.props)
        if rel is not None and len(g.relationships) > before:
            stats.new_relationships.append(rel.key)
    for c in out.checks:
        g.add_check(Check(connector=manifest.name, transform=out.transform, platform=c.platform,
                          identifier_type=c.identifier_type, identifier=c.identifier, status=c.status, detail=c.detail,
                          url=c.url, account_key=g.resolve(c.account) if c.account else None,
                          platform_title=c.platform_title, category=c.category, checked_at=now))
        stats.new_checks += 1
    return stats
