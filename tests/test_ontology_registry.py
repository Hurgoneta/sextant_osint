"""Ontology, input understanding, and the integrity of the connector / platform / jurisdiction registries."""

import pytest

from sextant.engine.input import parse_input
from sextant.ontology import ENTITY_TYPES, RELATION_TYPES, normalize_value
from sextant.registry import connectors as REG
from sextant.registry import jurisdictions as JUR
from sextant.registry import platforms as PLAT
from sextant.sdk.manifest import CATEGORIES


@pytest.mark.parametrize("t,raw,expected", [
    ("email", "John@Example.COM", "john@example.com"),
    ("phone", "+358 40 123 4567", "+358401234567"),
    ("username", "@JDoe", "jdoe"),
    ("account", "GitHub:JDoe", "github:jdoe"),
    ("website", "https://www.Example.com/a/?utm_source=x#f", "https://example.com/a"),
    ("publication", "https://doi.org/10.1000/XYZ", "doi:10.1000/xyz"),
    ("name", "SMITH, John", "john smith"),
    ("domain", "WWW.Example.co.uk", "example.co.uk"),
    ("ip", "2001:DB8::1", "2001:db8::1"),
    ("asn", "as15169", "AS15169"),
])
def test_normalizers(t, raw, expected):
    assert normalize_value(t, raw) == expected


def test_invalid_values_are_rejected():
    assert normalize_value("email", "not-an-email") is None
    assert normalize_value("ip", "999.1.1.1") is None
    assert normalize_value("domain", "exa mple.com") is None


def test_required_relationship_vocabulary_present():
    for rel in ("USES", "OWNS", "AUTHORED", "MENTIONED_IN", "LINKED_TO", "LOCATED_IN", "WORKS_FOR", "MEMBER_OF",
                "APPEARS_ON", "ASSOCIATED_WITH", "REGISTERED_TO", "REFERENCED_BY"):
        assert rel in RELATION_TYPES
    for t in ("person", "email", "phone", "username", "account", "website", "domain", "ip", "organization", "location",
              "document", "image", "publication", "public_record", "source"):
        assert t in ENTITY_TYPES


def test_local_only_birth_year():
    assert ENTITY_TYPES["person"].prop("birth_year").local_only


@pytest.mark.parametrize("text,target,seeds", [
    ("John Michael Smith", "John Michael Smith", []),
    ("john@example.com", None, [("email", "john@example.com")]),
    ("+358 40 123 4567", None, [("phone", "+358401234567")]),
    ("johnsmith123", None, [("username", "johnsmith123")]),
    ("John Smith, john@example.com, @jsmith", "John Smith", [("email", "john@example.com"), ("username", "jsmith")]),
    ("https://github.com/octocat", None, [("account", "github:octocat"), ("username", "octocat")]),
    ("example.com\n8.8.8.8", None, [("domain", "example.com"), ("ip", "8.8.8.8")]),
    ("@jdoe@social.example", None, [("username", "jdoe")]),
])
def test_input_understanding(text, target, seeds):
    p = parse_input(text)
    assert p.target_name == target
    assert [(s.type, s.value) for s in p.seeds] == seeds
    assert not p.errors


def test_input_prefixes_birth_year_and_org():
    p = parse_input("name: Jane Example\nborn:1988\nlocation: Oulu, Finland")
    assert p.target_name == "Jane Example" and p.birth_year == 1988 and p.seeds[0].type == "location"
    assert parse_input("Acme Software SRL").target_type == "organization"
    assert parse_input("example.com").target_type == "none"


def test_input_reports_ambiguity_instead_of_guessing():
    p = parse_input("0401234567")
    assert not p.seeds and any("international format" in e for e in p.errors)
    assert parse_input("0401234567", default_region="FI").seeds[0].value == "+358401234567"
    assert parse_input("jane").notes                       # single word: flagged as possibly a first name


def test_every_connector_manifest_is_valid_and_disclosed():
    assert len(REG.REGISTRY) >= 100
    for name, conn in REG.REGISTRY.items():
        m = conn.manifest
        problems = [p for p in m.validate() if m.implemented or ("credential" not in p and "disclose" not in p)]
        assert not problems, (name, problems)
        assert m.category in CATEGORIES
        if m.implemented and m.kind != "catalog":
            assert conn.transforms(), f"{name} has no transforms"
            # meta-connectors (one request to each of many platforms) list destinations per platform instead
            assert m.website.startswith(("https://", "http://")) or "meta" in m.tags, name


def test_transforms_declare_known_types_and_unique_names():
    names = set()
    for t in REG.TRANSFORMS.values():
        assert t.name not in names and "." in t.name
        names.add(t.name)
        assert t.input_types and all(x in ENTITY_TYPES for x in (*t.input_types, *t.output_types))


def test_every_entity_type_that_matters_has_transforms():
    for t in ("person", "email", "phone", "username", "domain", "ip", "organization", "account", "image", "location"):
        assert REG.transforms_for(t), t


def test_paid_sources_never_auto_run_by_default():
    for conn in REG.REGISTRY.values():
        m = conn.manifest
        if m.access in ("paid", "metered", "enterprise") and m.name != "hibp" and m.category != "search":
            assert not m.auto_default, m.name


def test_excluded_sources_are_never_available():
    for name in ("dehashed", "intelx", "pimeyes", "holehe", "ghunt"):
        assert REG.connector_status(REG.REGISTRY[name], None)[0] == "excluded"


def test_platform_registry_and_url_recognition():
    assert len(PLAT.curated()) >= 100
    cases = {"https://github.com/octocat": ("github", "octocat"), "https://github.com/about": None,
             "https://x.com/home": None, "https://twitter.com/jdoe": ("x", "jdoe"),
             "https://jdoe.substack.com": ("substack", "jdoe"), "https://news.ycombinator.com/user?id=jdoe": ("hackernews", "jdoe"),
             "https://www.youtube.com/@mkbhd": ("youtube", "mkbhd"), "https://fosstodon.org/@jdoe": ("mastodon", "jdoe"),
             "https://huggingface.co/models": None, "https://example.com/jdoe": None}
    for url, expected in cases.items():
        r = PLAT.parse_profile_url(url)
        assert (r[0].id, r[1]) == expected if expected else r is None, url
    links = {m["platform"] for m in PLAT.manual_links("jdoe92")}
    assert "facebook" not in {m["platform"] for m in PLAT.manual_links("jdoe")}   # too short for Facebook handles
    assert {"facebook", "linkedin", "tiktok"} <= links


def test_platform_checks_reference_real_connectors():
    special = {"whatsmyname", "maigret", "sherlock", "probe", "manual", "none"}
    for p in PLAT.curated():
        assert p.check in special or p.check in REG.REGISTRY, (p.id, p.check)


def test_jurisdiction_registry_integrity():
    js = JUR.jurisdictions()
    assert len(js) >= 70 and len(JUR.all_sources()) >= 300
    regions = {j.region for j in js.values()}
    assert {"Europe", "North America", "South America", "Asia", "Africa", "Oceania"} <= regions
    for s in JUR.all_sources():
        assert s.url.startswith(("https://", "http://")), s.id
        if s.connector:
            assert s.connector in REG.REGISTRY, (s.id, s.connector)
        if s.kind == "own_record":
            assert not s.name_search, s.id            # criminal-record extracts are never searchable by others
    assert not js["SE"].criminal_records_public and js["DE"].judgments_anonymized


def test_catalog_verifier_classification():
    from sextant.tools.verify_catalog import classify
    assert classify(200, None) == "ok"
    assert classify(200, None, "https://unblock.federalregister.gov/") == "reachable_refuses_bots"
    assert classify(403, None) == "reachable_refuses_bots" and classify(404, None) == "dead"
    assert classify(None, "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]") == "tls_problem"
    assert classify(None, "ConnectTimeout: ") == "unreachable"
