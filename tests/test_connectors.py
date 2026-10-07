"""Connectors with recorded responses (offline). Each test asserts statuses, entities, provenance and policy."""

import base64

import pytest

from conftest import mock_http
from sextant.connectors.adapters.tools import parse_harvester, parse_maigret_report, parse_spiderfoot
from sextant.connectors.email.email_sources import local_part_username, openpgp_user_ids
from sextant.connectors.legal_records import courts
from sextant.connectors.legal_records.courts import parse_find_case_law
from sextant.connectors.rdap.rdap import parse_rdap
from sextant.connectors.sanctions import lists as wl
from sextant.connectors.usernames import whatsmyname as wmn
from sextant.sdk.testing import run_transform, view
from sextant.sdk.transform import InvestigationInfo

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def ents(out, t):
    return {k: d for k, d in out.entities.items() if d.type == t}


def props(out, key):
    return {o.prop: o.value for o in out.observations if o.entity == key}


# --------------------------------------------------------------------------- platforms

def test_github_username_lookup_extracts_profile_with_provenance():
    routes = {
        "https://api.github.com/users/octocat/social_accounts": (200, [{"provider": "twitter", "url": "https://twitter.com/github"}]),
        "https://api.github.com/users/octocat": (200, {"login": "octocat", "name": "The Octocat", "company": "@github",
                                                       "blog": "https://github.blog", "location": "San Francisco",
                                                       "email": None, "html_url": "https://github.com/octocat", "id": 583231,
                                                       "avatar_url": "https://avatars.githubusercontent.com/u/583231"}),
        "https://avatars.githubusercontent.com/": (200, PNG, {"content-type": "image/png"}),
    }
    out, status, _ = run_transform("github.username_lookup", view("username", "octocat", username="octocat"),
                                   http=mock_http(routes))
    assert status == "found"
    acc = props(out, "account:github:octocat")
    assert acc["display_name"] == "The Octocat" and acc["website"] == "https://github.blog"
    assert "domain:github.blog" in out.entities and "account:x:github" in out.entities
    assert "organization:name:github" in out.entities and "location:san francisco" in out.entities
    assert all(o.source in out.sources for o in out.observations)                  # every fact has a source
    assert out.checks[0].status == "found"


def test_github_not_found_and_rate_limit():
    out, status, _ = run_transform("github.username_lookup", view("username", "nobody-xyz"), http=mock_http({}))
    assert status == "not_found" and out.checks[0].status == "not_found"
    limited = mock_http({"https://api.github.com/users/": (403, {"message": "rate"}, {"x-ratelimit-remaining": "0"})})
    _, status, msg = run_transform("github.username_lookup", view("username", "octocat"), http=limited)
    assert status == "rate_limited" and "GITHUB_TOKEN" in msg


@pytest.mark.parametrize("name,url,body,status_code,expected", [
    ("gitlab.username_lookup", "https://gitlab.com/api/v4/users", [], 200, "not_found"),
    ("gitlab.username_lookup", "https://gitlab.com/api/v4/users", [{"username": "jdoe", "name": "J D", "web_url": "https://gitlab.com/jdoe", "public_email": "j@x.org"}], 200, "found"),
    ("hackernews.username_lookup", "https://hacker-news.firebaseio.com/", None, 200, "not_found"),
    ("lichess.username_lookup", "https://lichess.org/api/user/", {"error": "Not found"}, 404, "not_found"),
    ("huggingface.username_lookup", "https://huggingface.co/api/users/", {"user": "jdoe", "fullname": "Jane Doe"}, 200, "found"),
    ("crates.username_lookup", "https://crates.io/api/v1/users/", {"errors": [{"detail": "Not Found"}]}, 404, "not_found"),
    ("lemmy.username_lookup", "https://lemmy.world/api/v3/user", {"error": "couldnt_find_person"}, 404, "not_found"),
    ("keybase.username_lookup", "https://keybase.io/_/api/", {"status": {"code": 100}}, 200, "not_found"),
    ("dockerhub.username_lookup", "https://hub.docker.com/v2/users/", {"message": "User not found"}, 404, "not_found"),
])
def test_platform_factory_statuses(name, url, body, status_code, expected):
    out, status, _ = run_transform(name, view("username", "jdoe", username="jdoe"), http=mock_http({url: (status_code, body)}))
    assert status == expected
    if expected == "found":
        assert ents(out, "account") and out.checks[-1].status == "found"


def test_blocked_platform_is_never_reported_as_not_found():
    _, status, _ = run_transform("codeberg.username_lookup", view("username", "jdoe"),
                                 http=mock_http({"https://codeberg.org/": (403, "denied")}))
    assert status == "blocked"


def test_linked_account_profile_is_fetched():
    acc = view("account", "gitlab:jdoe", username="jdoe", platform="gitlab", found_by="link")
    out, status, _ = run_transform("gitlab.account_profile", acc, http=mock_http({
        "https://gitlab.com/api/v4/users": (200, [{"username": "jdoe", "name": "Jane Doe", "public_email": "jane@x.org"}])}))
    assert status == "found" and "email:jane@x.org" in out.entities


def test_bluesky_not_found_and_npm_maintainer_email():
    _, status, _ = run_transform("bluesky.username_lookup", view("username", "nobodyxyz"), http=mock_http({
        "https://public.api.bsky.app/": (400, {"error": "InvalidRequest", "message": "Profile not found"})}))
    assert status == "not_found"
    out, status, _ = run_transform("npm.maintainer_lookup", view("username", "jdoe"), http=mock_http({
        "https://registry.npmjs.org/-/v1/search": (200, {"total": 1, "objects": [{"package": {
            "name": "left-pad", "maintainers": [{"username": "jdoe", "email": "jdoe@example.org"}],
            "links": {"homepage": "https://jdoe.dev"}}}]})}))
    assert status == "found" and "email:jdoe@example.org" in out.entities and "domain:jdoe.dev" in out.entities


# --------------------------------------------------------------------------- email, domains

def test_email_local_part_and_local_analysis():
    assert local_part_username("jdoe.fictional92@gmail.com")[0] == "jdoe.fictional92"
    assert local_part_username("info@acme.com") is None
    out, status, _ = run_transform("email_analysis.analyse", view("email", "jdoe.fictional92@gmail.com"), http=mock_http({}))
    assert "username:jdoe.fictional92" in out.entities and not ents(out, "domain")        # free provider: no MX lookup


def armor(packets: bytes) -> str:
    return "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n" + base64.b64encode(packets).decode() + "\n=abcd\n-----END PGP PUBLIC KEY BLOCK-----\n"


def test_openpgp_user_ids_parser():
    uid = "Jane Doe <jane@example.org>".encode()
    pkt = bytes([0x98, 4]) + b"\x04\x00\x00\x00" + bytes([0xCD, len(uid)]) + uid
    assert openpgp_user_ids(armor(pkt)) == ["Jane Doe <jane@example.org>"]
    assert openpgp_user_ids("garbage") == []
    out, status, _ = run_transform("openpgp_keyserver.key_lookup", view("email", "jane@example.org"),
                                   http=mock_http({"https://keys.openpgp.org/": (200, armor(pkt), {"content-type": "text/plain"})}))
    assert status == "found" and "name:doe jane" in out.entities


def test_rdap_hides_redacted_registrant():
    data = {"events": [{"eventAction": "registration", "eventDate": "2010-01-01"}],
            "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "REDACTED FOR PRIVACY"],
                                                                         ["org", {}, "text", "Acme Ltd"]]]},
                         {"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Registrar Inc"]]]}]}
    p = parse_rdap(data)
    assert p["registrant"] == {"org": "Acme Ltd"} and p["registrar"] == "Registrar Inc" and p["registered"] == "2010-01-01"


def test_website_reads_contacts_and_rel_me_only_on_target_domain():
    html = ('<html><head><title>Jane</title><meta name="author" content="Jane Doe"></head><body>'
            '<a href="mailto:jane@janedoe.dev">mail</a> <a rel="me" href="https://mastodon.social/@jdoe">m</a>'
            '<a href="https://github.com/jdoe">gh</a> +358 40 123 4567</body></html>')
    routes = {"https://janedoe.dev/": (200, html, {"content-type": "text/html"})}
    out, status, _ = run_transform("website.pages", view("domain", "janedoe.dev", status="seed"), http=mock_http(routes),
                                   info=InvestigationInfo(default_region="FI"))
    assert status == "found"
    assert {"email:jane@janedoe.dev", "account:mastodon:jdoe", "account:github:jdoe", "phone:+358401234567"} <= set(out.entities)
    out2, _, _ = run_transform("website.pages", view("domain", "janedoe.dev", status="candidate"), http=mock_http(routes))
    assert "account:mastodon:jdoe" in out2.entities and "account:github:jdoe" not in out2.entities   # rel=me only


# --------------------------------------------------------------------------- WhatsMyName

def site(name, uri, e_code=200, e_string="profile-ok", m_code=404, m_string="", cat="social", **extra):
    return {"name": name, "uri_check": uri, "e_code": e_code, "e_string": e_string, "m_code": m_code,
            "m_string": m_string, "known": ["x"], "cat": cat, **extra}


DATASET = {"sites": [
    site("ExampleSocial", "https://social.example/u/{account}"),
    site("MissingSite", "https://missing.example/{account}"),
    site("RobotsSite", "https://robots.example/{account}"),
    site("ShieldSite", "https://shield.example/{account}"),
    site("SpoofSite", "https://spoof.example/{account}", headers={"User-Agent": "Mozilla/5.0 fake browser"}),
    site("Facebook", "https://www.facebook.com/{account}/"),
    site("X", "https://api.x.com/i/users/username_available.json?username={account}"),
    site("Instagram (Imginn)", "https://imginn.com/{account}/"),
    site("GitHub", "https://github.com/{account}"),
    site("AdultSite", "https://adult.example/{account}", cat="xx NSFW xx"),
    site("CookieSite", "https://cookie.example/{account}", headers={"Cookie": "accept=1"}),
]}
WMN_ROUTES = {
    "https://raw.githubusercontent.com/": (200, DATASET),
    "https://robots.example/robots.txt": (200, "User-agent: *\nDisallow: /", {"content-type": "text/plain"}),
    "https://social.example/u/jdoe_x92": (200, '<html><head><meta property="og:title" content="Jane Doe (@jdoe_x92) • ExampleSocial">'
                                               '</head><body>profile-ok <a rel="me" href="https://mastodon.social/@jdoe_x92">m</a></body></html>',
                                          {"content-type": "text/html"}),
    "https://missing.example/": (404, "nope", {"content-type": "text/html"}),
    "https://shield.example/jdoe": (403, "<html>Just a moment... cf-chl</html>", {"content-type": "text/html"}),
    "https://spoof.example/jdoe": (200, "profile-ok", {"content-type": "text/html"}),
}


def test_wmn_policy_and_rules():
    sites, excluded = wmn.load_sites(DATASET, include_adult=False)
    reasons = {e["name"]: e["reason"] for e in excluded}
    assert {"Facebook", "X", "Instagram (Imginn)", "GitHub", "AdultSite", "CookieSite"} <= set(reasons)
    assert "terms" in reasons["Facebook"] and "dedicated" in reasons["GitHub"]
    s = wmn.Site("S", "social", "https://s/{account}", "https://s/{account}", 200, "marker", 404, "")
    assert wmn.evaluate(s, 200, "... marker ...")[0] == "found"
    assert wmn.evaluate(s, 404, "gone")[0] == "not_found"
    assert wmn.evaluate(s, 403, "Just a moment...")[0] == "blocked"
    assert wmn.evaluate(s, 302, "")[0] == "unknown"
    assert wmn.display_name_from_title("Jane Doe (@jdoe) • Pinterest", "jdoe", "Pinterest") == "Jane Doe"
    assert wmn.display_name_from_title("jdoe - Twitch", "jdoe", "Twitch") is None


def test_wmn_sweep_respects_policy_and_never_spoofs(tmp_path):
    calls = []
    out, status, msg = run_transform("whatsmyname.sweep", view("username", "jdoe_x92", username="jdoe_x92"),
                                     http=mock_http(WMN_ROUTES, calls), data_dir=tmp_path,
                                     info=InvestigationInfo(options={"respect_robots": True}))
    checks = {c.platform_title: c.status for c in out.checks}
    assert checks["ExampleSocial"] == "found" and checks["MissingSite"] == "not_found"
    assert checks["RobotsSite"] == "skipped" and checks["ShieldSite"] == "blocked" and checks["Facebook"] == "skipped"
    urls = [str(c.url) for c in calls]
    assert not any(h in u for u in urls for h in ("facebook.com", "api.x.com", "imginn.com", "adult.example", "cookie.example"))
    assert not any("robots.example/jdoe" in u for u in urls)
    spoof = next(c for c in calls if "spoof.example" in str(c.url))
    assert spoof.headers["user-agent"].startswith("Sextant/")
    assert "account:mastodon:jdoe_x92" in out.entities and "sites checked" in msg
    assert (tmp_path / "wmn" / "wmn-data.json").exists()


# --------------------------------------------------------------------------- sanctions & legal

UN_XML = b"""<?xml version="1.0" encoding="UTF-8"?><CONSOLIDATED_LIST><INDIVIDUALS>
<INDIVIDUAL><DATAID>1</DATAID><FIRST_NAME>JANE</FIRST_NAME><SECOND_NAME>DOE</SECOND_NAME><UN_LIST_TYPE>XX</UN_LIST_TYPE>
<REFERENCE_NUMBER>XXi.001</REFERENCE_NUMBER><LISTED_ON>2012-12-31</LISTED_ON><NATIONALITY><VALUE>Nowhere</VALUE></NATIONALITY>
<INDIVIDUAL_ALIAS><ALIAS_NAME>Janet Doe</ALIAS_NAME></INDIVIDUAL_ALIAS>
<INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><YEAR>1971</YEAR></INDIVIDUAL_DATE_OF_BIRTH></INDIVIDUAL>
<INDIVIDUAL><DATAID>2</DATAID><FIRST_NAME>SOLO</FIRST_NAME><REFERENCE_NUMBER>XXi.002</REFERENCE_NUMBER></INDIVIDUAL>
</INDIVIDUALS></CONSOLIDATED_LIST>"""


def test_sanctions_parsers():
    e = wl.parse_un_xml(UN_XML)
    assert len(e) == 1 and e[0]["names"] == ["JANE DOE", "Janet Doe"] and e[0]["birth_years"] == [1971]
    with pytest.raises(Exception):
        wl.parse_un_xml(b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "a"><!ENTITY b "&a;&a;">]><CONSOLIDATED_LIST>&b;</CONSOLIDATED_LIST>')
    sdn = b'10,"DOE, Jane",individual,"SDGT",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"DOB 12 Jan 1971; nationality Nowhere;"\r\n'
    assert wl.parse_ofac(sdn, b'10,1,"aka","DOE, Janet",-0- \r\n')[0]["names"] == ["DOE, Jane", "DOE, Janet"]
    eu = ("﻿fileGenerationDate;Entity_LogicalId;Entity_EU_ReferenceNumber;Entity_SubjectType_ClassificationCode;"
          "Entity_Regulation_Programme;NameAlias_WholeName;BirthDate_BirthDate;BirthDate_Year;BirthDate_Regulation_PublicationDate\n"
          "x;7;EU.1;person;ABC;Jane Doe;;;2013-01-01\nx;7;EU.1;person;ABC;;1971-02-03;;2013-01-01\n").encode()
    assert wl.parse_eu_csv(eu)[0]["birth_years"] == [1971]                         # publication dates are not birth years
    f = wl.parse_fbi_items([{"title": "JANE DOE", "poster_classification": "default", "subjects": [], "uid": "a"},
                            {"title": "JOHN ROE", "poster_classification": "missing", "subjects": [], "uid": "b"}])
    assert [x["names"][0] for x in f] == ["JANE DOE"]


def test_sanctions_match_locally_and_link_only_the_name(tmp_path):
    calls = []
    person = view("person", "target", label="Jane Doe", full_name="Jane Doe")
    out, status, msg = run_transform("un_sanctions.match", person, data_dir=tmp_path,
                                     http=mock_http({"https://scsanctions.un.org/": (200, UN_XML, {"content-type": "application/xml"})}, calls))
    assert status == "found" and "checked locally" in msg
    assert all("Jane" not in str(c.url) for c in calls)                            # the name never leaves the machine
    rec = next(iter(ents(out, "public_record")))
    assert not any(r.source == person.key or r.target == person.key for r in out.relationships)
    assert any(r.source == rec and r.type == "HAS_NAME" for r in out.relationships)


RO_XML = """<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body><CautareDosareResponse xmlns="portalquery.just.ro"><CautareDosareResult>
<Dosar><parti><DosarParte><nume>EXEMPLU ION</nume><calitateParte>Inculpat</calitateParte></DosarParte></parti>
 <sedinte><DosarSedinta><data>2021-05-01T00:00:00</data><solutieSumar>Condamnă</solutieSumar></DosarSedinta></sedinte>
 <numar>123/325/2021</numar><data>2021-01-10T00:00:00</data><institutie>JudecatoriaSECTORUL1BUCURESTI</institutie>
 <categorieCazNume>Penal</categorieCazNume><stadiuProcesualNume>Fond</stadiuProcesualNume></Dosar>
<Dosar><parti><DosarParte><nume>EXEMPLU MARIA</nume><calitateParte>Reclamant</calitateParte></DosarParte></parti>
 <numar>9/2/2020</numar><institutie>TribunalulBUCURESTI</institutie><categorieCazNume>Civil</categorieCazNume></Dosar>
</CautareDosareResult></CautareDosareResponse></soap:Body></soap:Envelope>""".encode()


def test_ro_court_parser_and_transform():
    cases, complete = courts.parse_cases(RO_XML, "Ion Exemplu")
    assert complete and [c["number"] for c in cases] == ["123/325/2021"]           # EXEMPLU MARIA is not a match
    cut = RO_XML[: RO_XML.index(b"<numar>123")]
    assert courts.parse_cases(cut, "Ion Exemplu")[1] is False                       # truncated: partial, flagged
    with pytest.raises(Exception):
        courts.parse_cases(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><Dosar>&e;</Dosar>', "x y")
    out, status, _ = run_transform("ro_courts.cases", view("person", "target", label="Ion Exemplu", full_name="Ion Exemplu"),
                                   http=mock_http({"http://portalquery.just.ro/": (200, RO_XML, {"content-type": "text/xml"})}))
    rec = props(out, next(iter(ents(out, "public_record"))))
    assert status == "found" and rec["criminal"] is True and rec["jurisdiction"] == "RO" and rec["official_url"]


def test_find_case_law_atom():
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:tna="https://caselaw.nationalarchives.gov.uk">
    <entry><title>A v B</title><link href="https://caselaw.nationalarchives.gov.uk/ewhc/ch/2026/1" rel="alternate"/>
    <published>2026-09-24T00:00:00+00:00</published><author><name>High Court</name></author>
    <id>https://caselaw.nationalarchives.gov.uk/id/d-1</id><tna:identifier type="ukncn">[2026] EWHC 1 (Ch)</tna:identifier></entry></feed>"""
    j = parse_find_case_law(atom)[0]
    assert j["court"] == "High Court" and j["citation"] == "[2026] EWHC 1 (Ch)" and j["published"] == "2026-09-24"


def test_french_registry_directors_become_separate_people():
    body = {"results": [{"siren": "123456789", "nom_complet": "ACME", "etat_administratif": "A", "siege": {"adresse": "Paris"},
                         "dirigeants": [{"nom": "DOE", "prenoms": "JANE", "annee_de_naissance": "1971", "date_de_naissance": "1971-02",
                                         "qualite": "Présidente", "type_dirigeant": "personne physique"}]}]}
    out, status, _ = run_transform("fr_entreprises.by_director", view("person", "target", label="Jane Doe", full_name="Jane Doe"),
                                   http=mock_http({"https://recherche-entreprises.api.gouv.fr/": (200, body)}))
    people = ents(out, "person")
    assert status == "found" and len(people) == 1
    pkey = next(iter(people))
    assert props(out, pkey)["birth_date"] == "1971-02"
    assert any(r.type == "MEMBER_OF" and r.source == pkey and r.target == "organization:fr-siren:123456789" for r in out.relationships)


# --------------------------------------------------------------------------- search, adapters, credentials

def test_search_results_link_names_not_people():
    routes = {"https://api.search.brave.com/": (200, {"web": {"results": [
        {"url": "https://github.com/janedoe", "title": "Jane Doe (janedoe) · GitHub", "description": "Jane Doe from Oulu"},
        {"url": "https://files.example.org/cv.pdf", "title": "CV", "description": "Jane Doe"}]}})}
    person = view("person", "target", label="Jane Doe", full_name="Jane Doe")
    out, status, _ = run_transform("brave_search.person_web", person, http=mock_http(routes), credentials={"BRAVE_API_KEY": "k"},
                                   info=InvestigationInfo(target_names=("Jane Doe",), options={"search_query_cap": 3}))
    assert status == "found" and "account:github:janedoe" in out.entities and ents(out, "document")
    assert not any(person.key in (r.source, r.target) for r in out.relationships)
    _, status, _ = run_transform("brave_search.person_web", person, http=mock_http(routes))
    assert status == "needs_credentials"


def test_paid_connector_without_key_is_needs_credentials():
    _, status, msg = run_transform("x_api.username_lookup", view("username", "jdoe"), http=mock_http({}))
    assert status == "needs_credentials" and "X_BEARER_TOKEN" in msg


def test_adapter_output_parsers():
    rep = {"GitHub": {"status": {"status": "Claimed", "url": "https://github.com/jdoe", "username": "jdoe",
                                 "ids": {"fullname": "Jane Doe"}, "tags": ["coding"]}},
           "Other": {"status": {"status": "Available", "url": "https://x"}}}
    found = parse_maigret_report(rep)
    assert len(found) == 1 and found[0]["ids"]["fullname"] == "Jane Doe"
    h = parse_harvester({"emails": ["a@acme.com"], "hosts": ["www.acme.com:1.2.3.4"], "ips": ["1.2.3.4"]})
    assert h["hosts"] == ["www.acme.com"] and h["emails"] == ["a@acme.com"]
    sf = parse_spiderfoot([{"type": "EMAILADDR", "data": "a@acme.com", "module": "sfp_x"},
                           {"type": "SOCIAL_MEDIA", "data": "Twitter: <SFURL>https://twitter.com/acme</SFURL>", "module": "sfp_y"},
                           {"type": "RAW_RIR_DATA", "data": "{}", "module": "sfp_z"}])
    assert [x[0] for x in sf] == ["email", "account"] and sf[1][1] == "https://twitter.com/acme"


def test_option_injection_is_impossible_and_missing_tools_are_reported(monkeypatch):
    for bad in ("--json", "-d", "a b", "x;rm"):
        with pytest.raises(ValueError):
            view("username", bad)                       # normalisation rejects anything that could become a CLI option
    monkeypatch.setenv("MAIGRET_PATH", "/nonexistent/maigret")   # hermetic even where Maigret is installed
    _, status, msg = run_transform("maigret.search", view("username", "jdoe"), http=mock_http({}))
    assert status == "error" and "not installed" in msg


# --------------------------------------------------------------------------- the SDK's worked example

CF_USER = {"status": "OK", "result": [{"handle": "Jdoe_92", "firstName": "Jane", "lastName": "Doe", "country": "Finland",
                                       "city": "Oulu", "organization": "Example University", "rating": 1650, "rank": "expert",
                                       "registrationTimeSeconds": 1420070400,
                                       "titlePhoto": "https://userpic.codeforces.org/1/title/x.jpg"}]}


def test_codeforces_worked_example():
    calls = []
    out, status, _ = run_transform("codeforces.username_lookup", view("username", "jdoe_92"),
                                   http=mock_http({"https://codeforces.com/api/user.info": (200, CF_USER)}, calls))
    assert status == "found" and calls[0].url.params["handles"] == "jdoe_92"
    acc = props(out, "account:codeforces:jdoe_92")
    assert acc["display_name"] == "Jane Doe" and acc["created_at"] == "2015-01-01" and acc["employer"] == "Example University"
    assert {"name:doe jane", "location:oulu finland", "organization:name:example university"} <= set(out.entities)
    assert all(o.source in out.sources for o in out.observations)
    out, status, _ = run_transform("codeforces.username_lookup", view("username", "nobody_zz9"), http=mock_http({
        "https://codeforces.com/api/user.info": (400, {"status": "FAILED", "comment": "handles: User with handle nobody_zz9 not found"})}))
    assert status == "not_found" and out.checks[0].status == "not_found"
    from sextant.registry import platforms as PLAT
    p, handle = PLAT.parse_profile_url("https://codeforces.com/profile/Jdoe_92")
    assert p.id == "codeforces" and handle.lower() == "jdoe_92"


# --------------------------------------------------------------------------- every transform: robustness & secrecy

from sextant.registry import connectors as REG  # noqa: E402

SAMPLE = {"username": ("username", "jdoe92", {}), "email": ("email", "jane@example.org", {}),
          "phone": ("phone", "+358401234567", {}), "domain": ("domain", "example.org", {}),
          "ip": ("ip", "93.184.216.34", {}), "name": ("name", "Jane Doe", {}),
          "person": ("person", "target", {"label": "Jane Doe", "full_name": "Jane Doe"}),
          "organization": ("organization", "name:acme ltd", {"label": "Acme Ltd", "name": "Acme Ltd"}),
          "account": ("account", "x:jdoe92", {"platform": "x", "username": "jdoe92"}),
          "location": ("location", "Oulu, Finland", {}), "website": ("website", "https://example.org/a", {})}
SECRET = "SECRETKEY123"
RUNNABLE = sorted(t.name for t in REG.TRANSFORMS.values()
                  if REG.REGISTRY[t.connector].manifest.access != "tool" and any(x in SAMPLE for x in t.input_types))


@pytest.mark.parametrize("name", RUNNABLE)
def test_transform_survives_empty_answer_and_never_leaks_credentials(name):
    t = REG.TRANSFORMS[name]
    m = REG.REGISTRY[t.connector].manifest
    typ, value, kw = SAMPLE[next(x for x in t.input_types if x in SAMPLE)]
    entity = view(typ, value, **kw)
    creds = {c.key: ("https://searx.example.org" if c.key.endswith("_URL") else SECRET) for c in m.credentials}
    out, status, msg = run_transform(name, entity, http=mock_http({"https://": (200, {})}), credentials=creds,
                                     info=InvestigationInfo(target_names=("Jane Doe",)))
    assert status in ("found", "empty", "not_found", "error", "skipped", "blocked", "needs_credentials"), (status, msg)
    blob = " ".join(f"{s.url} {s.request} {s.title}" for s in out.sources.values()) + msg
    assert SECRET not in blob
    assert all(o.source in out.sources for o in out.observations)
