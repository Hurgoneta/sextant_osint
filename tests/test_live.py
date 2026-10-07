"""Opt-in checks against real public sources (pytest -m live). They detect upstream API changes; they send
only well-known public test identifiers (GitHub's mascot account, IANA's example domain, Google's DNS IP)."""

import pytest

from sextant.sdk.http import SafeHttpClient
from sextant.sdk.testing import run_transform, view

pytestmark = pytest.mark.live


def live(name, entity, **kw):
    return run_transform(name, entity, http=SafeHttpClient(), **kw)


def test_github_profile():
    out, status, _ = live("github.username_lookup", view("username", "octocat"))
    assert status == "found" and "account:github:octocat" in out.entities


def test_rdap_and_dns():
    out, status, _ = live("rdap.domain", view("domain", "example.com"))
    assert status in ("found", "empty")
    out, status, _ = live("dns.records", view("domain", "example.com"))
    assert status == "found" and any(k.startswith("ip:") for k in out.entities)


def test_ip_context():
    _, status, _ = live("ripestat.network", view("ip", "8.8.8.8"))
    assert status == "found"


def test_wikidata_organization():
    out, status, _ = live("wikidata.organization", view("organization", "name:wikimedia foundation", label="Wikimedia Foundation",
                                                       name="Wikimedia Foundation"))
    assert status in ("found", "empty")


def test_gleif_organization():
    out, status, _ = live("gleif.search", view("organization", "name:deutsche bank", label="Deutsche Bank AG",
                                                  name="Deutsche Bank AG"))
    assert status == "found" and any(k.startswith("organization:lei:") for k in out.entities)
    _, status, _ = live("gleif.search", view("organization", "name:wikimedia foundation", label="Wikimedia Foundation",
                                            name="Wikimedia Foundation"))
    assert status == "not_found"                  # US non-profit without an LEI: an honest "not found"


def test_codeforces_sdk_example():
    # an invented handle: the live check is that the API answers an honest "not found" (parsing is tested offline)
    _, status, _ = live("codeforces.username_lookup", view("username", "nonexistent_user_zz9q"))
    assert status == "not_found"
