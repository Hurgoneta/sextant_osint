"""Evidence model: every fact has provenance, properties aggregate observations, merges are reversible."""

import pytest

from sextant.model import Graph, Observation, RelEvidence, Source, short_hash
from sextant.sdk.result import TransformResult


def obs(g, key, prop, value, src="s1"):
    g.add_observation(Observation(id=short_hash(key, prop, value, src), entity=key, prop=prop, value=value, source=src))


def make_graph():
    g = Graph()
    for sid in ("s1", "s2"):
        g.add_source(Source(id=sid, connector=f"c-{sid}", kind="api", url=f"https://{sid}.example/x"))
    a, _ = g.ensure_entity("account", "github:jdoe", "GitHub: jdoe")
    b, _ = g.ensure_entity("account", "gitlab:jdoe", "GitLab: jdoe")
    u, _ = g.ensure_entity("username", "jdoe", "jdoe")
    obs(g, a.key, "display_name", "Jane Doe", "s1")
    obs(g, a.key, "display_name", "Jane Doe", "s2")
    obs(g, b.key, "location", "Berlin", "s2")
    g.add_relationship(a.key, u.key, "USES", RelEvidence("s1", "handle"))
    g.add_relationship(b.key, u.key, "USES", RelEvidence("s2", "handle"))
    return g, a, b, u


def test_properties_aggregate_observations_with_provenance():
    g, a, _, _ = make_graph()
    vals = a.props["display_name"]
    assert len(vals) == 1 and vals[0]["v"] == "Jane Doe" and len(vals[0]["obs"]) == 2
    assert {g.observations[o].source for o in vals[0]["obs"]} == {"s1", "s2"}
    assert {s.url for s in g.sources_of(a.key)} == {"https://s1.example/x", "https://s2.example/x"}


def test_duplicate_fact_is_not_duplicated():
    g, a, _, _ = make_graph()
    n = len(g.observations)
    obs(g, a.key, "display_name", "Jane Doe", "s1")
    assert len(g.observations) == n


def test_merge_and_separate_are_reversible():
    g, a, b, u = make_graph()
    rels_before = {r.key for r in g.relationships.values()}
    kept = g.merge(a.key, b.key, "same person")
    assert b.key not in g.entities and kept.get("location") == "Berlin"
    assert g.resolve(b.key) == a.key
    assert len(g.edges(u.key)) == 1                       # both USES edges collapsed into one with two evidence items
    assert len(g.edges(u.key)[0].evidence) == 2
    g.separate(b.key)
    assert b.key in g.entities and g.get(a.key).get("location") is None and g.get(b.key).get("location") == "Berlin"
    assert {r.key for r in g.relationships.values()} == rels_before
    assert all(len(r.evidence) == 1 for r in g.relationships.values())


def test_merge_rejects_different_types():
    g, a, _, u = make_graph()
    with pytest.raises(ValueError):
        g.merge(a.key, u.key)


def test_seeds_cannot_be_reclassified():
    g = Graph()
    e, _ = g.ensure_entity("email", "a@example.com", status="seed")
    with pytest.raises(ValueError):
        g.set_status(e.key, "ignored")


def test_result_builder_requires_provenance_and_validates():
    out = TransformResult("x.t", "x", "username:jdoe", "username", "jdoe")
    with pytest.raises(ValueError):
        out.entity("email", "a@example.com", source="")
    src = out.source("https://api.example.org/u/jdoe", title="API")
    assert out.entity("email", "not an email", source=src) is None and out.rejected
    with pytest.raises(ValueError):
        out.entity("spaceship", "x", source=src)
    with pytest.raises(ValueError):
        out.link("username:jdoe", "email:a@example.com", "LIKES", source=src)
    k = out.entity("account", "example:jdoe", source=src, props={"bio": "x" * 10_000, "avatar_thumb": "data:image/jpeg;base64," + "A" * 9000})
    vals = {o.prop: o.value for o in out.observations if o.entity == k}
    assert len(vals["bio"]) == 5000                          # untrusted strings are bounded…
    assert len(vals["avatar_thumb"]) > 9000                  # …but small image data URLs are kept whole


def test_dirty_tracking_drives_incremental_persistence():
    g, a, _, _ = make_graph()
    assert g.dirty.entities and g.dirty.observations and g.dirty.relationships
    g.dirty.clear()
    assert not g.dirty
    obs(g, a.key, "bio", "hello", "s1")
    assert g.dirty.entities == {a.key} and len(g.dirty.observations) == 1


def test_year_range_for_the_graph_filter_ignores_birth_dates():
    g, a, b, u = make_graph()
    obs(g, a.key, "created_at", "2015-03-01T10:00:00Z")
    obs(g, a.key, "archive_last_capture", "2024-11-02")
    assert a.summary()["years"] == [2015, 2024]
    p, _ = g.ensure_entity("person", "registry:1", "Jane Doe")
    obs(g, p.key, "birth_date", "1971-02")
    assert p.years() is None and u.summary()["years"] is None
