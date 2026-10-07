"""Orchestrator end-to-end with test connectors (isolated in the 'adapters' category so no real source runs)."""

import pytest

from conftest import engine_with, run
from sextant.engine.input import parse_input
from sextant.sdk import Connector, Credential, Disclosure, Manifest, register, transform

SEEN_PROPS: list[dict] = []
OPTS = {"categories": ["adapters"]}


@register
class TProfile(Connector):
    manifest = Manifest(name="t_profile", title="Test profiles", category="adapters", description="test",
                        website="https://t.example", disclosure=Disclosure(("t.example",), "username"))

    @transform(inputs=("username",), outputs=("account", "domain", "email"), title="Test: account by username")
    async def lookup(self, entity, ctx):
        resp = await ctx.get(f"https://t.example/u/{entity.display}")
        d = ctx.json(ctx.expect(resp))
        src = ctx.out.source(resp.url, title="Test API")
        acc = ctx.out.entity("account", f"t:{d['login']}", source=src,
                             props={"username": d["login"], "display_name": d["name"], "platform": "t"})
        ctx.out.link(acc, entity.key, "USES", source=src)
        dom = ctx.out.entity("domain", d["site"], source=src)
        ctx.out.link(acc, dom, "LINKED_TO", source=src)
        if d.get("email"):
            em = ctx.out.entity("email", d["email"], source=src)
            ctx.out.link(acc, em, "USES", source=src)

    @transform(inputs=("person",), outputs=("username",), title="Test: record what the transform can see")
    async def peek(self, entity, ctx):
        SEEN_PROPS.append(dict(entity.props))


@register
class TDomain(Connector):
    manifest = Manifest(name="t_domain", title="Test domains", category="adapters", description="test",
                        website="https://t.example", disclosure=Disclosure(("dns.example",), "domain"))

    @transform(inputs=("domain",), outputs=("ip",), title="Test: resolve")
    async def resolve(self, entity, ctx):
        src = ctx.out.source(f"https://dns.example/{entity.value}")
        ip = ctx.out.entity("ip", "93.184.216.34", source=src)
        ctx.out.link(entity.key, ip, "RESOLVES_TO", source=src)

    @transform(inputs=("email",), outputs=(), title="Test: always crashes")
    async def crash(self, entity, ctx):
        raise RuntimeError("boom")


@register
class TKeyed(Connector):
    manifest = Manifest(name="t_keyed", title="Test keyed", category="adapters", description="test", access="free_key",
                        credentials=(Credential("T_KEY", "Test key"),), website="https://t.example",
                        disclosure=Disclosure(("keyed.example",), "username"))

    @transform(inputs=("username",), outputs=(), title="Test: needs a key")
    async def lookup(self, entity, ctx):
        ctx.require("T_KEY")


ROUTES = {"https://t.example/u/jdoe92": (200, {"login": "jdoe92", "name": "Jane Doe", "site": "janedoe.dev",
                                               "email": "jane@janedoe.dev"})}


def investigate(db, settings, text, depth="2", routes=ROUTES, overrides=None, mode="auto"):
    eng = engine_with(db, settings, routes)
    inv = eng.create(parse_input(text), depth=depth, options=OPTS, overrides=overrides)
    if mode == "auto":
        rt = run(_go(eng, inv))
    else:
        rt = eng.runtime(inv)
    return eng, inv, rt


async def _go(eng, inv):
    rt = await eng.start(inv)
    await rt.loop_task
    return rt


def runs(rt, transform=None):
    return [r for r in rt.runs.values() if transform is None or r.transform == transform]


def test_depth_one_only_expands_the_input(db, settings):
    _, _, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="1")
    assert rt.status == "completed" and runs(rt, "t_profile.lookup") and not runs(rt, "t_domain.resolve")
    assert rt.graph.get("domain:janedoe.dev").depth == 1


def test_depth_two_follows_medium_leads_and_stops_at_the_boundary(db, settings):
    _, _, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="2")
    assert [r.input_key for r in runs(rt, "t_domain.resolve")] == ["domain:janedoe.dev"]
    ip = rt.graph.get("ip:93.184.216.34")
    assert ip is not None and ip.depth == 2                     # found at depth 2, not expanded further
    acc = rt.graph.get("account:t:jdoe92")
    assert acc.confidence > 0.5 and {r.feature for r in acc.reasons} >= {"username", "name_full"}


def test_each_transform_runs_once_per_entity(db, settings):
    _, _, rt = investigate(db, settings, "jdoe92", depth="3")
    pairs = [(r.transform, r.input_key) for r in rt.runs.values()]
    assert len(pairs) == len(set(pairs))


def test_errors_are_isolated_and_coverage_is_honest(db, settings):
    eng, inv, rt = investigate(db, settings, "jdoe92", depth="2")
    crash = runs(rt, "t_domain.crash")
    assert crash and crash[0].status == "error" and "RuntimeError" in crash[0].message
    assert runs(rt, "t_domain.resolve")                                   # the crash did not stop the rest
    cov = {c["connector"]: c for c in eng.coverage(inv)["connectors"]}
    assert cov["t_keyed"]["state"] == "needs_credentials"
    assert cov["t_profile"]["state"] == "queried" and cov["t_profile"]["runs"]["found"] >= 1
    assert "never proves" in eng.coverage(inv)["disclaimer"]


def test_local_only_birth_year_never_reaches_a_transform(db, settings):
    SEEN_PROPS.clear()
    investigate(db, settings, "name: Jane Doe\nborn: 1971", depth="1")
    assert SEEN_PROPS and all("birth_year" not in p for p in SEEN_PROPS)
    assert "full_name" in SEEN_PROPS[0]


def test_entity_budget_stops_scheduling_and_continue_resumes(db, settings):
    eng, inv, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="2", overrides={"max_entities": 4})
    assert rt.status == "limit_reached" and "entities" in rt.stop_reason and len(rt.queue) > 0
    rt = run(_continue(eng, inv, {"max_entities": 500}))
    assert rt.status == "completed" and runs(rt, "t_domain.resolve")


async def _run_and_wait(eng, inv, entity_id, transforms):
    res = await eng.run_transforms(inv, entity_id, transforms)
    await eng.runtime(inv).loop_task
    return res


async def _continue(eng, inv, overrides):
    rt = await eng.continue_(inv, overrides)
    await rt.loop_task
    return rt


def test_manual_mode_runs_only_what_the_investigator_asks(db, settings):
    eng, inv, rt = investigate(db, settings, "jdoe92", mode="manual")
    assert not rt.runs
    seed = rt.graph.get("username:jdoe92")
    options = {t["name"]: t for t in eng.applicable_transforms(inv, seed.id)}
    assert options["t_profile.lookup"]["status"] == "available" and options["t_keyed.lookup"]["status"] == "needs_credentials"
    res = run(_run_and_wait(eng, inv, seed.id, ["t_profile.lookup"]))
    assert res["queued"] == ["t_profile.lookup"]
    assert [r.transform for r in rt.runs.values()] == ["t_profile.lookup"]
    assert all(r.trigger == "user" for r in rt.runs.values())


def test_decisions_rescore_and_are_audited(db, settings):
    eng, inv, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="1")
    acc = rt.graph.get("account:t:jdoe92")
    run(eng.decide(inv, acc.id, "confirm", "verified in person"))
    assert acc.status == "confirmed" and acc.confidence == 1.0
    assert any(r.type == "OWNS" and r.target == acc.key for r in rt.graph.in_edges(acc.key))
    assert rt.graph.get("email:jane@janedoe.dev").confidence == 1.0       # identifiers of a confirmed account follow
    run(eng.decide(inv, acc.id, "separate"))
    assert acc.confidence == 0.0
    assert not [r for r in rt.graph.in_edges(acc.key) if r.type == "OWNS" and r.status == "active"]
    log = db.decisions(inv)
    assert [d["action"] for d in log] == ["confirm", "separate"] and log[0]["note"] == "verified in person"
    with pytest.raises(ValueError):
        run(eng.decide(inv, rt.graph.get("username:jdoe92").id, "ignore"))     # seeds are input, not findings


def test_everything_survives_a_restart(db, settings):
    eng, inv, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="2")
    fresh = engine_with(db, settings, ROUTES)
    rt2 = fresh.runtime(inv)
    assert len(rt2.graph.entities) == len(rt.graph.entities)
    assert len(rt2.graph.relationships) == len(rt.graph.relationships)
    assert len(rt2.graph.observations) == len(rt.graph.observations)
    assert len(rt2.runs) == len(rt.runs) and rt2.status == "completed"
    acc = rt2.graph.get("account:t:jdoe92")
    assert acc.get("display_name") == "Jane Doe" and acc.reasons
    assert all(o.source in rt2.graph.sources for o in rt2.graph.observations.values())


def test_limit_overrides_are_parsed_strictly_and_capped():
    from sextant.engine.limits import HARD_CEILING, resolve_limits
    lim = resolve_limits("4", {"username_guesses": "false", "max_entities": "999999", "expand_threshold": "0.3"})
    assert lim.username_guesses is False and lim.expand_threshold == 0.3
    assert lim.max_entities == HARD_CEILING.max_entities
    assert resolve_limits("2", {"username_guesses": "true"}).username_guesses is True


def test_graph_changes_made_while_a_flush_is_writing_are_kept(db):
    # the engine writes in a worker thread while transforms keep merging results on the event loop
    import threading
    from contextlib import contextmanager
    from sextant.model import Graph, RelEvidence
    inv = db.create_investigation({}, "person", "2")
    g = Graph(db.id_fn(inv))
    root, _ = g.ensure_entity("username", "jdoe92")
    pending = db.prepare_graph(inv, g)
    writing, release, tx = threading.Event(), threading.Event(), db.tx

    @contextmanager
    def gated_tx():
        with tx() as c:
            writing.set()
            release.wait(5)
            yield c

    db.tx = gated_tx
    worker = threading.Thread(target=db.write_graph, args=(pending,))
    worker.start()
    assert writing.wait(5)
    for i in range(50):
        e, _ = g.ensure_entity("domain", f"site{i}.example")
        g.add_relationship(root.key, e.key, "LINKED_TO", RelEvidence("t", "mid-write"))
    release.set()
    worker.join(5)
    db.tx = tx
    db.flush_graph(inv, g)
    loaded = db.load_graph(inv, None, {}, [])
    assert len(loaded.entities) == 51 and len(loaded.relationships) == 50


def test_a_failed_investigation_stops_its_running_transforms(db, settings):
    import asyncio
    import httpx
    from sextant.engine.orchestrator import Engine
    from sextant.sdk.http import SafeHttpClient

    async def handler(request):
        if request.url.host == "t.example":
            await asyncio.sleep(60)                     # still in flight when the investigation fails
        return httpx.Response(404, json={})

    eng = Engine(db, settings, db.data_dir,
                 http_factory=lambda **kw: SafeHttpClient(transport=httpx.MockTransport(handler), **kw))
    inv = eng.create(parse_input("jdoe92"), depth="1", options=OPTS)
    checks = iter(range(10))

    def limit_hit(rt, requests):
        if next(checks):
            raise RuntimeError("engine fault")
        return ""

    eng._limit_hit = limit_hit

    async def go():
        rt = await eng.start(inv)
        await asyncio.wait_for(rt.loop_task, 10)
        return rt, [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    rt, leftover = run(go())
    assert rt.status == "failed" and not leftover
    hung = runs(rt, "t_profile.lookup")[0]
    assert hung.status == "cancelled" and rt.tasks[hung.task_id].status == "queued"
    assert not any(rt.running.values())
