"""Global coverage: the 195-country registry, country-aware sources, the platform registry, source health, the
coverage model and SEARCH EVERYTHING. Offline (recorded responses only)."""

import json

import pytest

from conftest import engine_with, mock_http
from sextant.connectors.usernames.profile_probes import evaluate as probe_eval
from sextant.engine.countries import infer_countries
from sextant.engine.input import parse_input
from sextant.registry import connectors as REG
from sextant.registry import countries as C
from sextant.registry import coverage_model as CM
from sextant.registry import discovered as D
from sextant.registry import health as H
from sextant.registry import jurisdictions as J
from sextant.registry import platforms as P
from sextant.sdk.testing import run_transform, view

AFRICA = ("DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML MR MU MA MZ NA "
          "NE NG RW ST SN SC SL SO ZA SS SD TZ TG TN UG ZM ZW").split()
EUROPE_MIN = ("SE HU DE FR IT ES PT NL BE AT DK FI NO IS IE PL CZ SK SI HR RS BA ME MK AL BG GR MD UA LT LV EE LU MT CY "
              "CH LI").split()
OCEANIA = "AU NZ FJ KI MH FM NR PW PG WS SB TO TV VU".split()


# --------------------------------------------------------------------------- countries

def test_registry_has_exactly_195_sovereign_states():
    sov = C.sovereign()
    assert len(sov) == 195
    assert sum(1 for c in sov if c.status == "un_member") == 193
    assert {c.code for c in sov if c.status == "un_observer_state"} == {"VA", "PS"}
    assert not {"TW", "XK", "HK"} & {c.code for c in sov}                 # listed, but never counted among the 195
    assert {"TW", "XK", "HK"} <= set(C.countries())


@pytest.mark.parametrize("region,codes,count", [("Africa", AFRICA, 54), ("Oceania", OCEANIA, 14)])
def test_continents_are_individual_countries(region, codes, count):
    sov = {c.code: c for c in C.sovereign()}
    assert all(code in sov and sov[code].region == region for code in codes)
    assert sum(1 for c in sov.values() if c.region == region) == count


def test_minimum_european_list_and_americas_are_present():
    sov = {c.code: c for c in C.sovereign()}
    assert all(code in sov and sov[code].region == "Europe" for code in EUROPE_MIN)
    americas = [c for c in sov.values() if c.region in ("North America", "Central America & Caribbean", "South America")]
    assert len(americas) == 35
    assert sum(1 for c in sov.values() if c.region in ("Asia", "Middle East")) == 43


def test_every_country_is_a_jurisdiction_and_no_continent_placeholders():
    js = J.jurisdictions()
    assert all(c.code in js for c in C.sovereign())
    assert not {"AFR", "PAC"} & set(js)
    assert {code for code, j in js.items() if j.region == "International"} == {"INT", "EU"}
    regional = [s for s in J.all_sources() if s.regional]
    assert regional and all(s.country in C.countries() for s in regional)      # shared portals attach per country
    assert "EU" in js["SE"].blocs and "CoE" in js["UA"].blocs


def test_source_categories_cover_the_requirement():
    for group in ("government", "corporate", "public_records", "judicial", "professional", "regulatory", "procurement",
                  "news", "documents", "archives", "web", "social", "domains", "infrastructure", "commercial"):
        assert group in J.SOURCE_GROUPS
    assert set(J.KIND_GROUP) == set(J.SOURCE_KINDS)                             # every kind belongs to a group


def test_discovered_sources_carry_provenance_and_corroboration():
    items = D.all_discovered()
    assert len(items) > 1000
    for s in items[:500]:
        assert s.dataset in D.DATASETS and s.url.startswith(("https://", "http://")) and s.country in C.countries()
    assert D.dataset_info()["gleif_ra"]["retrieved_at"]
    se = D.for_country("SE")
    assert any(s.dataset == "gleif_ra" and s.kind == "company_registry" for s in se)


# --------------------------------------------------------------------------- country inference & input

@pytest.mark.parametrize("text,codes", [
    ("Jane Doe, jdoe92, +36 30 123 4567, location: Stockholm, Sweden", {"HU", "SE"}),
    ("example.de", {"DE"}),
    ("startup.io", set()),
    ("location: Abidjan, Côte d'Ivoire", {"CI"}),
    ("location: Thimphu, Bhutan", {"BT"}),
])
def test_countries_inferred_for_any_of_the_195(text, codes):
    assert {c["code"] for c in infer_countries(parse_input(text))} == codes


def test_comma_separated_input_keeps_locations_whole():
    p = parse_input("Jane Doe, jdoe92, location: Oulu, Finland")
    assert p.target_name == "Jane Doe"
    assert [(s.type, s.label) for s in p.seeds] == [("username", "jdoe92"), ("location", "Oulu, Finland")]


# --------------------------------------------------------------------------- platforms

REQUIRED_PLATFORMS = ("facebook youtube whatsapp instagram tiktok telegram messenger discord snapchat signal viber x threads "
                      "reddit tumblr bluesky mastodon pinterest twitch flickr quora medium deviantart behance dribbble "
                      "goodreads letterboxd soundcloud bandcamp wattpad linkedin wechat qq weibo douyin kuaishou line "
                      "kakaotalk vk ok bereal truthsocial rumble clubhouse gab minds spotify foursquare swarm strava myspace "
                      "github gitlab bitbucket sourceforge stackoverflow npm pypi dockerhub huggingface kaggle codepen "
                      "replit glitch devto hashnode codeberg steam xbox playstation epicgames battlenet riot_games ubisoft ea "
                      "roblox minecraft lastfm apple_music deezer tidal discogs imdb upwork freelancer_com fiverr toptal "
                      "wellfound tinder bumble hinge okcupid pof match").split()
REQUIRED_CATEGORIES = ("social messaging microblogging video music photography art professional gaming forums developer "
                       "dating fitness travel books film marketplaces freelance education academic").split()


def test_platform_registry_scale_and_required_entries():
    ids = {p.id for p in P.curated()}
    missing = [p for p in REQUIRED_PLATFORMS if p not in ids]
    assert not missing, missing
    assert len(P.curated()) >= 500 and len(P.curated()) + len(P.imported()) >= 5000
    cats = {p.category for p in P.curated()}
    assert set(REQUIRED_CATEGORIES) <= cats
    assert sum(1 for p in P.curated() if p.regional) >= 150                      # regional / local platforms


def test_registered_is_not_searchable():
    states = {n: REG.connector_status(c, None)[0] for n, c in REG.REGISTRY.items()}
    adapters = {"maigret": False, "sherlock": False}
    st = {p.id: P.search_status(p, states, adapters) for p in P.curated()}
    assert st["whatsapp"] == st["signal"] == st["bumble"] == "not_publicly_searchable"
    assert st["instagram"] == "api_key_required" and st["x"] == "subscription_required"
    assert st["facebook"] == "manual" and st["skype"] == "defunct" and st["github"] == "searchable"
    assert st["minecraft"] == "searchable"                                      # declarative public-API probe
    assert all(p.private_accounts for p in P.curated() if p.category == "dating" and p.check == "none")


def test_probe_evaluation_is_honest():
    spec = {"require": "id", "fields": {"display_name": "name"}}
    assert probe_eval(spec, 200, b'{"id": "1", "name": "ExampleCrafter"}', "ExampleCrafter")[:2] == ("found", {"display_name": "ExampleCrafter"})
    assert probe_eval(spec, 404, b"{}", "x")[0] == "not_found"
    assert probe_eval(spec, 403, b"", "x")[0] == "blocked"                         # never "not found"
    assert probe_eval(spec, 500, b"", "x")[0] == "unknown"
    listing = {"list_path": "data", "match": "name"}
    assert probe_eval(listing, 200, b'{"data": [{"name": "Other"}]}', "jdoe")[0] == "not_found"
    assert probe_eval(listing, 200, b'{"data": []}', "jdoe")[0] == "not_found"
    assert probe_eval({**listing, "accept_empty_list": True}, 200, b'{"data": []}', "jdoe")[0] == "found"


def test_profile_probes_connector_uses_declared_endpoints():
    routes = {"https://api.mojang.com/users/profiles/minecraft/ExampleCrafter": (200, {"id": "069a", "name": "ExampleCrafter"})}
    out, status, msg = run_transform("profile_probes.sweep", view("username", "ExampleCrafter"), http=mock_http(routes))
    assert status == "found" and "account:minecraft:ExampleCrafter".lower() in {k.lower() for k in out.entities}
    checks = {c.platform: c.status for c in out.checks}
    assert checks["minecraft"] == "found" and checks["scratch"] == "not_found"    # unknown URLs answer 404 in the mock


# --------------------------------------------------------------------------- health

def test_health_statuses_from_outcomes():
    rec = None
    for s in ("error", "error", "error"):
        rec = H.merge_record(rec, s, "2026-10-06T10:00:00+00:00")
    assert H._from_outcomes([x for _, x in rec["recent"]]) == "broken"
    assert H._from_outcomes(["found", "timeout", "rate_limited", "blocked"]) == "temporarily_unavailable"
    assert H._from_outcomes(["found", "found", "error", "found", "not_found"]) == "operational"
    assert H._from_outcomes(["found", "error", "found", "error"]) == "degraded"
    h = H.connector_health("x_api", None, None, {})
    assert h["status"] == "subscription_required"
    assert H.connector_health("linkedin", None, None, {})["status"] == "not_publicly_searchable"
    assert H.connector_health("bing_web_search", None, None, {})["status"] == "deprecated"
    assert H.connector_health("github", None, {}, {})["status"] == "unknown"      # never claims "operational" untested


def test_shipped_probe_snapshot_has_dates():
    snap = H.snapshot()
    assert snap.get("checked_at") and snap["connectors"]["github"]["checked_at"]


# --------------------------------------------------------------------------- coverage model

def test_coverage_matrix_is_computed_not_invented():
    ctx = CM.CoverageContext()
    m = CM.matrix(ctx)
    sov = [r for r in m["rows"] if r["sovereign"]]
    assert len(sov) == 195 and sum(m["summary"]["countries"]["levels"].values()) == 195
    for r in sov:
        assert r["level"] in CM.LEVEL_TITLES
        if r["level"] == "none":
            assert r["reason"] == CM.NO_CONNECTOR_REASON
        if r["level"] == "connectors":
            assert ctx.country_connectors(r["code"])
    assert CM._level(0, 0, 0) == "none" and CM._level(0, 0, 3) == "discovered" and CM._level(1, 0, 0) == "connectors"
    se = CM.country_detail("SE", ctx)
    assert se["curated"] and se["potential_sources"] and any(g["connector"] == "gdelt" for g in se["global_capabilities_detail"])
    assert "entire Internet" in m["summary"]["disclaimer"]


def test_country_connectors_cover_new_countries():
    ctx = CM.CoverageContext()
    for code, name in (("LV", "lv_ur"), ("SK", "sk_rpo"), ("IL", "il_companies"), ("TW", "tw_gcis"), ("CO", "co_secop")):
        assert name in ctx.country_connectors(code)


# --------------------------------------------------------------------------- new country connectors (recorded)

LV_COMPANY = {"result": {"records": [{"regcode": 40003245752, "name": "Air Baltic Corporation AS", "type_text": "Akciju sabiedrība",
                                      "address": "Mārupes nov., Tehnikas iela 3", "registered": "1995-08-28T00:00:00",
                                      "terminated": None}]}}
LV_OFFICERS = {"result": {"records": [{"id": 1, "entity_type": "NATURAL_PERSON", "name": "Paraudziņš Jānis", "position": "",
                                       "governing_body": "EXECUTIVE_BOARD", "latvian_identity_number_masked": "010170-*****",
                                       "registered_on": "2020-01-01T00:00:00"}]}}


def test_latvia_register_and_officers_without_identity_numbers():
    calls = []

    def routes():
        return {"https://data.gov.lv/dati/api/3/action/datastore_search?resource_id=25e80bf3": (200, LV_COMPANY),
                "https://data.gov.lv/dati/api/3/action/datastore_search?resource_id=e665114a": (200, LV_OFFICERS)}
    out, status, _ = run_transform("lv_ur.companies", view("organization", "name:air baltic corporation", label="Air Baltic Corporation",
                                                           name="Air Baltic Corporation", jurisdiction="LV"),
                                   http=mock_http(routes(), calls))
    assert status == "found" and "organization:lv-ur:40003245752" in out.entities
    persons = [d for d in out.entities.values() if d.type == "person"]
    assert [p.label for p in persons] == ["Jānis Paraudziņš"]
    assert not any("010170" in json.dumps(o.value, ensure_ascii=False) for o in out.observations)


def test_colombia_secop_contracts_are_records_with_official_links():
    rows = [{"id_contrato": "CO1.PCCNTR.1", "nombre_entidad": "MINISTERIO X", "proveedor_adjudicado": "ACME SAS",
             "valor_del_contrato": "1000", "fecha_de_firma": "2026-01-02T00:00:00", "objeto_del_contrato": "Servicios",
             "urlproceso": {"url": "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/Index?noticeUID=1"}}]
    out, status, _ = run_transform("co_secop.contracts", view("organization", "name:acme sas", label="ACME SAS", name="ACME SAS",
                                                              jurisdiction="CO"),
                                   http=mock_http({"https://www.datos.gov.co/resource/jbjy-vk9h.json": (200, rows)}))
    assert status == "found"
    rec = next(d for d in out.entities.values() if d.type == "public_record")
    vals = {o.prop: o.value for o in out.observations if o.entity == rec.key}
    assert vals["official_url"].startswith("https://community.secop.gov.co/") and vals["jurisdiction"] == "CO"


# --------------------------------------------------------------------------- SEARCH EVERYTHING & investigation coverage

def test_search_everything_is_country_aware(db, settings):
    eng = engine_with(db, settings, {})
    inv = eng.create(parse_input("Jane Doe, location: Stockholm, Sweden"), options={"search_everything": True})
    rt = eng.runtime(inv)
    assert rt.info.countries == ("SE",)
    assert eng._connector_available(rt, REG.REGISTRY["ro_courts"])[0] == "disabled"      # Romanian source, Swedish case
    status, note = eng._connector_available(rt, REG.REGISTRY["hudsonrock"])
    assert status == "available" and note == "Enabled by SEARCH EVERYTHING"
    inv2 = eng.create(parse_input("Jane Doe, location: Romania"))
    st2, note2 = eng._connector_available(eng.runtime(inv2), REG.REGISTRY["ro_courts"])
    assert st2 == "available" and note2.startswith("Pre-selected")


def test_investigation_coverage_reports_global_counts(db, settings):
    from test_engine import investigate
    eng, inv, rt = investigate(db, settings, "Jane Doe\njdoe92\nlocation: Riga, Latvia", depth="1")
    g = eng.coverage(inv)["global"]
    assert g["countries"]["evaluated"] == 195 and [a["code"] for a in g["countries"]["applicable"]] == ["LV"]
    assert g["platforms"]["registered"] == len(P.curated()) + len(P.imported())
    assert "did not search the entire Internet" in g["disclaimer"]


def test_raw_evidence_is_referenced_and_stored_encrypted(db, settings):
    import hashlib
    from test_engine import ROUTES, investigate
    eng, inv, rt = investigate(db, settings, "Jane Doe\njdoe92", depth="1")
    srcs = [s for s in rt.graph.sources.values() if s.connector == "t_profile"]
    assert srcs and srcs[0].raw_refs
    raw = db.get_raw(inv, srcs[0].raw_refs[0])
    import base64
    body = base64.b64decode(raw["body"])
    assert hashlib.sha256(body).hexdigest() == srcs[0].raw_refs[0] and raw["url"].startswith("https://t.example/u/")
    plain = (db.data_dir / "sextant.sqlite3").read_bytes()
    assert b"janedoe.dev" not in plain                                           # the stored body is encrypted
    assert ROUTES


def test_search_everything_plan_and_country_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from sextant.api.app import build_state, create_app
    state = build_state(tmp_path / "data", tmp_path / "ui", "tok", 8765, http_factory=lambda **kw: mock_http({}, **kw))
    h = {"X-Sextant-Token": "tok"}
    with TestClient(create_app(state), base_url="http://127.0.0.1:8765") as c:
        r = c.post("/api/parse", headers=h, json={"text": "Jane Doe, location: Budapest, Hungary", "search_everything": True})
        d = r.json()
        assert [x["code"] for x in d["countries"]] == ["HU"] and d["search_everything"]
        assert not any(p["name"] == "ro_courts" and p["status"] == "available" for p in d["plan"])
        assert c.get("/api/global-coverage", headers=h).json()["summary"]["countries"]["total"] == 195
        assert c.get("/api/global-coverage/ZZ", headers=h).status_code == 404
        r = c.post("/api/custom-sources", headers=h, json={"country": "VA", "title": "Vatican News",
                                                            "url": "https://www.vaticannews.va/", "kind": "news_media"})
        sid = r.json()["id"]
        assert any(s["id"] == sid for s in c.get("/api/global-coverage/VA", headers=h).json()["custom"])
        assert c.post("/api/custom-sources", headers=h, json={"country": "VA", "title": "Bad link", "url": "javascript:alert(1)"}).status_code == 400
        assert c.delete(f"/api/custom-sources/{sid}", headers=h).json()["ok"]
        p = c.get("/api/platforms?category=dating&limit=500", headers=h).json()
        assert p["total"] >= 20 and all(r["category"] == "dating" for r in p["rows"])
    state.db.close()


def test_national_registers_run_only_for_the_investigations_countries():
    from sextant.connectors.companies._registry import org_accepts
    from sextant.sdk.transform import InvestigationInfo
    seed = view("organization", "name:air baltic", label="Air Baltic", name="Air Baltic")
    accepts_no = org_accepts("NO")
    accepts_lv = org_accepts("LV")
    assert accepts_lv(None, seed, InvestigationInfo(countries=("LV",)))
    assert not accepts_no(None, seed, InvestigationInfo(countries=("LV",)))     # name not sent to Norway's register
    assert accepts_no(None, seed, InvestigationInfo())                          # country unknown: every register
    found = view("organization", "lv-ur:1", label="Xyz SIA", name="Xyz SIA", jurisdiction="LV", status="candidate")
    assert accepts_lv(None, found, InvestigationInfo()) and not accepts_no(None, found, InvestigationInfo())


def test_every_connector_reports_transparency_labels():
    seen = set()
    for name, conn in REG.REGISTRY.items():
        status = REG.connector_status(conn, None)[0]
        t = REG.transparency(conn, status)
        assert t["requirement"] in ("Free", "API key required", "Account required (approval)", "Subscription required",
                                    "Unavailable")
        assert t["search"] in ("Publicly searchable", "Limited", "Not publicly searchable")
        seen.add(t["requirement"])
    assert {"Free", "API key required", "Subscription required", "Unavailable"} <= seen
    assert REG.transparency(REG.REGISTRY["linkedin"], "manual")["search"] == "Not publicly searchable"


def test_commercial_providers_are_listed_per_country():
    de = CM.country_detail("DE", CM.CoverageContext())
    providers = [s for s in de["curated"] if s["kind"] == "commercial_provider"]
    assert providers and all(s["access"] == "paid" for s in providers)
    assert CM.summary(CM.CoverageContext())["commercial"]["countries_with_providers"] >= 40


def test_installed_adapter_that_is_off_is_not_counted_as_searchable():
    p = next(x for x in P.imported() if x.check == "maigret" and x.dataset_valid)
    states = {n: REG.connector_status(c, None)[0] for n, c in REG.REGISTRY.items()}
    assert P.search_status(p, states, {"maigret": "available"}) == "searchable"
    assert P.search_status(p, states, {"maigret": "disabled"}) == "adapter_off"
    assert P.search_status(p, states, {}) == "adapter_not_installed"
    assert "adapter_off" not in P.SEARCHABLE and "adapter_off" not in P.UNAVAILABLE


def test_adapter_site_databases_are_filtered_by_policy(tmp_path):
    import json as _json
    from sextant.connectors.adapters.tools import write_policy_db
    src = tmp_path / "maigret.json"
    src.write_text(_json.dumps({"engines": {}, "tags": [], "sites": {
        "GitHub": {"urlMain": "https://github.com/", "tags": ["coding"]},
        "Instagram": {"urlMain": "https://www.instagram.com/"},
        "ImgInn": {"urlMain": "https://imginn.com/", "source": "Instagram"},
        "Nitter": {"urlMain": "https://nitter.net/", "source": "Twitter"},
        "SomeCam": {"urlMain": "https://somecam.example/", "tags": ["webcam"]}}}))
    out = tmp_path / "out.json"
    assert write_policy_db(src, out, "maigret", include_adult=False) == (1, 4)
    assert set(_json.loads(out.read_text())["sites"]) == {"GitHub"}
    assert write_policy_db(src, out, "maigret", include_adult=True) == (2, 3)
    sher = tmp_path / "sherlock.json"
    sher.write_text(_json.dumps({"$schema": "x", "GitHub": {"url": "https://github.com/{}"},
                                 "TikTok": {"url": "https://www.tiktok.com/@{}"}}))
    assert write_policy_db(sher, out, "sherlock", include_adult=False) == (1, 1)
    assert set(_json.loads(out.read_text())) == {"$schema", "GitHub"}


def test_maigret_counts_split_normal_runs_from_search_everything():
    p = next(x for x in P.imported() if x.check == "maigret" and x.dataset_valid and not x.mirror_of
             and x.category not in ("adult", "dating"))
    states = {n: REG.connector_status(c, None)[0] for n, c in REG.REGISTRY.items()}
    on = {"maigret": "available", "maigret_ids": frozenset(p.dataset_ids)}
    assert P.search_status(p, states, {**on, "maigret_top": frozenset(p.dataset_ids)}) == "searchable"
    assert P.search_status(p, states, {**on, "maigret_top": frozenset()}) == "search_everything_only"
    assert P.search_status(p, states, {**on, "maigret_ids": frozenset()}) == "no_rule"   # not in the installed version
    mirror = next(x for x in P.imported() if x.mirror_of.lower() in ("instagram", "twitter", "tiktok"))
    assert P.search_status(mirror, states, {"maigret": "available"}) == "excluded"
    assert REG.REGISTRY["maigret"].manifest.default_enabled and REG.REGISTRY["sherlock"].manifest.default_enabled


def test_current_maigret_db_follows_maigrets_update_channel(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    import json as _json
    from sextant.connectors.adapters import tools as T
    body = _json.dumps({"sites": {"GitHub": {"urlMain": "https://github.com/"}}, "engines": {}, "tags": []}).encode()
    good = {"updated_at": "2026-10-04T16:47:37Z", "min_maigret_version": "0.5.0", "data_sha256": hashlib.sha256(body).hexdigest(),
            "data_url": "https://raw.githubusercontent.com/soxoj/maigret/main/maigret/resources/data.json"}
    monkeypatch.setattr(T, "tool_eval", lambda *a, **k: "0.6.6")

    class Ctx:
        def __init__(self, meta, data_dir):
            self.data_dir = data_dir
            self.http = mock_http({T.MAIGRET_META_URL: (200, meta), good["data_url"]: (200, body.decode())})

        async def get(self, url, evidence=True, **kw):
            return await self.http.request("GET", url, **kw)

    path, note = asyncio.run(T.current_maigret_db(Ctx(good, tmp_path / "a"), "/fake/maigret"))
    assert path and path.read_bytes() == body and "2026-10-04" in note
    path, note = asyncio.run(T.current_maigret_db(Ctx({**good, "min_maigret_version": "9.9.9"}, tmp_path / "b"), "/fake"))
    assert path is None and "needs Maigret" in note
    path, note = asyncio.run(T.current_maigret_db(Ctx({**good, "data_sha256": "0" * 64}, tmp_path / "c"), "/fake"))
    assert path is None and "SHA-256" in note


def test_maigret_results_are_discarded_if_it_ignores_the_filtered_list(tmp_path, monkeypatch):
    import json as _json
    from sextant.connectors.adapters import tools as T
    db = tmp_path / "bundled.json"
    db.write_text(_json.dumps({"sites": {"GitHub": {"urlMain": "https://github.com/"}}, "engines": {}, "tags": []}))
    monkeypatch.setattr(T, "find_tool", lambda *a, **k: "/fake/maigret")
    monkeypatch.setattr(T, "tool_package_file", lambda *a, **k: db)
    monkeypatch.setattr(T, "tool_eval", lambda *a, **k: "1")

    async def fake_run(argv, *, timeout, workdir):
        return 0, "[!] Falling back to bundled database: /x/data.json\n", ""
    monkeypatch.setattr(T, "run_tool", fake_run)
    _, status, msg = run_transform("maigret.search", view("username", "octocat"), http=mock_http({}),
                                   settings={"maigret_database": "bundled"})
    assert status == "error" and "discarded" in msg
