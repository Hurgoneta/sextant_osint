"""Identity discovery: full name → web search → platform result → profile URL → handle → candidate → evidence → pivot.

Regression tests for the failure where a person's real handle was never found because only name-derived templates
(first.last, firstlast, …) were tried. The search engine is a fixture (a fake index answering per query), so the tests
check the pipeline, not live search behaviour. Synthetic profiles only.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import engine_with, run
from sextant.assistant import analysis as A
from sextant.assistant import identifiers as IDS
from sextant.assistant.names import candidates
from sextant.costs import ledger as LEDGER
from sextant.discovery import profiles as PROF
from sextant.discovery import queries as Q
from sextant.discovery import stages as STAGES
from sextant.engine.input import parse_input
from sextant.registry import connectors as REG
from sextant.sdk.testing import mock_http, run_transform, view
from sextant.sdk.transform import InvestigationInfo

MIRA = "Mira Fictus Exempla"
TAVO = "Tavo Imaginar Nullus"

# The fake index: exact query text → result list (rank = position). Anything else returns no results.
INDEX = {
    f'"{MIRA}" site:pinterest.com': [
        {"url": "https://www.pinterest.com/mirafictuse/", "title": "Mira Fictus (mirafictuse) - Profile | Pinterest",
         "description": "See what Mira Fictus (mirafictuse) has discovered on Pinterest."},
        {"url": "https://www.pinterest.com/pin/123456/", "title": "Some pin", "description": "…"}],
    '"mirafictuse" site:instagram.com': [
        {"url": "https://www.instagram.com/mira_fictus/", "title": "Mira Fictus Exempla (@mira_fictus) • Instagram",
         "description": "Pinterest: mirafictuse"}],
    f'"{TAVO}" site:instagram.com': [
        {"url": "https://www.instagram.com/tavo_in42/reels/", "title": "Tavo Nullus (@tavo_in42) • Instagram photos and videos",
         "description": "120 followers"}],
}


def brave_index(request):
    q = parse_qs(urlsplit(str(request.url)).query).get("q", [""])[0]
    return 200, {"web": {"results": INDEX.get(q, [])}}


def searx_index(request):
    q = parse_qs(urlsplit(str(request.url)).query).get("q", [""])[0]
    return 200, {"results": [{"url": r["url"], "title": r["title"], "content": r["description"]} for r in INDEX.get(q, [])]}


ROUTES = {"https://api.search.brave.com/": brave_index}


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("url,platform,handle,how", [
    ("https://www.pinterest.com/mirafictuse/", "pinterest", "mirafictuse", "profile"),
    ("https://ro.pinterest.com/mirafictuse/_saved/", "pinterest", "mirafictuse", "subpage"),
    ("https://www.pinterest.co.uk/mirafictuse/recipes/", "pinterest", "mirafictuse", "owner"),
    ("https://www.instagram.com/tavo_in42/", "instagram", "tavo_in42", "profile"),
    ("https://www.instagram.com/tavo_in42/reels/", "instagram", "tavo_in42", "subpage"),
    ("https://www.tiktok.com/@some.user/video/7301234567", "tiktok", "some.user", "subpage"),
    ("https://github.com/octo-dev/some-repo", "github", "octo-dev", "owner"),
    ("https://www.reddit.com/user/example_user/comments/", "reddit", "example_user", "profile"),
    ("https://x.com/example_user/status/1234567890", "x", "example_user", "subpage"),
    ("https://www.youtube.com/@examplechannel/videos", "youtube", "examplechannel", "subpage"),
    ("https://www.twitch.tv/examplestreamer", "twitch", "examplestreamer", "profile"),
    ("https://open.spotify.com/user/exampleuser", "spotify", "exampleuser", "profile"),
    ("https://gitlab.com/example-dev", "gitlab", "example-dev", "profile"),
])
def test_profile_urls_yield_platform_and_handle(url, platform, handle, how):
    hit = PROF.parse_profile(url)
    assert hit is not None and (hit.platform.id, hit.username, hit.how) == (platform, handle, how)


@pytest.mark.parametrize("url", ["https://www.pinterest.com/pin/123456/", "https://www.instagram.com/p/AbCdEf/",
                                 "https://www.instagram.com/explore/tags/x/", "https://example.org/someone/"])
def test_non_profile_urls_are_not_handles(url):
    assert PROF.parse_profile(url) is None


def test_titles_give_display_name_and_handle_agreement():
    t = PROF.title_identity("Mira Fictus (mirafictuse) - Profile | Pinterest", "mirafictuse")
    assert (t.display_name, t.handle_in_title, t.handle_agrees) == ("Mira Fictus", "mirafictuse", True)
    t = PROF.title_identity("Tavo Nullus (@tavo_in42) • Instagram photos and videos", "tavo_in42")
    assert t.display_name == "Tavo Nullus" and t.handle_agrees
    t = PROF.title_identity("octo-dev (Octo Developer) · GitHub", "octo-dev")
    assert t.display_name == "Octo Developer" and t.handle_agrees
    assert PROF.title_identity("Someone Else | LinkedIn").display_name == "Someone Else"


# --------------------------------------------------------------------------- query planning

def test_queries_target_platforms_by_name_not_guessed_handles():
    plan = Q.plan_person([MIRA], cap=40)
    texts = [p.text for p in plan]
    assert f'"{MIRA}" site:pinterest.com' in texts and f'"{MIRA}" site:instagram.com' in texts
    assert '"Mira Fictus" site:pinterest.com' in texts                 # a shorter display-name form
    assert len(Q.plan_person([MIRA], cap=12)) == 12                     # never hundreds of queries
    assert not any("mirafictuse" in t for t in texts)                 # no handle guessing in the web stage
    # regional platforms rank higher for their countries
    ru = [p.platform for p in Q.plan_person(["Ivan Primerov"], countries=("RU",), cap=40)]
    xx = [p.platform for p in Q.plan_person(["Ivan Primerov"], cap=40)]
    assert ru.index("vk") < xx.index("vk")


# --------------------------------------------------------------------------- the failing cases, transform level

@pytest.mark.parametrize("name,query,platform,handle", [
    (MIRA, f'"{MIRA}" site:pinterest.com', "pinterest", "mirafictuse"),
    (TAVO, f'"{TAVO}" site:instagram.com', "instagram", "tavo_in42"),
])
def test_unguessable_handle_is_found_from_a_search_result(name, query, platform, handle):
    # the handle is not among the name-derived templates a normal (Balanced) run would test, and for the second case it is not
    # derivable from the name at all: template generation alone would never find it
    assert handle not in {c.handle for c in candidates([name], 4)}
    if name == TAVO:
        assert handle not in {c.handle for c in candidates([name], 200)}
    person = view("person", "target", label=name, full_name=name)
    out, status, _ = run_transform("brave_search.profile_discovery", person, http=mock_http(ROUTES),
                                   credentials={"BRAVE_API_KEY": "k"},
                                   info=InvestigationInfo(target_names=(name,), options={"search_query_cap": 30,
                                                                                         "discovery_query_cap": 30}))
    assert status == "found"
    acc = f"account:{platform}:{handle}"
    assert acc in out.entities and f"username:{handle}" in out.entities
    props = {(o.entity, o.prop): o.value for o in out.observations}
    assert props[(acc, "search_rank")] == 1 and props[(acc, "search_query")] == query
    assert props[(acc, "search_provider")] == "brave_search" and props[(acc, "found_by")] == "search_result"
    assert props[(f"username:{handle}", "discovery")] == "search_result"
    assert props[(f"username:{handle}", "pivot")] is True                 # the title echoes the name → pivot
    assert any(r.type == "USES" and r.source == acc and r.target == f"username:{handle}" for r in out.relationships)
    assert any("Handle in the profile URL" in o.evidence for o in out.observations if o.entity == f"username:{handle}")


# --------------------------------------------------------------------------- the whole chain, engine level

def _only_search_and_usernames():
    keep = {"brave_search", "searxng", "username_guesses"}
    return {"categories": ["search", "usernames"],
            "connectors_disabled": [n for n, c in REG.REGISTRY.items()
                                    if c.manifest.category in ("search", "usernames", "adapters") and n not in keep]}


async def _finish(eng, inv):
    rt = eng.runtime(inv)
    if rt.loop_task:
        await rt.loop_task
    return rt


def test_full_chain_name_to_pivot_to_second_platform(db, settings):
    settings.set_credential("BRAVE_API_KEY", "k")
    eng = engine_with(db, settings, ROUTES)
    inv = eng.create(parse_input(f"name: {MIRA}"), depth="2", options=_only_search_and_usernames())

    async def go():
        await eng.start(inv)
        return await _finish(eng, inv)
    rt = run(go())
    g = rt.graph
    pin = g.get("account:pinterest:mirafictuse")
    handle = g.get("username:mirafictuse")
    assert pin is not None and handle is not None
    # stage 8–9: the discovered handle was searched on other platforms, and found Instagram
    assert any(t.entity_key == handle.key and t.transform == "brave_search.profile_discovery" for t in rt.tasks.values())
    ig = g.get("account:instagram:mira_fictus")
    assert ig is not None and ig.get("search_query") == '"mirafictuse" site:instagram.com'
    # identifier states keep discovered and generated apart
    rows = {r["key"]: r for r in IDS.rows(rt)}
    assert rows["username:mirafictuse"]["state"] == "discovered"
    assert rows["username:mirafictuse"]["search_rank"] == 1
    # evidence: explainable, conservative (name + rank are weak/medium signals, never "High")
    a = A.assess(pin)
    assert a["verdict"] not in ("strong", "confirmed") and a["explanation"]["confidence"] in ("Low", "Medium",
                                                                                             "Insufficient evidence")
    assert any(x["strength"] == "missing" for x in a["explanation"]["against"])
    assert any("Rank 1 result" in r["text"] for r in a["support"])
    # the stage report and the journal answer "what did you search and what did it find"
    rep = STAGES.report(rt, settings)
    st = {s["n"]: s for s in rep["stages"]}
    assert st[5]["status"] == "done" and st[7]["status"] == "done" and "mirafictuse" in st[7]["detail"]
    assert any(j["query"] == f'"{MIRA}" site:pinterest.com' and "pinterest:mirafictuse" in j["profiles"]
               for j in rep["journal"])
    assert any(h["handle"] == "mirafictuse" and not h["predictable_from_name"] for h in rep["discovered_handles"])
    # costs: search queries counted per provider as estimates
    costs = LEDGER.investigation_costs(rt, db, settings)
    brave = next(line for line in costs["lines"] if line["provider"] == "brave")
    assert brave["requests"] > 0 and brave["estimated"] > 0 and brave["status"] == "estimated"


def test_second_case_end_to_end(db, settings):
    settings.set_credential("BRAVE_API_KEY", "k")
    eng = engine_with(db, settings, ROUTES)
    inv = eng.create(parse_input(f"name: {TAVO}"), depth="2", options=_only_search_and_usernames())

    async def go():
        await eng.start(inv)
        return await _finish(eng, inv)
    rt = run(go())
    acc = rt.graph.get("account:instagram:tavo_in42")
    assert acc is not None and acc.get("display_name") == "Tavo Nullus" and acc.get("discovery_how") == "subpage"
    assert rt.graph.get("username:tavo_in42").get("pivot")


def test_without_a_search_provider_the_result_says_web_discovery_was_unavailable(db, settings):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input(f"name: {MIRA}"), depth="1", options=_only_search_and_usernames())
    rt = eng.runtime(inv)
    d = A.analyze(rt, eng, settings)
    assert "Web discovery unavailable — API-only search was used" in d["headline"]
    assert d["discovery"]["web_discovery"]["unavailable_message"]
    assert any(a["id"] == "web_unavailable" for a in d["next_actions"])


def test_one_provider_runs_and_the_next_takes_over_when_it_fails(db, settings):
    settings.set_credential("BRAVE_API_KEY", "k")
    settings.set_credential("SEARXNG_URL", "http://127.0.0.1:8888")
    settings.set("search_provider_order", "brave_search,searxng")
    routes = {"https://api.search.brave.com/": (500, {"error": "down"}), "http://127.0.0.1:8888/search": searx_index}
    eng = engine_with(db, settings, routes)
    inv = eng.create(parse_input(f"name: {MIRA}"), depth="1", options=_only_search_and_usernames())

    async def go():
        await eng.start(inv)
        return await _finish(eng, inv)
    rt = run(go())
    by = {}
    for r in rt.runs.values():
        by.setdefault(r.connector, []).append(r.transform)
    assert "brave_search" in by and "searxng" in by                      # primary tried, failover ran
    assert rt.data["search_failed"] == ["brave_search"]
    assert any(f["from"] == "brave_search" and f["to"] == "searxng" for f in rt.data["search_failovers"])
    assert rt.graph.get("account:pinterest:mirafictuse") is not None


def test_routing_never_pays_twice_for_the_same_search(db, settings):
    settings.set_credential("BRAVE_API_KEY", "k")
    settings.set_credential("SEARXNG_URL", "http://127.0.0.1:8888")
    eng = engine_with(db, settings, {**ROUTES, "http://127.0.0.1:8888/search": searx_index})
    inv = eng.create(parse_input(f"name: {MIRA}"), depth="1", options=_only_search_and_usernames())

    async def go():
        await eng.start(inv)
        return await _finish(eng, inv)
    rt = run(go())
    assert {r.connector for r in rt.runs.values() if r.connector in ("brave_search", "searxng")} == {"searxng"}  # free first


def test_budget_holds_billable_work_until_the_investigator_decides(db, settings):
    settings.set_credential("BRAVE_API_KEY", "k")
    eng = engine_with(db, settings, ROUTES)
    inv = eng.create(parse_input(f"name: {MIRA}"), depth="1",
                     options={**_only_search_and_usernames(), "budget_usd": 0.001})

    async def go():
        await eng.start(inv)
        return await _finish(eng, inv)
    rt = run(go())
    hold = rt.data.get("budget_hold")
    assert hold and hold["reason"] == "budget" and hold["pending"] and not any(
        r.connector == "brave_search" for r in rt.runs.values())

    async def cont():
        out = await eng.resolve_budget_hold(inv, proceed=True, new_budget=1.0)
        await _finish(eng, inv)
        return out
    out = run(cont())
    assert out["queued"] and rt.options["budget_usd"] == 1.0
    assert any(r.connector == "brave_search" for r in rt.runs.values())


def test_mock_index_fixture_is_query_specific():
    req = type("R", (), {"url": "https://api.search.brave.com/res/v1/web/search?q=%22nobody%22"})()
    assert brave_index(req)[1] == {"web": {"results": []}}
    assert json.dumps(INDEX)                                              # fixtures are plain data
