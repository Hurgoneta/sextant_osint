"""The investigation copilot: one provider abstraction (OpenAI, Anthropic, OpenRouter, local), failover, usage and cost
accounting, catalog changes through proposals, source inspection/discovery, notes, budgets, access classes, commercial
catalog entries and scoped resets. Synthetic data only; every external answer is a fixture."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import run
from sextant.assistant import copilot as AC
from sextant.assistant import providers as AIP
from sextant.costs import ledger as LEDGER
from sextant.costs import pricing as PR
from sextant.registry import connectors as REG
from sextant.sdk.testing import mock_http
from test_assistant import FakeProvider, app  # noqa: F401  (fixture helpers)


@pytest.fixture
def client(tmp_path):
    with app(tmp_path / "data") as c:
        yield c


def _inv(client, text="name: Example Person"):
    return client.post("/api/investigations", json={"text": text, "mode": "manual"}).json()["id"]


def _openai_reply(content="ready", tool_calls=None, usage=None, model="m"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": model, "choices": [{"message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}}


# --------------------------------------------------------------------------- providers

def test_openrouter_request_shape_usage_and_billed_cost(monkeypatch):
    sent = []
    routes = {"https://openrouter.ai/api/v1/chat/completions": (200, _openai_reply(usage={
        "prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 2300, "cost": 0.0123,
        "prompt_tokens_details": {"cached_tokens": 500}, "completion_tokens_details": {"reasoning_tokens": 40}},
        model="vendor/some-model"))}
    p = AIP.OpenAICompatibleProvider("sk-or", "vendor/some-model", "https://openrouter.ai/api/v1", provider_id="openrouter",
                                     temperature=0.2, max_output=900, reasoning="high")
    monkeypatch.setattr(p, "_client", lambda: mock_http(routes, sent))
    reply = run(p.complete("sys", [p.user_message("hi")], AC.TOOLS[:2]))
    body = json.loads(sent[0].content)
    assert body["model"] == "vendor/some-model" and body["reasoning"] == {"effort": "high"} and body["usage"] == {"include": True}
    assert body["temperature"] == 0.2 and body["max_tokens"] == 900 and body["tool_choice"] == "auto"
    assert sent[0].headers["authorization"] == "Bearer sk-or" and sent[0].headers["x-title"] == "Sextant"
    assert reply.usage == {"prompt_tokens": 2000, "completion_tokens": 300, "cached_tokens": 500, "reasoning_tokens": 40,
                           "total_tokens": 2300, "cost": 0.0123} and reply.model == "vendor/some-model"


def test_openai_uses_max_completion_tokens_and_reasoning_effort(monkeypatch):
    sent = []
    p = AIP.OpenAICompatibleProvider("sk", "some-model", "https://api.openai.com/v1", provider_id="openai", max_output=500,
                                     reasoning="low")
    monkeypatch.setattr(p, "_client", lambda: mock_http({"https://api.openai.com/": (200, _openai_reply())}, sent))
    run(p.complete("sys", [p.user_message("hi")], []))
    body = json.loads(sent[0].content)
    assert body["max_completion_tokens"] == 500 and body["reasoning_effort"] == "low" and "tools" not in body


def test_profiles_keys_and_shared_provider_identity(client):
    s = client.state.settings
    r = client.put("/api/ai/providers/openrouter", json={"model": "vendor/model-a", "reasoning": "medium", "key": "sk-or-x",
                                                         "make_primary": True}).json()
    p = next(x for x in r["providers"] if x["id"] == "openrouter")
    assert p["ready"] and p["model"] == "vendor/model-a" and p["primary"] and "sk-or-x" not in json.dumps(r)
    assert client.put("/api/ai/providers/openrouter", json={"reasoning": "galaxy-brain"}).status_code == 400
    assert client.put("/api/ai/providers/openrouter", json={"temperature": 5}).status_code == 400
    # one provider identity for every service that uses its key: chat, connection tests, … all count as "openrouter"
    assert s.credential("OPENROUTER_API_KEY") == "sk-or-x" and AIP.config(s)["provider"] == "openrouter"


def test_chat_fails_over_and_never_uses_a_disabled_provider(client, monkeypatch):
    inv = _inv(client)
    s = client.state.settings
    client.put("/api/ai/providers/openrouter", json={"model": "vendor/m", "key": "k1", "make_primary": True})
    client.put("/api/ai/providers/openai", json={"model": "gpt-x", "key": "k2", "enabled": False})
    client.put("/api/ai/providers/local", json={"model": "llama-local"})
    client.put("/api/ai/failover", json={"order": ["openai", "local"]})
    calls = []

    async def complete(self, system, transcript, tools):
        calls.append(self.provider_id)
        if self.provider_id == "openrouter":
            raise AIP.ProviderError("openrouter.ai answered HTTP 503: overloaded")
        return AIP.Reply("Not enough evidence yet.", [], [{"role": "assistant", "content": "x"}], "end",
                         usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, model="llama-local")
    monkeypatch.setattr(AIP.OpenAICompatibleProvider, "complete", complete)
    r = client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "What did you find?"}).json()
    assert calls == ["openrouter", "local"]                                     # openai disabled → never called
    assert r["handled_by"]["provider"] == "local" and r["handled_by"]["fallback"]
    assert any(a["provider"] == "openai" and a.get("skipped") for a in r["attempts"])
    rec = client.state.db.usage_list(inv)
    assert rec and rec[-1]["provider"] == "local" and rec[-1]["estimated_cost"] == 0.0        # a local model: no charge
    assert rec[-1]["data"]["note"] == "local model: no per-token charge"
    assert s.credential("OPENAI_API_KEY") == "k2"


def test_usage_is_actual_when_billed_and_estimated_from_published_prices(client, monkeypatch):
    inv = _inv(client)
    client.put("/api/settings", json={"values": {"ai_provider": "openrouter"}})
    client.put("/api/ai/providers/openrouter", json={"model": "vendor/m", "key": "k"})
    PR.put_override(client.state.settings, {"provider": "openrouter", "service": "ai_chat", "model": "vendor/m",
                                            "input_per_mtok": 1.0, "output_per_mtok": 2.0, "verified_date": "2026-10-01"})

    async def complete(self, system, transcript, tools):
        return AIP.Reply("ok", [], [{"role": "assistant", "content": "ok"}], "end",
                         usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000,
                                "cost": 2.5}, model="vendor/m")
    monkeypatch.setattr(AIP.OpenAICompatibleProvider, "complete", complete)
    client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "hi"})
    u = client.state.db.usage_list(inv)[-1]
    assert (u["estimated_cost"], u["actual_cost"], u["cost_status"]) == (3.0, 2.5, "actual")
    costs = client.get(f"/api/investigations/{inv}/costs").json()
    line = next(x for x in costs["lines"] if x["provider"] == "openrouter")
    assert line["actual"] == 2.5 and line["estimated"] == 3.0 and line["category"] == "ai"
    assert costs["totals"]["actual"] == 2.5 and "invoice" in costs["explanation"]


def test_budget_stops_the_ai_before_it_spends(client, monkeypatch):
    inv = client.post("/api/investigations", json={"text": "name: Example Person", "mode": "manual",
                                                   "budget_usd": 0.0001}).json()["id"]
    client.put("/api/settings", json={"values": {"ai_provider": "openrouter"}})
    client.put("/api/ai/providers/openrouter", json={"model": "vendor/m", "key": "k"})
    PR.put_override(client.state.settings, {"provider": "openrouter", "service": "ai_chat", "model": "vendor/m",
                                            "input_per_mtok": 3.0, "output_per_mtok": 15.0})
    called = []

    async def complete(self, *a):
        called.append(1)
        return AIP.Reply("ok", [], [{"role": "assistant", "content": "ok"}], "end")
    monkeypatch.setattr(AIP.OpenAICompatibleProvider, "complete", complete)
    r = client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "hi"}).json()
    assert r["stop"] == "budget" and not called and "Budget check" in r["answer"]
    r = client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "hi", "confirm_budget": True}).json()
    assert called and r["stop"] == "end"


# --------------------------------------------------------------------------- catalog through the copilot

def _tool(client, inv, name, args, turn=None):
    st = client.state
    out, err = run(AC.call_tool(name, args, engine=st.engine, cfg=st.settings, db=st.db, inv_id=inv,
                                user_platforms=st.catalog.snapshot().platform_objects, turn=turn))
    return json.loads(out.split("… (truncated)")[0]) if not out.endswith("(truncated)") else out, err


def test_inspect_then_add_a_swedish_court_source_through_a_proposal(tmp_path):
    page = ("<html lang='sv'><head><title>Exempeldomstolen – Sveriges Domstolar</title>"
            "<meta name='description' content='Officiell webbplats för Exempeldomstolen, en domstol.'></head></html>")
    with app(tmp_path / "data", {"https://court.example.se/": (200, page, {"content-type": "text/html"})}) as client:
        inv = _inv(client)
        r, err = _tool(client, inv, "inspect_source_url", {"url": "https://court.example.se/"})
        assert not err and r["reachable"] and r["classification"]["category"] == "legal_records"
        assert r["classification"]["jurisdiction"] == "SE" and any("legal records" in x for x in r["classification"]["signals"])
        assert any(".se" in x for x in r["classification"]["signals"])
        d = r["draft"]
        assert d["jurisdictions"] == ["SE"] and d["review"] == "not_verified" and "Not verified" in d["notes"]
        r, err = _tool(client, inv, "add_source", d)
        assert not err and not r["applied"] and r["proposal"]["status"] == "pending"
        assert ["Jurisdiction", "Sweden"] in r["proposal"]["summary"]
        assert not client.get("/api/catalog/sources?jurisdiction=SE").json()                  # nothing saved yet
        pid = r["proposal"]["proposal_id"]
        out = client.post(f"/api/investigations/{inv}/assistant/proposals/{pid}", json={"decision": "apply"}).json()
        sid = out["result"]["source_id"]
        # one record, visible everywhere it belongs
        assert [s["id"] for s in client.get("/api/catalog/sources?jurisdiction=SE").json()] == [sid]
        se = next(j for reg in client.get("/api/jurisdictions?countries=SE").json()["regions"] for j in reg["jurisdictions"]
                  if j["code"] == "SE")
        assert any(s.get("id") == sid for s in se["sources"])
        assert any(s["name"] == sid for g in client.get("/api/sources").json()["groups"] for s in g["sources"])
        assert any(s.get("title") == d["title"] for s in client.get("/api/catalog/audit/SE").json()["sources"])
        # applied proposals cannot be applied twice
        assert client.post(f"/api/investigations/{inv}/assistant/proposals/{pid}",
                           json={"decision": "apply"}).status_code == 409
        # a second identical add is flagged as a duplicate and never auto-applied
        client.put("/api/settings", json={"values": {"ai_auto_apply_catalog": True}})
        r, _ = _tool(client, inv, "add_source", d)
        assert not r["applied"] and r["duplicates"]


def test_auto_apply_only_for_non_destructive_changes(client):
    inv = _inv(client)
    client.put("/api/settings", json={"values": {"ai_auto_apply_catalog": True}})
    r, err = _tool(client, inv, "add_source", {"title": "Example Legal Portal", "url": "https://legal-portal.example.se/",
                                                 "category": "legal_records", "jurisdictions": ["SE"]})
    assert not err and r["applied"] and r["proposal"]["auto"]
    sid = r["result"]["source_id"]
    r, _ = _tool(client, inv, "link_source", {"source_id": sid, "add_jurisdictions": ["NO"]})
    assert r["applied"]
    r, _ = _tool(client, inv, "link_source", {"source_id": sid, "remove_jurisdictions": ["NO"]})
    assert not r["applied"] and r["proposal"]["destructive"]                     # removals always wait
    r, _ = _tool(client, inv, "delete_catalog_item", {"kind": "source", "id": sid})
    assert not r["applied"] and r["proposal"]["destructive"]
    assert sid in [s["id"] for s in client.get("/api/catalog/sources").json()]    # still there
    r, err = _tool(client, inv, "update_source", {"source_id": "github", "changes": {"title": "x"}})
    assert err and "Only your own sources" in r["error"]                          # built-in records are read-only


def test_discover_sources_suggests_without_adding(tmp_path):
    def brave(request):
        q = parse_qs(urlsplit(str(request.url)).query)["q"][0]
        assert "Sweden" in q
        return 200, {"web": {"results": [
            {"url": "https://www.contactdata.example/pricing", "title": "ContactData – B2B contact data provider",
             "description": "Business contact database. Pricing plans and API. Free trial."},
            {"url": "https://en.wikipedia.org/wiki/Data_broker", "title": "Data broker - Wikipedia", "description": "…"},
            {"url": "https://www.linkedin.com/company/x", "title": "X | LinkedIn", "description": "…"}]}}
    with app(tmp_path / "data", {"https://api.search.brave.com/": brave}) as client:
        client.state.settings.set_credential("BRAVE_API_KEY", "k")
        r = client.post("/api/catalog/discover", json={"request": "commercial people/contact data providers",
                                                       "jurisdiction": "SE"}).json()
        assert r["ok"] and r["provider"] == "brave_search" and len(r["candidates"]) == 1
        c = r["candidates"][0]
        assert c["host"] == "contactdata.example" and c["classification"]["commercial"]
        assert c["draft"]["provider_class"] == "commercial" and c["draft"]["jurisdictions"] == ["SE"]
        assert c["rank"] == 1 and c["status"] == "Not verified"
        assert client.get("/api/catalog/sources").json() == []                     # nothing silently added
        assert client.get("/api/usage").json()["providers"][0]["provider"] == "brave"   # search cost recorded


def test_add_identifier_only_from_the_investigators_words(client):
    inv = _inv(client)
    r, err = _tool(client, inv, "add_identifier", {"type": "username", "value": "made_up_handle", "why": "guess"},
                   turn={"user_text": "Search tavo_in42 please"})
    assert err and "not in their message" in r["error"]
    r, err = _tool(client, inv, "add_identifier", {"type": "username", "value": "tavo_in42", "why": "asked"},
                   turn={"user_text": "Search tavo_in42 please"})
    assert not err and client.state.engine.runtime(inv).graph.get("username:tavo_in42") is not None


def test_notes_persist_and_deleting_needs_confirmation(client):
    inv = _inv(client)
    r, _ = _tool(client, inv, "add_note", {"title": "IMPORTANT IDENTIFIERS", "body": "Candidate username tavo_in42 (public "
                                           "profile), status candidate", "identifiers": ["tavo_in42"], "status": "candidate"})
    nid = r["note"]["id"]
    assert client.get(f"/api/investigations/{inv}/notes").json()[0]["title"] == "IMPORTANT IDENTIFIERS"
    r, _ = _tool(client, inv, "delete_note", {"note_id": nid})
    assert r["proposal"]["destructive"] and client.get(f"/api/investigations/{inv}/notes").json()
    pid = r["proposal"]["proposal_id"]
    client.post(f"/api/investigations/{inv}/assistant/proposals/{pid}", json={"decision": "apply"})
    assert client.get(f"/api/investigations/{inv}/notes").json() == []


def test_discover_profiles_with_a_platform_focus(client):
    from test_discovery import ROUTES
    st = client.state
    st.settings.set_credential("BRAVE_API_KEY", "k")
    st.engine.http_factory = lambda **kw: mock_http(ROUTES, **kw)
    inv = _inv(client, "name: Mira Fictus Exempla")

    async def go():
        out, err = await AC.call_tool("discover_profiles", {"platforms": ["Pinterest"], "why": "asked to check Pinterest"},
                                      engine=st.engine, cfg=st.settings, db=st.db, inv_id=inv)
        await _wait(st.engine.runtime(inv))
        return json.loads(out), err
    r, err = run(go())
    assert not err and r["queued"] == ["brave_search.profile_discovery"] and r["platforms"] == ["pinterest"]
    rt = st.engine.runtime(inv)
    queries = [j["query"] for j in rt.data["search_journal"]]
    assert queries and all("site:pinterest.com" in q for q in queries)            # only the requested platform
    assert rt.graph.get("account:pinterest:mirafictuse") is not None


async def _wait(rt):
    if rt.loop_task:
        await rt.loop_task


def test_tool_list_covers_the_application(client):
    names = {t["name"] for t in AC.TOOLS}
    assert {"discover_profiles", "identifiers", "discovery_report", "cost_report", "jurisdiction_audit", "catalog_search",
            "inspect_source_url", "discover_sources", "add_source", "link_source", "add_category", "add_platform",
            "delete_catalog_item", "add_note", "connector_health", "platform_lookup", "search_identifier"} <= names
    for t in AC.TOOLS:
        assert t["schema"]["type"] == "object" and t["schema"]["additionalProperties"] is False


# --------------------------------------------------------------------------- costs, catalog classes, resets

def test_pricing_is_editable_and_unknown_stays_unknown(client):
    assert any(p["provider"] == "anthropic" and p["model"] == "claude-opus-5-5" and p["verified"]
               for p in client.get("/api/pricing").json()["prices"])
    r = client.put("/api/pricing", json={"provider": "serper", "service": "web_search", "connector": "serper",
                                         "per_request": 0.001, "source_url": "https://serper.dev",
                                         "verified_date": "2026-10-07"})
    assert r.status_code == 200 and PR.price_for_connector("serper", client.state.settings)["per_request"] == 0.001
    assert client.put("/api/pricing", json={"provider": "x", "per_request": -1}).status_code == 400
    assert client.delete("/api/pricing", params={"key": r.json()["key"]}).json()["ok"]
    assert PR.price_for_connector("tavily", client.state.settings) is None          # no price known → None, not 0
    assert PR.token_cost(None, {"prompt_tokens": 10}) is None


def test_forecast_and_search_providers_in_the_plan(client):
    client.state.settings.set_credential("BRAVE_API_KEY", "k")
    p = client.post("/api/assistant/plan", json={"text": "name: Mira Fictus Exempla"}).json()
    f = p["forecast"]
    assert any(line["provider"] == "brave" and line["estimated"] > 0 for line in f["lines"])
    assert f["total_estimated"] > 0 and f["money"]["currency"] in ("EUR", "USD")
    assert p["search_providers"][0]["status"] == "available" and not p["web_discovery_unavailable"]


def test_access_classes_are_filterable_and_commercial_entries_are_not_queryable(client):
    d = client.get("/api/sources").json()
    assert set(d["access_classes"]) >= {"FREE_PUBLIC", "FREE_API", "FREE_TIER", "PAID_API", "SUBSCRIPTION", "ENTERPRISE",
                                        "MANUAL", "REFERENCE_ONLY", "UNAVAILABLE", "NOT_VERIFIED"}
    ent = client.get("/api/sources", params={"access_class": "ENTERPRISE"}).json()
    names = {s["name"] for g in ent["groups"] for s in g["sources"]}
    assert {"zoominfo", "acxiom", "experian"} <= names
    apollo = next(s for g in d["groups"] for s in g["sources"] if s["name"] == "apollo")
    assert apollo["status"] == "catalog_only" and apollo["transforms"] == []
    assert "FREE_TIER" in apollo["access_classes"] and "NOT_VERIFIED" in apollo["access_classes"]
    assert apollo["commercial"]["api"] == "documented" and apollo["category"] == "b2b_sales_intelligence"
    for name in ("zoominfo", "apollo", "lusha", "kaspr", "uplead", "lead411", "acxiom", "experian", "equifax", "nielsen",
                 "spokeo", "whitepages", "intelius", "peoplefinders"):
        assert REG.connector_status(REG.REGISTRY[name], client.state.settings)[0] == "catalog_only"
    # a user commercial source with the new metadata
    r = client.post("/api/catalog/sources", json={
        "title": "Nordic Contact Data AB", "url": "https://nordic-contacts.example", "category": "business_contact_data",
        "source_type": "official_api", "access": "paid", "provider_class": "commercial", "jurisdictions": ["SE"],
        "access_models": ["free_tier", "per_query"], "api_available": "yes", "web_app": True,
        "documentation_url": "https://nordic-contacts.example/docs", "limitations": "B2B only"})
    assert r.status_code == 201, r.text
    item = next(s for g in client.get("/api/sources").json()["groups"] for s in g["sources"] if s["name"] == r.json()["id"])
    assert item["access_classes"][:2] == ["FREE_TIER", "PAID_API"] and item["commercial"]["api_available"] == "yes"
    assert client.post("/api/catalog/sources", json={"title": "Bad", "url": "https://bad.example", "category": "people_search",
                                                     "access_models": ["lifetime"]}).status_code == 400


def test_scoped_resets_touch_only_what_was_chosen(client):
    inv = _inv(client)
    client.put("/api/assistant/key", json={"value": "sk-keep"})
    client.put("/api/ai/providers/openrouter", json={"model": "vendor/m", "key": "sk-or"})
    client.post("/api/catalog/sources", json={"title": "Mine", "url": "https://mine.example", "category": "public_records"})
    PR.put_override(client.state.settings, {"provider": "brave", "service": "web_search", "connector": "brave_search",
                                            "per_request": 0.004})
    summary = client.get("/api/reset").json()
    assert set(summary["scopes"]) == {"catalog", "history", "ai_settings", "credentials"}
    assert client.post("/api/reset", json={"scopes": ["catalog"], "confirm": "nope"}).status_code == 400
    client.post("/api/reset", json={"scopes": ["catalog"], "confirm": "RESET"})
    assert client.get("/api/catalog/sources").json() == []
    assert client.get(f"/api/investigations/{inv}").status_code == 200                  # history kept
    assert client.state.settings.credential("OPENROUTER_API_KEY") == "sk-or"            # keys kept
    assert PR.price_for_connector("brave_search", client.state.settings)["per_request"] == 0.004
    client.post("/api/reset", json={"scopes": ["ai_settings"], "confirm": "RESET"})
    assert AIP.profile(client.state.settings, "openrouter")["model"] == ""             # profile gone…
    assert client.state.settings.credential("OPENROUTER_API_KEY") == "sk-or"            # …key still there
    assert PR.price_for_connector("brave_search", client.state.settings)["per_request"] == 0.005
    client.post("/api/reset", json={"scopes": ["history"], "confirm": "RESET"})
    assert client.get("/api/investigations").json() == []
    client.post("/api/reset", json={"scopes": ["credentials"], "confirm": "RESET"})
    assert not client.state.settings.credential("OPENROUTER_API_KEY") and not client.state.settings.credential("AI_API_KEY")


def test_budget_api_and_ledger_money(client):
    inv = client.post("/api/investigations", json={"text": "name: Example Person", "mode": "manual",
                                                   "budget_usd": 1.0, "confirm_above_usd": 0.5}).json()["id"]
    rt = client.state.engine.runtime(inv)
    assert rt.options["budget_usd"] == 1.0 and rt.options["confirm_above_usd"] == 0.5
    assert client.put(f"/api/investigations/{inv}/budget", json={"budget_usd": 5.0}).json()["budget_usd"] == 5.0
    assert client.post(f"/api/investigations/{inv}/budget", json={"decision": "continue"}).json()["queued"] == []
    m = LEDGER.money(client.state.settings)
    assert m["currency"] == "EUR" and m["symbol"] == "€" and "not a live exchange rate" in m["note"]


class Scripted(FakeProvider):
    """FakeProvider with the full interface (provider id, plain-text replay of other providers' turns)."""
    def __init__(self, script, pid="fake"):
        super().__init__(script)
        self.provider_id = self.id = pid

    def assistant_text(self, text):
        return {"role": "assistant", "content": text}

    async def complete(self, system, transcript, tools):
        self.seen.append(list(transcript))
        return self.script.pop(0)


def test_chat_turn_inspects_and_proposes_a_source(tmp_path, monkeypatch):
    page = "<html><head><title>Exempelrätten | Officiell webbplats</title></head></html>"
    with app(tmp_path / "data", {"https://lexbase.example.se/": (200, page, {"content-type": "text/html"})}) as client:
        inv = _inv(client)
        fake = Scripted([
            AIP.Reply("", [AIP.ToolCall("1", "inspect_source_url", {"url": "https://lexbase.example.se/",
                                                                     "category": "legal_records"})],
                      [{"role": "assistant", "content": "inspect"}], "tool_use"),
            AIP.Reply("", [AIP.ToolCall("2", "add_source", {"title": "Exempelrätten", "url": "https://lexbase.example.se/",
                                                             "category": "legal_records", "jurisdictions": ["SE"],
                                                             "source_type": "reference"})],
                      [{"role": "assistant", "content": "propose"}], "tool_use"),
            AIP.Reply("Here is what I plan to add. Save it?", [], [{"role": "assistant", "content": "done"}], "end")])
        monkeypatch.setattr(AC, "make", lambda cfg: fake)
        r = client.post(f"/api/investigations/{inv}/assistant/chat",
                        json={"text": "Add https://lexbase.example.se/ to the Swedish legal sources"}).json()
        assert [c["tool"] for c in r["tool_calls"]] == ["inspect_source_url", "add_source"]
        assert len(r["proposals"]) == 1 and r["proposals"][0]["status"] == "pending"
        assert "Investigation" in fake.seen[0][0]["content"]                        # the state digest rides along
        a = client.get(f"/api/investigations/{inv}/assistant").json()
        assert a["proposals"][0]["op"] == "add_source" and any(x["kind"] == "proposal" for x in a["log"])
        pid = a["proposals"][0]["proposal_id"]
        assert client.post(f"/api/investigations/{inv}/assistant/proposals/{pid}", json={"decision": "cancel"}).json()[
            "status"] == "cancelled"
        assert client.get("/api/catalog/sources").json() == []


def test_conversation_continues_when_the_provider_changes(client, monkeypatch):
    inv = _inv(client)
    first = Scripted([AIP.Reply("Candidate A looks strongest.", [], [{"role": "assistant", "content": "A"}], "end")], "p1")
    monkeypatch.setattr(AC, "make", lambda cfg: first)
    client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "Which one is most likely?"})
    second = Scripted([AIP.Reply("Because of the cross-link.", [], [{"role": "assistant", "content": "B"}], "end")], "p2")
    monkeypatch.setattr(AC, "make", lambda cfg: second)
    client.post(f"/api/investigations/{inv}/assistant/chat", json={"text": "Why?"})
    seen = second.seen[0]
    assert seen[0] == {"role": "user", "content": "Which one is most likely?"}       # earlier turn replayed as text
    assert "Candidate A looks strongest." in seen[1]["content"] and "p1" in seen[1]["content"]
    log = client.get(f"/api/investigations/{inv}/assistant").json()["log"]
    assert [x["provider"] for x in log if x["kind"] == "chat"] == ["p1", "p2"]
