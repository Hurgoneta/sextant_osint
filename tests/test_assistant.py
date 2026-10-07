"""The investigation assistant: plans from the registry, candidate handles as hypotheses, evidence-based verdicts,
next best actions, an AI chat that can only act through tools, compliant anti-bot handling. Synthetic people only."""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import engine_with, run
from sextant.api.app import build_state, create_app
from sextant.assistant import analysis as A
from sextant.assistant import copilot as AC
from sextant.assistant import names as N
from sextant.assistant import providers as AIP
from sextant.connectors._common import add_account
from sextant.engine.input import parse_input
from sextant.model import Entity, Reason
from sextant.registry import connectors as REG
from sextant.registry import jurisdictions as J
from sextant.sdk import Connector, Disclosure, Manifest, register, transform
from sextant.sdk.errors import NotFound, VerificationRequired
from sextant.sdk.http import HttpResponse, is_verification_challenge
from sextant.sdk.testing import mock_http, run_transform, view


@contextmanager
def app(data_dir, routes=None):
    ui = data_dir.parent / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<html>ui</html>")
    state = build_state(data_dir, ui, "tok", 8765, http_factory=lambda **kw: mock_http(routes or {}, **kw))
    with TestClient(create_app(state), base_url="http://127.0.0.1:8765") as c:
        c.headers["X-Sextant-Token"] = "tok"
        c.state = state
        yield c
    state.db.close()


@pytest.fixture
def client(tmp_path):
    with app(tmp_path / "data") as c:
        yield c


@contextmanager
def registered(*classes):
    """Test connectors registered for one test only (removed afterwards so other tests see the real catalog)."""
    for cls in classes:
        register(cls)
    try:
        yield
    finally:
        for cls in classes:
            REG.REGISTRY.pop(cls.manifest.name, None)
            for t in [n for n in REG.TRANSFORMS if n.startswith(cls.manifest.name + ".")]:
                REG.TRANSFORMS.pop(t)


def only(*names):
    """Investigation options that leave just these connectors on (plus the local generator)."""
    keep = set(names) | {"username_guesses"}
    return {"categories": ["adapters", "usernames"],
            "connectors_disabled": [n for n, c in REG.REGISTRY.items()
                                    if c.manifest.category in ("adapters", "usernames") and n not in keep]}


async def finish(eng, inv):
    rt = eng.runtime(inv)
    if rt.loop_task:
        await rt.loop_task
    return rt


# --------------------------------------------------------------------------- 1–3 names and candidates

def test_1_full_name_plan_uses_the_registry_and_warns_about_namesakes(client):
    p = client.post("/api/assistant/plan", json={"text": "name: Example Person", "mode": "balanced"}).json()
    assert p["mode"]["id"] == "balanced" and p["depth"] == "2"
    assert p["name_analysis"]["names"] == ["Example Person"]
    search = [s for s in p["steps"] if s["phase"] == "search"]
    assert search and all(src["name"] in REG.REGISTRY for st in search for src in st["sources"])   # nothing invented
    person = next(s for s in search if s["input"] == "person")
    registers = [src for src in person["sources"] if src["category"] in ("legal_records", "sanctions", "companies")]
    assert registers and all("namesakes" in src["reason"] for src in registers)
    assert p["jurisdiction_note"]                                           # nothing inferred: says so


def test_2_name_readings_and_diacritics():
    assert [r.title for r in N.readings("Example Person")][0] == "first “example”, last “person”"
    three = N.readings("Ana Maria Example")
    assert len(three) == 3 and any("family name first" in r.title for r in three)
    assert N.candidates(["Éva Példa"], 3)[0].handle in ("eva.pelda", "evapelda", "eva_pelda")
    assert N.readings("Zorbanx") == [] and N.candidates(["Zorbanx"]) == []


def test_3_candidates_are_labelled_hypotheses_and_never_use_the_birth_year(client):
    p = client.post("/api/assistant/plan", json={"text": "name: Example Person\nborn: 1990"}).json()
    cands = p["candidates"]
    assert cands and all(c["status"] == "Generated candidate · Not verified" and c["kind"] == "hypothesis" for c in cands)
    assert all("1990" not in c["handle"] for c in cands)                  # local-only data never leaves in a handle
    assert {"example.person", "examplepers0n"} & {c["handle"] for c in cands} == {"example.person"}
    assert all(c["platforms_valid_count"] >= 0 and "Pattern" in c["explanation"] for c in cands)
    quick = client.post("/api/assistant/plan", json={"text": "name: Example Person", "mode": "quick"}).json()
    assert quick["candidates_testable"] == 0 and quick["candidates"]


# --------------------------------------------------------------------------- 4 platform discovery with generated handles

class THandles(Connector):
    manifest = Manifest(name="t_handles", title="Test handle checker", category="adapters", description="test",
                        website="https://handles.example", disclosure=Disclosure(("handles.example",), "username"))

    @transform(inputs=("username",), outputs=("account",), title="Test: does the handle exist")
    async def lookup(self, entity, ctx):
        src = ctx.out.source(f"https://handles.example/{entity.display}", title="Test handles")
        if entity.value != "example.person":
            ctx.out.check("handles", "not_found", platform_title="Handles")
            raise NotFound("no such handle")
        acc = add_account(ctx.out, src, platform="handles", username=entity.display, link_from=entity.key,
                          profile={"display_name": "Example Person"}, platform_title="Handles")
        ctx.out.check("handles", "found", url=f"https://handles.example/{entity.display}", account=acc,
                      platform_title="Handles")


def test_4_generated_handles_are_tested_only_on_approval_and_stay_leads(db, settings):
    with registered(THandles):
        eng = engine_with(db, settings, {})
        inv = eng.create(parse_input("name: Example Person"), depth="2", options=only("t_handles"))
        rt = eng.runtime(inv)
        assert not [u for u in rt.graph.of_type("username")]               # nothing generated before approval
        acts = {a["id"]: a for a in A.next_actions(rt, eng, settings)}
        assert "test_candidates" in acts and acts["test_candidates"]["count"] == 4

        async def go():
            res = await AC.run_action(eng, settings, db, inv, "test_candidates")
            return res, await finish(eng, inv)
        res, rt = run(go())
        assert res["ok"] and "username_guesses.generate" in res["result"]["queued"]
        hyp = [u for u in rt.graph.of_type("username") if u.get("hypothesis")]
        assert len(hyp) == 4
        rows = [r for r in A.platform_table(rt.graph) if r["platform"] == "Handles"]
        found = next(r for r in rows if r["candidate"] == "example.person")
        assert found["status"] == "Found (generated handle)" and found["candidate_origin"].startswith("Generated")
        assert any(r["check"] == "not_found" for r in rows)
        acc = next(a for a in rt.graph.of_type("account"))
        verdict = A.candidate_row(rt.graph, acc)
        assert verdict["found_under_generated_handle"] and verdict["verdict"] == "insufficient"   # a lead, not a match
        assert db.assistant_log(inv, ("action",))[0]["kind"] == "action"


# --------------------------------------------------------------------------- 5–8 verdicts and false positives

def ent(type_, value, reasons, *, tier="", confidence=0.0, status="candidate"):
    e = Entity(type=type_, value=value, label=value, id=value.replace(":", "_"), status=status, confidence=confidence,
               tier=tier)
    e.reasons = [Reason(d, w, f) for d, w, f in reasons]
    return e


def test_5_to_8_confidence_needs_independent_signals():
    name_only = ent("account", "x:a", [("Same full name", 1.5, "name_full"), ("Same city", 0.8, "city")],
                    tier="medium", confidence=0.55)
    a = A.assess(name_only)
    assert a["verdict"] == "insufficient" and a["name_only"] and "Same name only" in a["note"]
    one = ent("account", "x:b", [("Profile shows the target's email", 4.0, "email"), ("Same name", 1.5, "name_full")],
              tier="high", confidence=0.9)
    assert A.assess(one)["verdict"] == "likely" and "second, independent" in A.assess(one)["note"]
    two = ent("account", "x:c", [("Email", 4.0, "email"), ("Links to the target's GitHub", 2.5, "link_to_anchor"),
                                 ("Different occupation", -0.3, "occupation_diff")], tier="high", confidence=0.95)
    s = A.assess(two)
    assert s["verdict"] == "strong" and s["contradictions"][0]["text"] == "Different occupation"
    assert A.assess(ent("account", "x:d", [], status="confirmed"))["verdict"] == "confirmed"
    assert A.assess(ent("account", "x:e", [("Same handle", 1.7, "username")], tier="medium"), generated=True)["verdict"] \
        == "insufficient"                                               # a generated handle match is circular


def test_6_identities_group_cross_linked_profiles_with_the_reason():
    from sextant.model import Graph, RelEvidence, Source
    g = Graph(lambda k: k.replace(":", "_")[:40])
    g.add_source(Source(id="s1", connector="t", kind="api", url="https://a.example"))
    a, _ = g.ensure_entity("account", "alpha:example_handle", "Alpha: example_handle")
    b, _ = g.ensure_entity("account", "beta:example_handle", "Beta: example_handle")
    c, _ = g.ensure_entity("account", "gamma:someone", "Gamma: someone")
    for e, u in ((a, "example_handle"), (b, "example_handle"), (c, "someone")):
        e.props["username"] = [{"v": u, "k": u, "conf": 1.0, "obs": []}]
    g.add_relationship(a.key, b.key, "LINKED_TO", RelEvidence(source="s1", description="profile links to the other"))
    groups = A.identities(g)
    pair = next(gr for gr in groups if len(gr["members"]) == 2)
    assert {m["label"] for m in pair["members"]} == {"Alpha: example_handle", "Beta: example_handle"}
    assert any("links to the other" in w for w in pair["grouped_because"]) and any("same handle" in w for w in
                                                                                    pair["grouped_because"])
    assert len(groups) == 2 and groups[0]["letter"] == "A"


# --------------------------------------------------------------------------- 9–10 jurisdiction and cost

def test_9_jurisdictions_are_inferred_or_chosen_and_sources_exist(client):
    p = client.post("/api/assistant/plan", json={"text": "Jane Doe, location: Stockholm, Sweden"}).json()
    se = next(j for j in p["jurisdictions"] if j["code"] == "SE")
    assert se["status"] == "inferred — not confirmed"
    known = {s.title for s in J.jurisdictions()["SE"].sources} | {s.title for _b, s in J.bloc_sources("SE")}
    assert se["manual"] and all(s["title"] in known for s in se["manual"])           # only the registry's sources
    assert {g["group"] for g in se["coverage"]} >= {"Corporate", "Judicial / legal"}
    chosen = client.post("/api/assistant/plan", json={"text": "Jane Doe", "countries": ["SE"]}).json()
    assert chosen["jurisdictions"][0]["status"] == "selected by you"


def test_10_paid_sources_follow_the_settings(client, db, settings):
    client.put("/api/sources/hibp/credentials", json={"values": {"HIBP_API_KEY": "k"}})
    plan = lambda: client.post("/api/assistant/plan", json={"text": "jane@example.org"}).json()  # noqa: E731
    used = {s["name"] for st in plan()["steps"] if st["phase"] == "search" for s in st["sources"]}
    assert "hibp" in used
    client.put("/api/settings", json={"values": {"allow_paid_sources": False}})
    p = plan()
    assert "hibp" not in {s["name"] for st in p["steps"] if st["phase"] == "search" for s in st["sources"]}
    off = next(s for s in p["not_used"] if s["name"] == "hibp")
    assert off["note"] == "Paid sources are switched off in Settings" and p["cost"]["allow_paid"] is False
    # the cost cap counts estimated spend per investigation
    settings.set("max_paid_cost_usd", 0.05)
    eng = engine_with(db, settings, {})
    rt = eng.runtime(eng.create(parse_input("jane@example.org"), depth="1", options={}))
    m = Manifest(name="t_paid", title="Paid", category="email", description="t", access="paid", cost_per_call_usd=0.04,
                 website="https://p.example", disclosure=Disclosure(("p.example",), "email"),
                 credentials=())
    assert eng._over_cost_cap(rt, m) is None
    rt.calls["hibp"] = 0
    rt.calls["t_paid"] = 1
    REG.REGISTRY["t_paid"] = type("C", (), {"manifest": m})()
    try:
        assert "limit of $0.05" in eng._over_cost_cap(rt, m)
    finally:
        REG.REGISTRY.pop("t_paid")


# --------------------------------------------------------------------------- 11 AI provider configuration and keys

def test_11_ai_key_is_stored_encrypted_and_never_returned(client):
    c = client.get("/api/assistant/provider").json()
    assert c["provider"] == "none" and not c["ready"] and c["problem"] == ""
    client.put("/api/settings", json={"values": {"ai_provider": "openai_compatible"}})
    assert "endpoint" in client.get("/api/assistant/provider").json()["problem"]
    client.put("/api/settings", json={"values": {"ai_base_url": "http://127.0.0.1:11434/v1", "ai_model": "local-model"}})
    r = client.put("/api/assistant/key", json={"value": "sk-supersecret-test-key"}).json()
    assert r["key_set"] and r["ready"] and r["local"] and "supersecret" not in json.dumps(r)
    client.state.db.close()
    raw = b"".join(p.read_bytes() for p in client.state.data_dir.rglob("*") if p.is_file())
    assert b"sk-supersecret-test-key" not in raw
    client.state.open()
    client.put("/api/settings", json={"values": {"ai_provider": "anthropic", "ai_base_url": "", "ai_model": ""}})
    c = client.get("/api/assistant/provider").json()
    assert c["destination"] == "api.anthropic.com" and c["model"] == "claude-opus-5-5"
    if not c["sdk_installed"]:
        assert "pip install anthropic" in c["problem"]


# --------------------------------------------------------------------------- 12 chat through tools, with memory

class FakeProvider:
    """Scripted model: plays back replies, records what it was sent (to check the transcript is append-only)."""
    id = "fake"
    model = "fake-model"
    destination = "fake.example"

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def user_message(self, text):
        return {"role": "user", "content": text}

    def tool_results(self, results):
        return [{"role": "tool", "id": c.id, "content": out} for c, out, _err in results]

    async def complete(self, system, transcript, tools):
        self.seen.append(list(transcript))
        assert "Not enough evidence" in system and {t["name"] for t in tools} >= {"run_action", "search_identifier"}
        return self.script.pop(0)


def test_12_chat_acts_only_through_tools_and_is_remembered(client, monkeypatch):
    inv = client.post("/api/investigations", json={"text": "name: Example Person\njane.example@example.org",
                                                   "mode": "manual"}).json()["id"]
    fake = FakeProvider([
        AIP.Reply("", [AIP.ToolCall("1", "investigation_overview", {}),
                       AIP.ToolCall("2", "search_identifier", {"entity_id": "invented-id", "why": "guess"})],
                  [{"role": "assistant", "content": "calling tools"}], "tool_use"),
        AIP.Reply("Not enough evidence yet: no profile is linked to the person.", [], [{"role": "assistant",
                                                                                            "content": "final"}], "end"),
        AIP.Reply("Still nothing new.", [], [{"role": "assistant", "content": "second"}], "end"),
    ])
    monkeypatch.setattr(AC, "make", lambda cfg: fake)
    r = client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "What did you find?"}).json()
    assert r["answer"].startswith("Not enough evidence") and [c["tool"] for c in r["tool_calls"]] == \
        ["investigation_overview", "search_identifier"]
    bad = r["tool_calls"][1]
    assert bad["error"] and "already in the graph" in bad["result_preview"]          # no invented pivots
    client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "And now?"})
    first_turn = fake.seen[1]
    assert fake.seen[2][:len(first_turn)] == first_turn                               # append-only transcript
    log = client.get(f"/api/investigations/{inv}/assistant").json()["log"]
    assert [x["kind"] for x in log] == ["chat", "chat"] and log[0]["tool_calls"][0]["tool"] == "investigation_overview"
    assert client.delete(f"/api/investigations/{inv}").status_code == 200
    with closing(sqlite3.connect(client.state.db.path)) as con:
        assert con.execute("select count(*) from assistant_log").fetchone()[0] == 0


def test_12b_paid_actions_need_the_investigators_confirmation(db, settings):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("jane@example.org"), depth="1", options={})
    rt = eng.runtime(inv)
    settings.set_credential("HIBP_API_KEY", "k")
    paid = [a for a in A.next_actions(rt, eng, settings) if a["kind"] == "paid"]
    assert paid and paid[0]["requires_confirmation"]
    out = run(AC.run_action(eng, settings, db, inv, paid[0]["id"], by="assistant"))
    assert out["needs_confirmation"]                                                  # the AI can never run it
    out = run(AC.run_action(eng, settings, db, inv, paid[0]["id"]))
    assert out["needs_confirmation"]                                                  # nor a click without confirming


# --------------------------------------------------------------------------- 13–15 traceability, graph, next actions

def test_13_edges_explain_why_they_exist(client):
    inv = client.post("/api/investigations", json={"text": "name: Example Person\njane.example@example.org",
                                                   "mode": "manual"}).json()["id"]
    graph = client.get(f"/api/investigations/{inv}/graph").json()
    rel = graph["relationships"][0]
    d = client.get(f"/api/investigations/{inv}/relationships/{rel['id']}").json()
    assert d["evidence"] and d["evidence"][0]["class"] == "provided" and d["from"] and d["to"]
    assert client.get(f"/api/investigations/{inv}/relationships/doesnotexist").status_code == 404


def test_14_15_discovered_pivots_become_next_actions(db, settings):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("name: Example Person\njane.example@example.org"), depth="1", options={})
    rt = eng.runtime(inv)
    from sextant.model import RelEvidence, Source
    g = rt.graph
    g.add_source(Source(id="p1", connector="github", kind="api", url="https://profile.example/x", title="A public profile"))
    acc, _ = g.ensure_entity("account", "github:examplep", "GitHub: examplep", depth=1)
    u, _ = g.ensure_entity("username", "example_handle_57", "example_handle_57", depth=1)
    u.value_score = 0.8
    g.add_relationship(acc.key, u.key, "USES", RelEvidence(source="p1", description="profile lists this username"))
    from sextant.model import Observation
    g.add_observation(Observation(id="o1", entity=u.key, prop="@", value="example_handle_57", source="p1"))
    acts = {a["id"]: a for a in A.next_actions(rt, eng, settings)}
    pivot = acts[f"pivot:{u.id}"]
    assert pivot["expected_value"] == "high" and "Discovered by A public profile" in pivot["reason"]
    assert all(REG.REGISTRY[t.split(".")[0]].manifest.requires_subscription is False for t in pivot["transforms"])
    analysis = A.analyze(rt, eng, settings)
    assert {"headline", "identities", "platforms", "next_actions", "searched", "progress"} <= set(analysis)
    assert analysis["progress"][0] == {"step": "Normalize identifiers", "status": "done"}


# --------------------------------------------------------------------------- anti-bot: verification, manual-only, fallback

def test_challenge_pages_are_reported_not_bypassed():
    cf = HttpResponse(403, "https://x.example", {"content-type": "text/html"},
                      b"<html><title>Just a moment...</title><div id='challenge-platform'></div></html>")
    assert is_verification_challenge(cf.status, cf.headers, cf.content)
    assert is_verification_challenge(503, {"cf-mitigated": "challenge"}, b"")
    assert is_verification_challenge(403, {}, b"<form><div class='g-recaptcha'></div></form>")
    assert not is_verification_challenge(200, {}, b"<form>comment <div class='g-recaptcha'></div></form>")
    assert not is_verification_challenge(403, {}, b"Forbidden")
    routes = {"https://api.github.com/users/octocat": (403, "<title>Just a moment...</title> cf_chl_opt",
                                                        {"content-type": "text/html"})}
    out, status, message = run_transform("github.username_lookup", view("username", "octocat"), http=mock_http(routes))
    assert status == "blocked" and message.startswith("Verification required — manual action needed")


class TChallenged(Connector):
    manifest = Manifest(name="t_challenged", title="Test challenged", category="adapters", description="t",
                        website="https://challenged.example", disclosure=Disclosure(("challenged.example",), "username"))

    @transform(inputs=("username",), outputs=(), title="Test: answers with a CAPTCHA")
    async def lookup(self, entity, ctx):
        raise VerificationRequired("Verification required — manual action needed: test", url="https://challenged.example/x")


class TAlternative(Connector):
    manifest = Manifest(name="t_alternative", title="Test alternative", category="adapters", description="t",
                        website="https://alt.example", disclosure=Disclosure(("alt.example",), "username"),
                        default_enabled=False)

    @transform(inputs=("username",), outputs=(), title="Test: the other source")
    async def lookup(self, entity, ctx):
        raise NotFound("not here either")


def test_verification_required_offers_open_manual_only_and_another_source(db, settings, client):
    with registered(TChallenged, TAlternative):
        settings.set_connector_pref("t_alternative", enabled=True, auto_run=False)
        eng = engine_with(db, settings, {})
        inv = eng.create(parse_input("example_handle_57"), depth="1", options=only("t_challenged", "t_alternative"))

        async def go():
            await eng.start(inv)
            return await finish(eng, inv)
        rt = run(go())
        v = rt.data["verification"]["t_challenged"]
        assert v["url"] == "https://challenged.example/x" and v["title"] == "Test challenged"
        acts = {a["id"]: a for a in A.next_actions(rt, eng, settings)}
        assert acts["verify:t_challenged"]["kind"] == "verification" and not acts["verify:t_challenged"]["executable"]
        assert not any(r.connector == "t_alternative" for r in rt.runs.values())       # off without enhanced mode
        async def try_other():                      # the action and its runs share one event loop
            res = await AC.run_action(eng, settings, db, inv, "fallback:t_challenged")
            await finish(eng, inv)
            return res
        res = run(try_other())
        assert res["result"]["queued"] == ["t_alternative.lookup"]
        assert any(r.connector == "t_alternative" for r in rt.runs.values())
        # enhanced mode tries the other source by itself
        settings.set("enhanced_automation", True)
        inv2 = eng.create(parse_input("example_handle_58"), depth="1", options=only("t_challenged", "t_alternative"))

        async def go2():
            await eng.start(inv2)
            return await finish(eng, inv2)
        rt2 = run(go2())
        assert any(r.connector == "t_alternative" for r in rt2.runs.values())
        # "Mark source manual-only" takes it out of automation everywhere, and the catalog reset undoes it
        assert client.put("/api/sources/github", json={"manual_only": True}).json()["status"] == "manual"
        client.post("/api/catalog/reset", json={"confirm": "RESET"})
        assert client.get("/api/sources").json()["counts"].get("manual", 0) >= 1
        gh = next(s for g in client.get("/api/sources").json()["groups"] for s in g["sources"] if s["name"] == "github")
        assert gh["status"] == "available" and not gh["manual_only"]


# --------------------------------------------------------------------------- 16 reset compatibility, commercial sources

def test_16_reset_keeps_ai_credentials_and_investigation_memory(client):
    client.put("/api/assistant/key", json={"value": "sk-keep"})
    client.put("/api/settings", json={"values": {"ai_provider": "anthropic", "allow_paid_sources": False}})
    inv = client.post("/api/investigations", json={"text": "jane.example@example.org", "mode": "manual"}).json()["id"]
    client.state.db.assistant_append(inv, "action", {"title": "kept"})
    assert client.post("/api/catalog/reset", json={"confirm": "RESET"}).json()["ok"]
    p = client.get("/api/assistant/provider").json()
    assert p["key_set"] and p["provider"] == "anthropic"                               # credentials are not catalog data
    assert client.get(f"/api/investigations/{inv}/assistant").json()["log"][0]["title"] == "kept"


def test_commercial_providers_are_listed_but_never_queryable_by_default(client):
    r = client.post("/api/catalog/sources", json={
        "title": "Example People Data Inc", "url": "https://people-data.example", "category": "people_search",
        "source_type": "official_api", "access": "paid", "provider_class": "commercial", "pricing": "per lookup",
        "auth": "API key + contract", "terms_url": "https://people-data.example/terms", "data_types": ["names", "addresses"]})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    item = next(s for g in client.get("/api/sources").json()["groups"] for s in g["sources"] if s["name"] == sid)
    assert item["status"] == "catalog_only" and item["record"]["provider_class_title"] == "Licensed commercial provider"
    assert item["transforms"] == [] and item["disclosure"]["destinations"] == []
    doc = client.get("/api/catalog/export").json()
    assert next(s for s in doc["sources"] if s["id"] == sid)["data_types"] == ["names", "addresses"]


@pytest.mark.skipif(not __import__("importlib").util.find_spec("anthropic"), reason="anthropic SDK not installed")
def test_anthropic_provider_shapes_requests_and_reads_tool_calls(monkeypatch):
    p = AIP.AnthropicProvider("sk-test", "claude-opus-5-5")
    sent = {}

    class Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def to_dict(self, mode="json"):
            return dict(self.__dict__)

    class Resp:
        stop_reason = "tool_use"
        content = [Block(type="text", text="Checking."), Block(type="tool_use", id="tu1", name="investigation_overview",
                                                               input={})]

    async def create(**kw):
        sent.update(kw)
        return Resp()
    monkeypatch.setattr(p.client.beta.messages, "create", create)
    reply = run(p.complete("sys", [p.user_message("hi")], AC.TOOLS))
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default" and sent["output_config"]["effort"]
    assert "tool_choice" not in sent and sent["tools"][0]["input_schema"]["type"] == "object"
    assert reply.stop == "tool_use" and reply.tool_calls[0].name == "investigation_overview"
    assert p.tool_results([(reply.tool_calls[0], "{}", False)])[0]["content"][0]["tool_use_id"] == "tu1"
