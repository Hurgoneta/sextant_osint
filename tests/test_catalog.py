"""The catalog's user layer: one record per source, seen by every screen, persistent, and removed by the one reset.

A "restart" is a second app on the same data folder. Tests A–O follow the acceptance list of the feature.
"""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import engine_with, run
from sextant.api.app import build_state, create_app
from sextant.engine.input import parse_input
from sextant.registry import catalog as CAT
from sextant.registry import connectors as REG
from sextant.registry import platforms as PLAT
from sextant.sdk import Connector, Disclosure, Manifest, register, transform
from sextant.sdk.errors import Blocked
from sextant.sdk.testing import mock_http
from sextant.storage.crypto import Vault
from sextant.storage.db import Database

ROUTES = {"https://krimfup.se": (200, "<html>Krimfup</html>", {"content-type": "text/html"})}
KRIMFUP = {"title": "Krimfup", "url": "https://krimfup.se", "category": "legal_records", "jurisdictions": ["SE"],
           "source_type": "manual"}
LEXBASE = {"title": "Lexbase", "url": "https://www.lexbase.se", "category": "legal_records", "jurisdictions": ["SE"],
           "source_type": "reference"}


@contextmanager
def app(data_dir):
    ui = data_dir.parent / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<html>ui</html>")
    state = build_state(data_dir, ui, "tok", 8765, http_factory=lambda **kw: mock_http(ROUTES, **kw))
    with TestClient(create_app(state), base_url="http://127.0.0.1:8765") as c:
        c.headers["X-Sextant-Token"] = "tok"
        c.state = state
        yield c
    state.db.close()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def client(data_dir):
    with app(data_dir) as c:
        yield c


# --------------------------------------------------------------------------- views of one record

def in_sources(c, sid):
    for g in c.get("/api/sources").json()["groups"]:
        for s in g["sources"]:
            if s["name"] == sid:
                return {**s, "_group": g["category"], "_group_title": g["title"]}
    return None


def in_jurisdictions(c, code, sid):
    for r in c.get("/api/jurisdictions").json()["regions"]:
        for j in r["jurisdictions"]:
            if j["code"] == code:
                return next((s for s in j["sources"] if s["id"] == sid), None)
    raise AssertionError(code)


def jurisdiction(c, code):
    return next(j for r in c.get("/api/jurisdictions").json()["regions"] for j in r["jurisdictions"] if j["code"] == code)


def in_country(c, code, sid):
    return next((s for s in c.get(f"/api/global-coverage/{code}").json()["custom"] if s["id"] == sid), None)


def in_matrix(c, sid):
    return next((r for r in c.get("/api/coverage-matrix").json()["rows"] if r["name"] == sid), None)


def country_row(c, code):
    return next(r for r in c.get("/api/global-coverage").json()["rows"] if r["code"] == code)


def add(c, body, status=201):
    r = c.post("/api/catalog/sources", json=body)
    assert r.status_code == status, r.text
    return r.json()


# --------------------------------------------------------------------------- A–H: one record, every view

def test_a_source_added_in_data_sources_is_seen_by_the_jurisdiction(client):
    before = country_row(client, "SE")                    # fills the coverage cache first
    src = add(client, KRIMFUP)
    sid = src["id"]
    assert src["origin"] == "user" and src["jurisdictions"] == ["SE"]
    item = in_sources(client, sid)
    assert item["_group"] == "legal_records" and item["_group_title"] == "Legal & judicial records"
    assert item["status"] == "manual" and item["origin"] == "user"
    assert item["health"]["status"] == "unknown"           # never claimed to work before it is checked
    assert in_jurisdictions(client, "SE", sid)["group"] == "judicial"
    assert in_country(client, "SE", sid)["title"] == "Krimfup"
    after = country_row(client, "SE")                     # not served from the stale cache
    assert after["custom_sources"] == before["custom_sources"] + 1 and after["sources"] == before["sources"] + 1
    assert after["groups"]["judicial"]["user"] == 1


def test_b_source_added_from_a_jurisdiction_is_one_record_in_data_sources(client):
    src = add(client, {**KRIMFUP, "jurisdictions": ["SE"]})
    records = client.get("/api/catalog/sources").json()
    assert [r["id"] for r in records] == [src["id"]]       # exactly one record, no per-screen copy
    assert in_sources(client, src["id"])["title"] == "Krimfup"
    assert in_jurisdictions(client, "SE", src["id"])["id"] == src["id"]
    assert [s["id"] for s in client.get("/api/catalog/sources?jurisdiction=SE").json()] == [src["id"]]
    se = jurisdiction(client, "SE")
    assert se["user_sources"] == 1 and len(se["sources"]) == len(
        [s for s in se["sources"] if s["origin"] == "system"]) + 1


def test_c_reference_link_is_shown_as_a_link_not_as_automation(client):
    link = add(client, LEXBASE)
    item = in_sources(client, link["id"])
    assert item["automation"]["mode"] == "none" and item["status"] == "manual"
    assert item["transforms"] == [] and item["auto_allowed"] is False
    assert item["disclosure"]["destinations"] == [] and item["disclosure"]["sends"].startswith("Nothing")
    assert in_matrix(client, link["id"])["api"] is False
    assert in_jurisdictions(client, "SE", link["id"])["source_type"] == "reference"
    api_src = add(client, {"title": "Some register API", "url": "https://api.register.example/v1",
                           "category": "companies", "source_type": "official_api", "jurisdictions": ["SE"]})
    m = in_matrix(client, api_src["id"])
    assert m["api"] is True and m["automation"] == "none"  # an API address is not a connector
    assert "no connector" in in_sources(client, api_src["id"])["automation"]["title"]


def test_d_category_change_updates_matrix_data_sources_and_jurisdiction(client):
    sid = add(client, KRIMFUP)["id"]
    assert in_matrix(client, sid)["category"] == "legal_records"
    r = client.put(f"/api/catalog/sources/{sid}", json={**KRIMFUP, "category": "companies"})
    assert r.status_code == 200, r.text
    assert in_matrix(client, sid)["category_title"] == "Company registries"
    assert in_sources(client, sid)["_group"] == "companies"
    assert in_jurisdictions(client, "SE", sid)["group"] == "corporate"


def test_e_jurisdiction_change_moves_the_source(client):
    sid = add(client, KRIMFUP)["id"]
    se_before = country_row(client, "SE")["custom_sources"]
    client.put(f"/api/catalog/sources/{sid}", json={**KRIMFUP, "jurisdictions": ["DE"]})
    assert in_jurisdictions(client, "SE", sid) is None and in_country(client, "SE", sid) is None
    assert in_jurisdictions(client, "DE", sid) is not None and in_country(client, "DE", sid) is not None
    assert country_row(client, "SE")["custom_sources"] == se_before - 1
    assert in_sources(client, sid)["jurisdictions"] == ["DE"] and in_matrix(client, sid)["jurisdictions"] == ["DE"]


def test_f_disable_keeps_the_source_and_persists_everywhere(data_dir):
    with app(data_dir) as c:
        sid = add(c, KRIMFUP)["id"]
        active_before = country_row(c, "SE")["custom_active"]
        assert c.patch(f"/api/catalog/sources/{sid}", json={"enabled": False}).status_code == 200
    with app(data_dir) as c:                             # after a restart
        assert in_sources(c, sid)["status"] == "disabled"
        assert in_matrix(c, sid)["status"] == "disabled"
        j = in_jurisdictions(c, "SE", sid)
        assert j["status"] == "disabled" and j["active"] is False and j["title"] == "Krimfup"
        row = country_row(c, "SE")
        assert row["custom_active"] == active_before - 1 and in_country(c, "SE", sid) is not None


def test_g_delete_removes_it_everywhere_without_orphans(client):
    sid = add(client, KRIMFUP)["id"]
    assert client.delete(f"/api/catalog/sources/{sid}").json()["ok"]
    assert in_sources(client, sid) is None and in_matrix(client, sid) is None
    assert in_jurisdictions(client, "SE", sid) is None and in_country(client, "SE", sid) is None
    with closing(sqlite3.connect(client.state.db.path)) as con:
        assert con.execute("select count(*) from catalog_source_jurisdictions").fetchone()[0] == 0
    assert client.delete(f"/api/catalog/sources/{sid}").status_code == 404


def test_h_custom_configuration_survives_a_restart(data_dir):
    with app(data_dir) as c:
        cat = c.post("/api/catalog/categories", json={"title": "Court gossip", "group": "judicial"}).json()
        sid = add(c, {**KRIMFUP, "category": cat["id"]})["id"]
        pid = c.post("/api/catalog/platforms", json={"name": "Examplegram", "url": "https://examplegram.example",
                                                     "category": "social"}).json()["id"]
    with app(data_dir) as c:
        assert in_sources(c, sid)["_group_title"] == "Court gossip"
        assert in_jurisdictions(c, "SE", sid)["group"] == "judicial"
        assert any(p["id"] == pid for p in c.get("/api/catalog/platforms").json())


# --------------------------------------------------------------------------- I–J: the audit

def test_i_audit_reads_the_current_catalog_and_changes_nothing(client):
    a0 = client.get("/api/catalog/audit/SE").json()
    assert a0["read_only"] and a0["read_from"]["your_sources"] == 0
    judicial0 = next(g for g in a0["coverage"] if g["group"] == "judicial")
    add(client, KRIMFUP)
    version = client.get("/api/meta").json()["catalog_version"]
    a1 = client.get("/api/catalog/audit/SE").json()
    assert a1["read_from"]["your_sources"] == 1 and a1["read_from"]["catalog_version"] == version
    assert next(g for g in a1["coverage"] if g["group"] == "judicial")["user"] == judicial0["user"] + 1
    assert client.get("/api/meta").json()["catalog_version"] == version          # reading changed nothing
    # nothing already in the catalog is recommended, and every recommendation is evidence-based and unverified
    known = {CAT.url_key(s["url"]) for s in a1["sources"]}
    for r in a1["recommendations"]:
        assert CAT.url_key(r["url"]) not in known
        assert r["status"] == "Not verified" and r["provenance"]["dataset"] and r["confidence"] in ("low", "medium", "high")
        assert r["confidence_reasons"] and r["prefill"]["jurisdictions"] == ["SE"]
    # an entry duplicating a built-in source is reported
    add(client, {"title": "Bolagsverket again", "url": "https://www.bolagsverket.se/", "category": "companies",
                 "jurisdictions": ["SE"], "allow_duplicate": True})
    dups = client.get("/api/catalog/audit/SE").json()["duplicates"]
    assert any(d["reason"] == "Same address" and {i["origin"] for i in d["items"]} == {"user", "system"} for d in dups)


def test_j_accepting_a_recommendation_saves_it_everywhere_and_it_is_no_longer_recommended(data_dir):
    with app(data_dir) as c:
        a = c.get("/api/catalog/audit/SE").json()
        assert a["recommendations"], "Sweden has dataset entries that are not in the catalog yet"
        rec = a["recommendations"][0]
        src = add(c, rec["prefill"])
        assert in_sources(c, src["id"]) and in_matrix(c, src["id"]) and in_jurisdictions(c, "SE", src["id"])
        again = c.get("/api/catalog/audit/SE").json()
        assert all(r["id"] != rec["id"] for r in again["recommendations"])
        # ignoring one moves it out of the list without adding anything
        other = again["recommendations"][0]
        c.post("/api/catalog/audit/SE/ignore", json={"item": other["id"]})
        a2 = c.get("/api/catalog/audit/SE").json()
        assert all(r["id"] != other["id"] for r in a2["recommendations"]) and any(r["id"] == other["id"] for r in a2["ignored"])
        assert len(c.get("/api/catalog/sources").json()) == 1
    with app(data_dir) as c:
        assert in_jurisdictions(c, "SE", src["id"])["title"] == rec["prefill"]["title"][:200]


# --------------------------------------------------------------------------- K–N: the one reset

def _mess_up(c):
    cat = c.post("/api/catalog/categories", json={"title": "Court gossip", "group": "judicial"}).json()
    plat = c.post("/api/catalog/platforms", json={"name": "Examplegram", "url": "https://examplegram.example",
                                                  "category": "social", "countries": ["SE"],
                                                  "profile_url": "https://examplegram.example/u/{username}"}).json()
    add(c, {**KRIMFUP, "category": cat["id"], "platforms": [plat["id"]]})
    add(c, LEXBASE)
    add(c, {"title": "Swedish register mirror", "url": "https://mirror.example.se", "category": "companies",
            "jurisdictions": ["SE", "NO"], "connector": "gleif", "source_type": "dataset"})
    c.put("/api/sources/github", json={"enabled": False})
    c.put("/api/sources/github/credentials", json={"values": {"GITHUB_TOKEN": "ghp_keepme"}})
    c.put("/api/settings", json={"values": {"default_depth": "3"}})
    c.post("/api/catalog/audit/SE/ignore", json={"item": "wd:SE:Q1:example.se"})
    c.state.db.set_setting("discovery:SE", {"ran_at": "2026-01-01T00:00:00+00:00", "new_sources": []})
    inv = c.post("/api/investigations", json={"text": "jdoe92", "mode": "manual"}).json()["id"]
    return inv


def test_k_l_m_n_reset_removes_only_user_configuration(data_dir, tmp_path):
    with app(tmp_path / "fresh") as fresh:                # what a new installation shows
        defaults = {"sources": fresh.get("/api/sources").json()["total"],
                    "jurisdictions": fresh.get("/api/jurisdictions").json()["totals"],
                    "categories": {c["id"] for c in fresh.get("/api/catalog/categories").json()},
                    "SE": country_row(fresh, "SE"), "platforms": fresh.get("/api/meta").json()["platforms"]}
    with app(data_dir) as c:
        inv = _mess_up(c)
        summary = c.get("/api/catalog/reset").json()
        rm = summary["remove"]
        assert (rm["sources"], rm["links"], rm["categories"], rm["platforms"]) == (2, 1, 1, 1)
        assert rm["jurisdiction_links"] == 4 and rm["platform_links"] == 1 and rm["connector_links"] == 1
        assert rm["source_preferences"] == 1 and rm["audit_ignores"] == 1 and rm["discovery_results"] == 1
        assert "Krimfup" in summary["items"]["sources"] and "Lexbase" in summary["items"]["links"]
        assert c.post("/api/catalog/reset", json={"confirm": "yes"}).status_code == 400
        assert c.post("/api/catalog/reset", json={"confirm": "RESET"}).json()["ok"]
        # K: the custom records are gone from every view
        assert c.get("/api/catalog/sources").json() == [] and c.get("/api/catalog/platforms").json() == []
        assert c.get("/api/sources").json()["origins"]["user"] == 0
        assert all(r["origin"] == "system" for r in c.get("/api/coverage-matrix").json()["rows"])
        assert jurisdiction(c, "SE")["user_sources"] == 0
        assert c.get("/api/catalog/reset").json()["total"] == 0
        # what is kept: keys, settings, investigations; the switched-off connector is back to its default
        github = in_sources(c, "github")
        assert github["status"] == "available" and any(cr["is_set"] for cr in github["credentials"])
        assert c.get("/api/settings").json()["settings"][0]["value"] == "3"
        assert any(i["id"] == inv for i in c.get("/api/investigations").json())
    with app(data_dir) as c:                              # L: still gone after a restart
        assert c.get("/api/catalog/sources").json() == []
        with closing(sqlite3.connect(c.state.db.path)) as con:
            for table in ("catalog_sources", "catalog_source_jurisdictions", "catalog_source_platforms",
                          "catalog_categories", "catalog_platforms", "catalog_audit_ignores", "connectors"):
                assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
        # M: system defaults are exactly those of a fresh installation
        assert c.get("/api/sources").json()["total"] == defaults["sources"]
        assert c.get("/api/jurisdictions").json()["totals"] == defaults["jurisdictions"]
        assert {x["id"] for x in c.get("/api/catalog/categories").json()} == defaults["categories"]
        assert c.get("/api/meta").json()["platforms"] == defaults["platforms"]
        # N: the coverage matrix and the country rows are recomputed from the defaults
        assert country_row(c, "SE") == defaults["SE"]
        assert len(c.get("/api/coverage-matrix").json()["rows"]) == defaults["sources"]


def test_n_counts_agree_across_screens(client):
    _mess_up(client)
    sources = client.get("/api/sources").json()
    matrix = client.get("/api/coverage-matrix").json()["rows"]
    names = {s["name"] for g in sources["groups"] for s in g["sources"]}
    assert len(matrix) == sources["total"] == len(names) and {r["name"] for r in matrix} == names
    mine_se = [s for s in client.get("/api/catalog/sources").json() if "SE" in s["jurisdictions"]]
    assert jurisdiction(client, "SE")["user_sources"] == len(mine_se) == country_row(client, "SE")["custom_sources"]
    assert len(client.get("/api/global-coverage/SE").json()["custom"]) == len(mine_se)
    assert client.get("/api/catalog/audit/SE").json()["read_from"]["your_sources"] == len(mine_se)


# --------------------------------------------------------------------------- O: platforms

def test_o_platform_relationships_stay_consistent(client):
    p = client.post("/api/catalog/platforms", json={"name": "Examplegram", "url": "https://examplegram.example",
                                                    "category": "social", "countries": ["SE"],
                                                    "profile_url": "https://examplegram.example/u/{username}"}).json()
    sid = add(client, {**KRIMFUP, "platforms": [p["id"], "github"]})["id"]
    rows = client.get("/api/platforms?source=user").json()["rows"]
    row = next(r for r in rows if r["id"] == p["id"])
    assert row["origin"] == "user" and row["search_status"] == "manual" and row["user_sources"][0]["id"] == sid
    assert any(r["id"] == p["id"] for r in client.get("/api/global-coverage/SE").json()["platforms"])
    links = PLAT.manual_links("alice", client.state.catalog.snapshot().platform_objects)
    assert {"title": "Examplegram", "url": "https://examplegram.example/u/alice"}.items() <= next(
        x for x in links if x["platform"] == p["id"]).items()
    # a duplicate of a built-in platform is refused unless confirmed
    assert client.post("/api/catalog/platforms", json={"name": "GH", "url": "https://github.com",
                                                       "category": "developer"}).status_code == 409
    assert client.delete(f"/api/catalog/platforms/{p['id']}").json()["ok"]
    assert client.get("/api/catalog/sources").json()[0]["platforms"] == ["github"]     # no dangling link
    assert client.post("/api/catalog/sources", json={**LEXBASE, "platforms": ["no-such-platform"]}).status_code == 400


# --------------------------------------------------------------------------- duplicates, categories, security

def test_duplicates_are_detected_before_saving(client):
    first = add(client, KRIMFUP)
    r = client.post("/api/catalog/sources", json={**KRIMFUP, "title": "Krimfup.se", "url": "https://www.krimfup.se/"})
    assert r.status_code == 409
    dups = r.json()["detail"]["duplicates"]
    assert dups[0]["id"] == first["id"] and "Same address" in dups[0]["reasons"] and dups[0]["editable"]
    assert add(client, {**KRIMFUP, "url": "https://www.krimfup.se/", "allow_duplicate": True})["id"] != first["id"]
    official = client.post("/api/catalog/duplicates", json={"title": "Bolagsverket", "url": "https://bolagsverket.se",
                                                             "jurisdictions": ["SE"]}).json()["duplicates"]
    assert any(d["type"] == "official_source" and d["id"] == "se-bolagsverket" for d in official)
    conn = client.post("/api/catalog/duplicates", json={"title": "CourtListener"}).json()["duplicates"]
    assert any(d["type"] == "connector" and d["reasons"] == ["Same name"] for d in conn)
    # editing does not report the source itself, nor a duplicate the investigator already accepted …
    assert client.put(f"/api/catalog/sources/{first['id']}", json={**KRIMFUP, "notes": "edited"}).status_code == 200
    # … but an edit that creates a new one is caught
    lex = add(client, LEXBASE)
    assert client.put(f"/api/catalog/sources/{lex['id']}", json={**LEXBASE, "url": "https://krimfup.se"}).status_code == 409


def test_categories_are_data_and_protected(client):
    cat = client.post("/api/catalog/categories", json={"title": "Court gossip", "group": "judicial"}).json()
    assert cat["id"].startswith("ucat-")
    vocab = {c["id"]: c for c in client.get("/api/catalog/vocab").json()["categories"]}
    assert vocab[cat["id"]]["origin"] == "user" and vocab["legal_records"]["origin"] == "system"
    assert client.post("/api/catalog/categories", json={"title": "court GOSSIP"}).status_code == 400
    assert client.post("/api/catalog/categories", json={"title": "News"}).status_code == 400       # built-in name
    sid = add(client, {**KRIMFUP, "category": cat["id"]})["id"]
    assert client.delete(f"/api/catalog/categories/{cat['id']}").status_code == 409
    assert client.delete("/api/catalog/categories/legal_records").status_code == 400               # built-in id
    client.delete(f"/api/catalog/sources/{sid}")
    assert client.delete(f"/api/catalog/categories/{cat['id']}").json()["ok"]


def test_source_definitions_are_metadata_only(client):
    bad = [{**KRIMFUP, "command": "rm -rf /"}, {**KRIMFUP, "script": "fetch('x')"}]
    for body in bad:
        assert client.post("/api/catalog/sources", json=body).status_code == 422
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pw@example.com/", "ftp://example.com"):
        assert client.post("/api/catalog/sources", json={**KRIMFUP, "url": url}).status_code == 400, url
    assert client.post("/api/catalog/sources", json={**KRIMFUP, "connector": "no_such_connector"}).status_code == 400
    assert client.post("/api/catalog/sources", json={**KRIMFUP, "jurisdictions": ["XX"]}).status_code == 400
    assert client.post("/api/catalog/sources", json={**KRIMFUP, "inputs": ["shell"]}).status_code == 400
    # built-in sources cannot be edited or deleted through the catalog
    assert client.put("/api/catalog/sources/github", json=KRIMFUP).status_code == 400
    assert client.delete("/api/catalog/sources/user-nothing-000000").status_code == 404
    sid = add(client, {**KRIMFUP, "inputs": ["person", "organization"]})["id"]
    plan = client.post("/api/parse", json={"text": "Jane Doe"}).json()["plan"]
    assert all(p["name"] != sid for p in plan) and sid not in REG.REGISTRY      # never part of an investigation plan
    # the link check is one GET through the guarded client, on request
    checked = client.post(f"/api/catalog/sources/{sid}/check-link").json()
    assert checked["link_check"]["result"] == "ok"
    assert in_sources(client, sid)["health"]["status"] == "operational"


def test_linked_connector_drives_the_status(client):
    sid = add(client, {**KRIMFUP, "connector": "github"})["id"]
    item = in_sources(client, sid)
    assert item["automation"]["mode"] == "connector" and item["status"] == "available"
    client.put("/api/sources/github", json={"enabled": False})
    assert in_sources(client, sid)["status"] == "disabled"         # follows the connector everywhere
    assert in_jurisdictions(client, "SE", sid)["status"] == "disabled"


# --------------------------------------------------------------------------- migration

def test_country_page_sources_migrate_with_their_ids(tmp_path):
    db = Database(tmp_path, Vault.open(tmp_path))
    with db.tx() as c:
        c.execute("INSERT INTO source_capabilities VALUES(?,?,?,?,?,?,1,?)",
                  ("custom-se-0123456789", "SE", "judgments", "free_web", 1, None,
                   json.dumps({"title": "Old court site", "url": "https://court.example.se/", "notes": "kept",
                               "added_at": "2026-01-02T00:00:00+00:00"})))
        c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db.close()
    for _ in range(2):                                    # the migration runs once and is idempotent
        db = Database(tmp_path, Vault.open(tmp_path))
        snap = CAT.store_for(db).snapshot()
        assert [s["id"] for s in snap.sources] == ["custom-se-0123456789"]
        s = snap.sources[0]
        assert (s["title"], s["category"], s["kind"], s["jurisdictions"], s["notes"], s["name_search"]) == \
            ("Old court site", "legal_records", "judgments", ["SE"], "kept", True)
        assert s["created_at"] == "2026-01-02T00:00:00+00:00"
        with closing(sqlite3.connect(db.path)) as con:
            assert con.execute("select count(*) from source_capabilities where custom=1").fetchone()[0] == 0
            assert con.execute("select value from meta where key='schema_version'").fetchone()[0] == "3"
        db.close()


# --------------------------------------------------------------------------- provider back-off

@register
class TRefuser(Connector):
    manifest = Manifest(name="t_refuser", title="Test refuser", category="adapters", description="test",
                        website="https://refuser.example", max_concurrency=1,
                        disclosure=Disclosure(("refuser.example",), "phone number"))

    @transform(inputs=("phone",), outputs=(), title="Test: always refused")
    async def lookup(self, entity, ctx):
        raise Blocked("HTTP 403")


def test_engine_backs_off_from_a_provider_that_refuses(db, settings):
    eng = engine_with(db, settings, {})
    phones = "\n".join(f"+4670123456{i}" for i in range(8))
    inv = eng.create(parse_input(phones), depth="1", options={"categories": ["adapters"]})

    async def go():
        rt = await eng.start(inv)
        await rt.loop_task
        return rt
    rt = run(go())
    refused = [r for r in rt.runs.values() if r.connector == "t_refuser"]
    assert len(refused) == 3 and all(r.status == "blocked" for r in refused)      # stopped after 3 refusals in a row
    skipped = [t for t in rt.tasks.values() if t.transform == "t_refuser.lookup" and t.status == "skipped"]
    assert len(skipped) == 5 and "stopped sending" in skipped[0].note
    row = next(r for r in eng.coverage(inv)["connectors"] if r["connector"] == "t_refuser")
    assert "stopped sending" in row["note"] and row["skips"]["provider_refused"] == 5


# --------------------------------------------------------------------------- export / import (backup before reset)

def _export(c):
    r = c.get("/api/catalog/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    return r.json()


def test_export_reset_import_restores_the_catalog(data_dir):
    with app(data_dir) as c:
        _mess_up(c)
        before = {"sources": c.get("/api/catalog/sources").json(), "platforms": c.get("/api/catalog/platforms").json(),
                  "categories": [x for x in c.get("/api/catalog/categories").json() if x["origin"] == "user"],
                  "SE": country_row(c, "SE")}
        doc = _export(c)
        assert doc["format"] == "sextant-catalog" and doc["format_version"] == 1
        assert doc["counts"] == {"sources": 2, "links": 1, "categories": 1, "platforms": 1, "audit_ignores": 1,
                                 "source_preferences": 1}
        assert "ghp_keepme" not in json.dumps(doc)                      # API keys are never exported
        assert c.post("/api/catalog/reset", json={"confirm": "RESET"}).json()["ok"]
        version = c.get("/api/meta").json()["catalog_version"]
        preview = c.post("/api/catalog/import", json={"document": doc, "restore_preferences": True}).json()
        assert preview["dry_run"] and not preview["applied"]           # a preview unless asked otherwise
        assert (preview["sources"]["add"], preview["categories"]["add"], preview["platforms"]["add"]) == (3, 1, 1)
        assert preview["source_preferences"]["apply"] == 1 and preview["audit_ignores"]["add"] == 1
        assert c.get("/api/meta").json()["catalog_version"] == version and c.get("/api/catalog/sources").json() == []
        done = c.post("/api/catalog/import", json={"document": doc, "dry_run": False, "restore_preferences": True}).json()
        assert done["applied"] and done["changes"] == 7
    with app(data_dir) as c:                                         # restored, and still there after a restart
        assert c.get("/api/catalog/sources").json() == before["sources"]          # same ids, relationships, dates
        assert c.get("/api/catalog/platforms").json() == before["platforms"]
        assert [x for x in c.get("/api/catalog/categories").json() if x["origin"] == "user"] == before["categories"]
        assert country_row(c, "SE") == before["SE"]
        assert in_sources(c, "github")["status"] == "disabled"
        assert c.get("/api/catalog/reset").json()["remove"]["audit_ignores"] == 1
        again = c.post("/api/catalog/import", json={"document": doc, "dry_run": False, "restore_preferences": True}).json()
        assert again["changes"] == 0 and not again["applied"] and again["sources"]["skip"] == 3   # idempotent


def test_import_treats_the_file_as_untrusted_and_skips_what_exists(client):
    src = add(client, KRIMFUP)
    doc = _export(client)
    bad = json.loads(json.dumps(doc))
    bad["categories"] = [{"id": "ucat-news", "title": "news", "group": "news"}]       # the built-in "News"
    bad["sources"] += [
        {"id": "user-evil-000001", "title": "Evil", "url": "javascript:alert(1)", "category": "legal_records"},
        {"id": "user-cmd-000002", "title": "Has a command", "url": "https://cmd.example", "category": "legal_records",
         "command": "rm -rf /", "connector": "gone_connector", "jurisdictions": ["SE", "XX"], "platforms": ["nope"]},
        {"title": "Copy with a new id", "url": "https://www.krimfup.se/", "category": "legal_records"},
        {"title": "In a merged category", "url": "https://cat.example", "category": "ucat-news"},
    ]
    r = client.post("/api/catalog/import", json={"document": bad, "dry_run": False}).json()
    assert r["applied"] and r["sources"]["add"] == 2
    reasons = {s["title"]: s["reason"] for s in r["skipped"]}
    assert reasons["Krimfup"] == "already in your catalog"
    assert "http" in reasons["Evil"]                                     # refused like in the form
    assert reasons["Copy with a new id"].startswith("same address as your source")
    assert reasons["news"].startswith("merged into the existing category")
    mine = {s["title"]: s for s in client.get("/api/catalog/sources").json()}
    cmd = mine["Has a command"]
    assert cmd["connector"] == "" and cmd["jurisdictions"] == ["SE"] and cmd["platforms"] == [] and "command" not in cmd
    assert mine["In a merged category"]["category"] == "news"
    assert r["dropped_fields"] >= 1 and any("gone_connector" in w for w in r["warnings"])
    # overwrite replaces records with the same id
    doc["sources"][0]["title"] = "Krimfup (renamed)"
    r = client.post("/api/catalog/import", json={"document": doc, "dry_run": False, "overwrite": True}).json()
    assert r["sources"]["update"] == 1 and in_sources(client, src["id"])["title"] == "Krimfup (renamed)"
    assert client.post("/api/catalog/import", json={"document": {"format": "other"}}).status_code == 400
    assert client.post("/api/catalog/import", json={"document": {**doc, "format_version": 99}}).status_code == 400


# --------------------------------------------------------------------------- user platforms in investigation input

EXAMPLEGRAM = {"name": "Examplegram", "url": "https://examplegram.example", "category": "social",
               "profile_url": "https://examplegram.example/u/{username}"}


def test_pasted_profile_url_of_a_user_platform_becomes_an_account(client):
    before = client.post("/api/parse", json={"text": "https://www.examplegram.example/u/Alice"}).json()["input"]["seeds"]
    assert {s["type"] for s in before} == {"website", "domain"}                 # unknown site: just a website
    pid = client.post("/api/catalog/platforms", json=EXAMPLEGRAM).json()["id"]
    seeds = client.post("/api/parse", json={"text": "https://www.examplegram.example/u/Alice"}).json()["input"]["seeds"]
    acc = next(s for s in seeds if s["type"] == "account")
    assert acc["value"] == f"{pid}:alice" and acc["label"] == "Examplegram: Alice"
    assert any(s["type"] == "username" and s["value"] == "alice" for s in seeds)
    assert not any(s["type"] == "domain" for s in seeds)
    # the investigation starts from that account, and nothing runs against the platform itself
    inv = client.post("/api/investigations", json={"text": "https://examplegram.example/u/Alice", "mode": "manual"}).json()["id"]
    graph = client.get(f"/api/investigations/{inv}/graph").json()
    node = next(e for e in graph["entities"] if e["type"] == "account")
    assert node["platform"] == pid and node["status"] == "seed"
    menu = client.get(f"/api/investigations/{inv}/entities/{node['id']}/transforms").json()
    assert not any(t["applicable"] and t["auto"] and t["name"].endswith("account_profile") for t in menu)
    # the recognition does not leak out of the request
    assert PLAT.parse_profile_url("https://examplegram.example/u/alice") is None


def test_built_in_platforms_win_over_a_user_platform_on_the_same_site(client):
    r = client.post("/api/catalog/platforms", json={"name": "My GitHub", "url": "https://github.com", "category": "developer",
                                                   "profile_url": "https://github.com/{username}", "allow_duplicate": True})
    assert r.status_code == 201
    seeds = client.post("/api/parse", json={"text": "https://github.com/jdoe92"}).json()["input"]["seeds"]
    assert next(s for s in seeds if s["type"] == "account")["value"] == "github:jdoe92"


def test_profile_links_found_during_an_investigation_are_recognised(db, settings):
    from sextant.connectors._common import add_link_target
    from sextant.registry.platforms import user_platforms_in_scope

    class TLinker(Connector):
        manifest = Manifest(name="t_linker", title="Test linker", category="adapters", description="test",
                            website="https://linker.example", disclosure=Disclosure(("linker.example",), "AS number"))

        @transform(inputs=("asn",), outputs=("account", "domain"), title="Test: page with a profile link")
        async def lookup(self, entity, ctx):
            src = ctx.out.source("https://linker.example/page", title="Test page")
            add_link_target(ctx.out, entity.key, "https://examplegram.example/u/alice", src)

    p = CAT.store_for(db).put_platform(EXAMPLEGRAM)
    register(TLinker)
    try:
        eng = engine_with(db, settings, {})
        inv = eng.create(parse_input("AS3333"), depth="1", options={"categories": ["adapters"]})

        async def go():
            rt = await eng.start(inv)
            await rt.loop_task
            return rt
        rt = run(go())
        acc = rt.graph.get(f"account:{p['id']}:alice")
        assert acc is not None and acc.get("platform_title") == "Examplegram"       # an account, not a domain
        assert rt.graph.get("domain:examplegram.example") is None
        with user_platforms_in_scope(CAT.store_for(db).snapshot().platform_objects):
            assert PLAT.parse_profile_url("https://examplegram.example/u/bob")[1] == "bob"
    finally:
        REG.REGISTRY.pop("t_linker", None)
        REG.TRANSFORMS.pop("t_linker.lookup", None)


def test_platform_rows_say_whether_pasted_links_are_recognised(client):
    a = client.post("/api/catalog/platforms", json=EXAMPLEGRAM).json()["id"]
    b = client.post("/api/catalog/platforms", json={"name": "Old forum", "url": "https://forum.example", "category": "forums",
                                                   "profile_url": "https://forum.example/member.php?u={username}"}).json()["id"]
    c = client.post("/api/catalog/platforms", json={"name": "No profiles", "url": "https://noprofiles.example",
                                                   "category": "other"}).json()["id"]
    rows = {r["id"]: r for r in client.get("/api/platforms?source=user").json()["rows"]}
    # templates with ?parameters are recognised too (host + path + the parameter that carries the username)
    assert (rows[a]["recognizable"], rows[b]["recognizable"], rows[c]["recognizable"]) == (True, True, False)
    seeds = client.post("/api/parse", json={"text": "https://forum.example/member.php?u=alice"}).json()["input"]["seeds"]
    assert any(s["type"] == "account" and "alice" in s["label"] for s in seeds)
    seeds = client.post("/api/parse", json={"text": "https://forum.example/other.php?u=alice"}).json()["input"]["seeds"]
    assert not any(s["type"] == "account" for s in seeds)
