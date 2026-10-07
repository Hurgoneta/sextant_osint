"""Local storage is encrypted at rest; the local API only answers its own UI; exports keep provenance."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import engine_with, run
from sextant.api.app import build_state, create_app
from sextant.engine.input import parse_input
from sextant.reporting.report import build_report
from sextant.sdk.testing import mock_http

SECRET_VALUES = ("jane.secret@example.org", "Quinta Placeholder", "ghp_supersecrettoken")


def all_bytes(data_dir):
    return b"".join(p.read_bytes() for p in data_dir.rglob("*") if p.is_file())


def test_investigation_data_and_credentials_are_encrypted_at_rest(db, settings, tmp_path):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("name: Quinta Placeholder\njane.secret@example.org\nborn: 1980"), depth="1", options={})
    eng.runtime(inv)
    settings.set_credential("GITHUB_TOKEN", "ghp_supersecrettoken")
    db.close()
    raw = all_bytes(tmp_path)
    for value in SECRET_VALUES:
        assert value.encode() not in raw, value
    con = sqlite3.connect(tmp_path / db.path.name)
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert {"investigations", "entities", "entity_properties", "relationships", "evidence", "sources", "connectors",
            "transform_runs", "search_tasks", "observations", "confidence_scores", "api_credentials", "jurisdictions",
            "source_capabilities"} <= tables


def test_credentials_round_trip_and_never_listed(settings):
    settings.set_credential("GITHUB_TOKEN", "ghp_x")
    assert settings.credential("GITHUB_TOKEN") == "ghp_x" and settings.credential_source("GITHUB_TOKEN").startswith("stored")
    settings.set_credential("GITHUB_TOKEN", None)
    assert settings.credential("GITHUB_TOKEN") is None


@pytest.fixture
def client(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html>ui</html>")
    state = build_state(tmp_path / "data", tmp_path / "ui", "tok", 8765, http_factory=lambda **kw: mock_http({}, **kw))
    with TestClient(create_app(state), base_url="http://127.0.0.1:8765") as c:
        c.headers["X-Sextant-Token"] = "tok"
        yield c
    state.db.close()


def test_api_guard(client):
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/meta", headers={"X-Sextant-Token": "wrong"}).status_code == 401
    assert client.get("/api/meta", headers={"Host": "evil.example:8765"}).status_code == 421
    assert client.get("/api/meta", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.get("/")
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_api_investigation_flow_and_exports(client):
    plan = client.post("/api/parse", json={"text": "Jane Doe\njdoe92@example.org"}).json()
    assert plan["input"]["target_name"] == "Jane Doe" and plan["plan"]
    assert all("sends" in p and "destinations" in p for p in plan["plan"])          # disclosure shown before running
    r = client.post("/api/investigations", json={"text": "Jane Doe\njdoe92@example.org\nborn: 1971", "mode": "manual",
                                                 "depth": "2"})
    assert r.status_code == 201
    inv = r.json()["id"]
    graph = client.get(f"/api/investigations/{inv}/graph").json()
    labels = {n["label"] for n in graph["entities"]}
    assert {"Jane Doe", "jdoe92@example.org"} <= labels
    email = next(n for n in graph["entities"] if n["type"] == "email")
    menu = client.get(f"/api/investigations/{inv}/entities/{email['id']}/transforms").json()
    statuses = {t["status"] for t in menu}
    assert "available" in statuses and "needs_credentials" in statuses
    cov = client.get(f"/api/investigations/{inv}/coverage").json()
    assert "summary" in cov and "never proves" in cov["disclaimer"]

    j = client.get(f"/api/investigations/{inv}/export/json")
    doc = j.json()
    assert "1971" not in j.text                                                       # local-only, never exported
    assert all(o["source"] for o in doc["observations"])
    html = client.get(f"/api/investigations/{inv}/export/html").text
    assert "Appendix — evidence sources" in html and "never proves" in html and "16. Data-source coverage" in html
    csv_text = client.get(f"/api/investigations/{inv}/export/csv").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("id,type,label")
    assert client.delete(f"/api/investigations/{inv}").status_code == 200
    assert client.get(f"/api/investigations/{inv}").status_code == 404


def test_api_rejects_bad_input(client):
    assert client.post("/api/investigations", json={"text": "", "mode": "manual"}).status_code in (400, 422)
    assert client.get("/api/investigations/../../etc").status_code in (400, 404)
    assert client.put("/api/sources/github/credentials", json={"values": {"SHODAN_API_KEY": "x"}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"nope": 1}}).status_code == 400
    assert client.post("/api/data/wipe", json={"confirm": "yes"}).status_code == 400


def test_sources_catalog_reports_honest_states(client):
    data = client.get("/api/sources").json()
    by_name = {s["name"]: s for g in data["groups"] for s in g["sources"]}
    assert by_name["hibp"]["status"] == "needs_subscription"
    assert by_name["shodan"]["status"] == "needs_subscription"                     # paid membership
    assert by_name["virustotal"]["status"] == "needs_credentials"                  # free key
    assert by_name["lexisnexis"]["status"] == "catalog_only" and by_name["dehashed"]["status"] == "excluded"
    assert by_name["linkedin"]["status"] == "manual"
    r = client.put("/api/sources/github/credentials", json={"values": {"GITHUB_TOKEN": "ghp_test"}}).json()
    assert r["status"] == "available" and "ghp_test" not in json.dumps(r)


def test_csv_export_is_formula_safe(db, settings):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("org: =HYPERLINK(\"http://x\")"), depth="1", options={})
    content, _ = build_report(eng.runtime(inv), eng.coverage(inv), [], "csv")
    assert ',=HYPERLINK' not in content.decode("utf-8-sig") and "'=HYPERLINK" in content.decode("utf-8-sig")


def test_deleting_an_investigation_leaves_nothing_behind(db, settings, tmp_path):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("Jane Doe\njdoe92@example.org"), depth="1", options={})
    db.cache_set(inv, "k", {"v": 1}, 60)
    db.log_decision(inv, "note", {"x": 1})
    run(eng.delete(inv))
    con = sqlite3.connect(db.path)
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tables:
        cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
        if "investigation_id" in cols:
            assert con.execute(f"select count(*) from {t} where investigation_id=?", (inv,)).fetchone()[0] == 0, t
    con.close()
