"""Identity resolution: conservative, explained, contradiction-aware; investigator decisions anchor it."""

from sextant.model import Graph, Observation, RelEvidence, Source, short_hash
from sextant.resolution.scorer import link_same_owner, resolve

SRC = "src"


def obs(g, key, prop, value):
    g.add_observation(Observation(id=short_hash(key, prop, str(value)), entity=key, prop=prop, value=value, source=SRC))


def base(name="Jane Doe", birth_year=None, username="jdoe_x92", email="jane@janedoe.dev", city="Oulu, Finland"):
    g = Graph()
    g.add_source(Source(id=SRC, connector="test", kind="api"))
    t, _ = g.ensure_entity("person", "target", name, status="seed")
    g.target_key = t.key
    obs(g, t.key, "full_name", name)
    if birth_year:
        obs(g, t.key, "birth_year", birth_year)
    for typ, val in (("username", username), ("email", email)):
        if val:
            e, _ = g.ensure_entity(typ, val, val, status="seed")
            g.add_relationship(t.key, e.key, "USES", RelEvidence(SRC, "input"))
    if city:
        loc, _ = g.ensure_entity("location", "oulu finland", city, status="seed")
        obs(g, loc.key, "text", city)
    return g


def account(g, key, **props):
    e, _ = g.ensure_entity("account", key, key)
    for k, v in props.items():
        obs(g, e.key, k, v)
    return e


def record(g, key, matched, birth_years=None):
    e, _ = g.ensure_entity("public_record", key, key)
    obs(g, e.key, "matched_name", matched)
    obs(g, e.key, "name_match", "exact")
    if birth_years:
        obs(g, e.key, "birth_years", birth_years)
    return e


def feats(e):
    return {r.feature for r in e.reasons}


def test_name_only_record_is_never_likely():
    g = base()
    r = record(g, "un:1", "Jane Doe")
    resolve(g)
    assert r.confidence < 0.4 and r.match_label == "Possible name match"
    assert "name_full" in feats(r)


def test_birth_year_rules_namesakes_in_or_out_locally():
    g = base(birth_year=1985)
    r = record(g, "un:1", "Jane Doe", [1971])
    resolve(g)
    assert r.confidence < 0.05 and any(x.weight < 0 for x in r.reasons)      # contradiction kept and shown
    g = base(birth_year=1971)
    r = record(g, "un:1", "Jane Doe", [1971])
    resolve(g)
    assert r.confidence > 0.6 and r.match_label == "Possible name match"      # still never "confirmed" automatically


def test_email_on_profile_is_strong_and_contradictions_are_separate():
    g = base()
    a = account(g, "github:jdoe_x92", username="jdoe_x92", display_name="Jane Doe", emails=["jane@janedoe.dev"],
                location="Lisbon")
    resolve(g)
    assert a.confidence > 0.95 and a.tier == "high" and a.match_label == "Likely match"
    assert {"email", "username", "name_full"} <= feats(a) and "location_mismatch" in feats(a)
    assert any(r.weight < 0 for r in a.reasons)


def test_common_username_alone_stays_low():
    g = base(username="john", email=None, name="John Smith", city=None)
    a = account(g, "steam:john", username="john")
    resolve(g)
    assert a.confidence < 0.3 and a.tier == "low" and "username_common" in feats(a)


def test_different_name_is_a_contradiction():
    g = base()
    a = account(g, "chesscom:jdoe_x92", username="jdoe_x92", display_name="Robert Smith", location="Ohio")
    resolve(g)
    assert "name_mismatch" in feats(a) and "location_mismatch" in feats(a) and a.match_label == "Unlikely match"


def test_confirmation_makes_an_anchor_for_cross_links():
    g = base(email=None)
    gh = account(g, "gitlab:jdoe_x92", username="jdoe_x92", profile_url="https://gitlab.com/jdoe_x92")
    other = account(g, "mastodon:x@mastodon.social", username="x", links=["https://gitlab.com/jdoe_x92"])
    resolve(g)
    before = other.confidence
    g.set_status(gh.key, "confirmed")
    resolve(g)
    assert "link_to_anchor" in feats(other) and other.confidence > before + 0.3


def test_cross_linked_same_handle_accounts_corroborate():
    g = base(email=None)
    a = account(g, "github:jdoe_x92", username="jdoe_x92")
    b = account(g, "keybase:jdoe_x92", username="jdoe_x92")
    g.add_relationship(b.key, a.key, "LINKED_TO", RelEvidence(SRC, "Verified link (cryptographic proof)"))
    resolve(g)
    assert "corroborated_verified" in feats(a) and "corroborated_verified" in feats(b)


def test_seed_and_separated_are_pinned():
    g = base()
    a = account(g, "github:other", username="other", emails=["jane@janedoe.dev"])
    g.set_status(a.key, "separated")
    resolve(g)
    assert a.confidence == 0.0 and a.value_score == 0.0 and a.match_label.startswith("Different person")


def test_identifiers_inherit_identity_and_value_separately():
    g = base(email=None)
    a = account(g, "github:jdoe_x92", username="jdoe_x92", display_name="Jane Doe")
    d, _ = g.ensure_entity("domain", "janedoe.dev", "janedoe.dev")
    gen, _ = g.ensure_entity("domain", "github.com", "github.com")
    g.add_relationship(a.key, d.key, "LINKED_TO", RelEvidence(SRC, "website"))
    g.add_relationship(a.key, gen.key, "LINKED_TO", RelEvidence(SRC, "website"))
    resolve(g)
    assert abs(d.confidence - a.confidence) < 1e-6               # identity inherited along the link
    assert d.value_score >= 0.5 and gen.value_score == 0.0       # generic platform domains are never expanded


def test_same_owner_links_need_two_signals():
    g = base(email=None)
    a = account(g, "github:jdoe_x92", username="jdoe_x92", display_name="Jane Doe")
    b = account(g, "gitlab:jdoe_x92", username="jdoe_x92", display_name="Jane Doe")
    c = account(g, "codeberg:jdoe_x92", username="jdoe_x92")
    assert link_same_owner(g) == 1
    rel = next(r for r in g.relationships.values() if r.type == "SAME_AS")
    assert {rel.source, rel.target} == {a.key, b.key} and c.key not in (rel.source, rel.target)

