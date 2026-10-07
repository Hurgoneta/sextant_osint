# Connector SDK — how to add connector #1000

A **connector** integrates one data source: its manifest (what it is, what access costs, what it sends where),
credentials and transforms. A **transform** is one operation: it takes *one entity* of an accepted type and
returns new entities, facts and relationships, each with a source. The engine does everything else: scheduling,
rate limiting, budgets, caching, deduplication, persistence, scoring and coverage.

```
backend/sextant/connectors/<category>/<your_source>.py     ← code (auto-discovered, no registration list to edit)
backend/sextant/data/platforms/*.toml                      ← if it is a social/online platform
backend/sextant/data/jurisdictions/*.toml                  ← if it is an official register of a country
tests/test_connectors.py                                   ← offline test with a recorded response
```

## 1. The worked example: Codeforces (`connectors/developer_platforms/codeforces.py`)

This is a real, shipped connector. It uses only the public SDK so every step is visible.

```python
"""Codeforces — competitive-programming profiles through the official public API.

This connector is the worked example in docs/CONNECTOR_SDK.md. It deliberately uses only the public SDK
(no shared helpers) so every step is visible; keep the two in sync.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ...sdk import Connector, Disclosure, Manifest, NotFound, RateLimit, SourceUnavailable, register, transform

API = "https://codeforces.com/api/user.info"
HANDLE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")


@register
class Codeforces(Connector):
    manifest = Manifest(
        name="codeforces", title="Codeforces", category="developer_platforms",
        description="Competitive-programming profiles: published name, country, city, organization, rating and "
                    "registration date.",
        access="free", cost="Free", rate_limit=RateLimit(0.5, 1, "one request per 2 seconds (API terms)"),
        website="https://codeforces.com", documentation="https://codeforces.com/apiHelp", reliability="high",
        license="Codeforces API terms", official_api=True,
        capabilities=("account existence", "first/last name (if the user published it)", "country and city",
                      "organization", "registration date", "rating"),
        disclosure=Disclosure(("codeforces.com",), "the username"),
    )

    @transform(inputs=("username",), outputs=("account", "name", "location", "organization"),
               title="Codeforces: account by username", priority=0.4,
               accepts=lambda self, e, info: bool(HANDLE.match(e.display)))
    async def username_lookup(self, entity, ctx):
        """Looks the handle up with user.info; a missing handle is an honest "not found"."""
        resp = await ctx.get(API, params={"handles": entity.display})
        data = ctx.json(resp) or {}
        if resp.status == 400 and "not found" in str(data.get("comment", "")):
            ctx.out.check("codeforces", "not_found", platform_title="Codeforces")    # shown in the platform sweep
            raise NotFound(f"No Codeforces account “{entity.display}”")
        ctx.expect(resp)                                   # 403 → blocked, 429 → rate limited, 5xx → unavailable
        users = data.get("result") or []
        if data.get("status") != "OK" or not users:
            raise SourceUnavailable(f"Codeforces answered: {str(data.get('comment') or 'no result')[:120]}")
        u = users[0]
        handle = u["handle"]

        # 1. provenance: every fact below cites this source
        src = ctx.out.source(f"https://codeforces.com/profile/{handle}", title="Codeforces API (user.info)",
                             request=f"handle “{entity.display}”")

        # 2. the account and its facts
        name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x).strip()
        place = ", ".join(x for x in (u.get("city"), u.get("country")) if x)
        created = u.get("registrationTimeSeconds")
        acc = ctx.out.entity("account", f"codeforces:{handle}", label=f"Codeforces: {handle}", source=src, props={
            "platform": "codeforces", "platform_title": "Codeforces", "category": "developer_platforms",
            "username": handle, "display_name": name or None, "location": place or None,
            "employer": u.get("organization") or None, "profile_url": f"https://codeforces.com/profile/{handle}",
            "avatar_url": u.get("titlePhoto"), "rating": u.get("rating"), "rank": u.get("rank"),
            "created_at": datetime.fromtimestamp(created, timezone.utc).date().isoformat() if created else None,
            "found_by": "username"}, evidence="Profile returned by the official API")
        ctx.out.link(acc, entity.key, "USES", source=src, evidence="Handle of the account")
        ctx.out.check("codeforces", "found", url=f"https://codeforces.com/profile/{handle}", account=acc,
                      platform_title="Codeforces")

        # 3. new selectors / objects the engine can pivot on — linked to the ACCOUNT, never to a person
        if len(name.split()) >= 2:
            ne = ctx.out.entity("name", name, source=src)
            ctx.out.link(acc, ne, "HAS_NAME", source=src, evidence="Name published on the profile")
        if place:
            le = ctx.out.entity("location", place, source=src, props={"text": place, "city": u.get("city"),
                                                                      "country": u.get("country")})
            ctx.out.link(acc, le, "LOCATED_IN", source=src, evidence="City/country on the profile")
        if u.get("organization"):
            org = u["organization"]
            oe = ctx.out.entity("organization", f"name:{org.casefold()}", label=org, source=src, props={"name": org})
            ctx.out.link(acc, oe, "WORKS_FOR", source=src, evidence="Organization stated on the profile")
```

What happens around it:

* `@register` validates the manifest at import time (`Manifest.validate()`; the test suite fails on any problem).
* `ctx.get()` goes through `SafeHttpClient`: SSRF guard, the manifest's rate limit, the investigation's request
  budget, an encrypted per-investigation cache, retries with back-off, and size caps.
* The engine maps exceptions to run statuses (§4), merges the result, scores the new account against the
  target, and plans the next transforms on the name, location and organization it produced.
* The Data Sources screen, the plan shown before a run, the transform menu and the coverage report all pick the
  connector up automatically.

## 2. The manifest

| Field | Meaning |
|---|---|
| `name`, `title`, `category`, `description` | Identity. `category` must be a key of `sdk.manifest.CATEGORIES` (it drives the catalog grouping and the live progress groups). |
| `access` | `local` · `free` · `free_key` (free registration) · `restricted` (platform approval) · `metered` (pay per call) · `paid` · `enterprise` · `tool` (local executable) · `manual` (links only). |
| `credentials` | `Credential(key, label, help, required=True, obtain_url=…)`. Keys are read from the encrypted store or the environment. `required=False` means "works without, better with". |
| `cost`, `cost_per_call_usd`, `max_calls_per_investigation` | Shown to the user. Paid and metered sources need `auto_run=True` explicitly to run in auto mode. |
| `rate_limit` | `RateLimit(per_second, burst, note)`: a token bucket shared by every transform of the connector. |
| `jurisdictions`, `supported_countries`, `legal`, `public_record` | Legal home, data coverage, terms and access requirements that matter. |
| `official_api`, `access_method`, `website`, `documentation`, `terms_url`, `license`, `reliability`, `coverage`, `capabilities` | Catalog information. `capabilities` renders as "This data source can provide: …". |
| `disclosure` | `Disclosure(destinations, sends, mode)`: **what leaves the machine, and where.** Shown before every run. `mode`: plaintext · hashed · public · download (dataset fetched, matching done locally) · tool · none. |
| `default_enabled`, `auto_run`, `opt_in_reason` | Off-by-default sources explain why (e.g. "sends names to a third party"). |
| `max_concurrency`, `timeout`, `status` (`stable`/`beta`/`sunsetting`/`retired`/`excluded`), `status_note` | Behaviour and lifecycle. |

## 3. Transforms

```python
@transform(inputs=("username",), outputs=("account",), title="…", priority=0.5,   # 0..1 usefulness (ranking)
           auto=True,               # may auto-investigation run it? (False: manual menu only)
           seeds_only=False,        # only on what the investigator typed in
           min_value=0.0,           # automatic runs need this investigative value on the input
           estimated_requests=1,    # budget planning
           accepts=lambda self, e, info: …,   # cheap applicability check (country, format, …)
           sends="the username {value}")      # override the disclosure text for this transform
async def lookup(self, entity: EntityView, ctx: TransformContext) -> None: …
```

A standalone class works the same way (`class X(Transform)` with `name`, `connector`, `input_types`,
`output_types`, `async def transform(self, entity, ctx)`, registered with `register_transform`).

**`EntityView`** (read-only): `key`, `type`, `value` (normalized), `label`, `display`, `status`, `is_seed`,
`get(prop)`, `values(prop)`. Local-only properties such as the birth year are **not** present.
**`InvestigationInfo`**: target names, countries, default region, `option(key)` (e.g. `search_query_cap`).

**`TransformContext`**

| Method | |
|---|---|
| `await ctx.get(url, params=, headers=, cache_ttl=)` / `ctx.post(...)` / `ctx.download(url, dest, max_bytes=)` | Guarded HTTP. |
| `ctx.expect(resp, ok=(200,), not_found=(404,))` | Maps 404 → NotFound, 401 → NeedsCredentials, 403 → Blocked, 429 → RateLimited, other → SourceUnavailable. |
| `ctx.json(resp)` | Parses JSON; truncated or invalid → SourceUnavailable. |
| `ctx.require("KEY", …)` / `ctx.credential("KEY")` | Credentials (`require` raises NeedsCredentials naming the key). |
| `ctx.setting(key)` / `ctx.flag(key)` | Application settings (e.g. `respect_robots_txt`). |
| `ctx.take("queries", cap)` | Per-investigation quota (False when used up). |
| `ctx.data_dir` | Where a dataset connector may cache a downloaded list. |

**`ctx.out` (`TransformResult`)**

| Method | |
|---|---|
| `src = out.source(url, title=, kind="api", request=)` | Provenance for the facts that follow. Required. |
| `key = out.entity(type, value, source=src, label=, props={…}, evidence=)` | Validated and normalized; returns the key, or `None` (and records a rejection) for invalid values. |
| `out.prop(key, name, value, source=src)` / `out.props(key, {…}, source=src)` | Extra facts. Strings, lists and data URLs are bounded. |
| `out.link(a, b, "REL", source=src, evidence=, confidence=, props=)` | Relationship with evidence. Unknown relationship types raise. |
| `out.check(platform, status, url=, account=)` | Per-platform outcome for sweeps (found / not_found / blocked / skipped / unknown). |
| `out.set_status(status, message)` | Override the final status (rarely needed). |

## 4. Statuses: be honest

| Raise | Status | Use when |
|---|---|---|
| `NotFound` | `not_found` | The source answered authoritatively that nothing exists. |
| `NeedsCredentials` | `needs_credentials` | A key is missing or was rejected. |
| `Blocked` | `blocked` | 403, bot wall, login wall. **Never** retry around it. |
| `RateLimited` | `rate_limited` | 429 or a documented quota. |
| `SourceUnavailable` | `error` | Timeouts, 5xx, malformed answers. |
| `BudgetExceeded` | `budget_exhausted` | Raised by the engine. |
| `Skipped("why")` | `skipped` | Policy (robots.txt, opt-in not given, nothing to do). |
| (no exception) | `found` / `empty` | `found` if anything was produced. |

"Could not check" must never look like "not found", and that difference is what the coverage report is built on.

## 5. Rules for every connector (reviewed in code review, many enforced by tests)

1. **No bypassing** of authentication, payment, CAPTCHAs, bot protection or rate limits. No spoofed browser
   user agents (the shared client sends `Sextant/<version>`), no cookies from a logged-in session, and no use
   of password-reset or registration flows to infer accounts.
2. **Declare the disclosure truthfully.** If a list can be downloaded and matched locally, do that
   (`mode="download"`). Names of people are never sent to a source that does not need them.
3. **Names are not identities.** A record, officer or search hit about a *name* creates a `public_record` or a
   `person:<registry>:<id>` linked by `HAS_NAME` to a `name` entity, never to the target person. Only identifiers
   (email, phone, registration ID, verified link) may link to what led to the lookup.
4. **Bounded work.** Respect `ctx.take()` quotas, paginate with caps, keep `estimated_requests` realistic.
5. **Parse defensively.** Use `defusedxml` for XML. Treat every field as untrusted text; don't build URLs from
   response fields without `canonical_url()`.
6. **Credentials stay in headers** (never in logs or source URLs). The client strips them on cross-host redirects.
   If an API only accepts a key in the query string, set `request=` on the source to a redacted description.

## 6. Testing (offline, with recorded responses)

```python
from sextant.sdk.testing import mock_http, run_transform, view

def test_codeforces_worked_example():
    http = mock_http({"https://codeforces.com/api/user.info": (200, {"status": "OK", "result": [{…}]})})
    out, status, message = run_transform("codeforces.username_lookup", view("username", "jdoe_92"), http=http)
    assert status == "found" and "account:codeforces:jdoe_92" in out.entities
    assert all(o.source in out.sources for o in out.observations)        # every fact has a source
```

`mock_http(routes, calls)` answers by longest URL prefix (dicts become JSON) and records requests in `calls`,
so you can assert what was sent, e.g. that a name never left the machine. `run_transform` runs the transform
exactly as the engine does and returns `(result, status, message)`. Add a `@pytest.mark.live` test in
`tests/test_live.py` using a public test identifier to detect upstream API changes.

## 7. Shortcuts for common shapes

* **Platform profile APIs:** `make_platform_connector(PlatformSpec(name, platform, title, category, host,
  endpoint, parse, website, …))` in `connectors/_platform_api.py` creates `username_lookup` and `account_profile`
  transforms. Your `parse(resp, username)` returns `("found" | "not_found" | "unknown", profile_dict, detail)`;
  profile keys follow `connectors/_common.PROFILE_FIELDS` (display_name, bio, location, website, company,
  created_at, avatar_url, links, emails…). `add_account()` then also creates the email, link, location,
  organization, name and profile-photo entities.
* **Company registers:** `connectors/companies/_registry.py` provides `add_org()`, `add_officer()` (officers become
  record-scoped persons), and `org_accepts(country)` / `seed_person(country)` applicability rules.
* **Sanctions and watch lists:** `connectors/sanctions/lists.py: make_list_connector(...)` downloads the official
  list into `ctx.data_dir`, refreshes it daily, and matches names locally.
* **Search engines:** subclass `SearchConnector` in `connectors/search/engines.py` and implement `fetch(query, ctx)`.
  Query generation, quotas, categorization and profile-URL recognition are shared.
* **External tools:** see `connectors/adapters/tools.py`: `run_tool(argv, timeout, workdir)` (no shell, minimal
  environment, process-group kill), plus a parser for the tool's JSON output. Mark `access="tool"`, implement
  `installed()`, and state in the manifest that the tool makes its own requests.
* **Known source without automation:** add a `[[source]]` to `data/catalog/catalog_only.toml` (enterprise
  services, sources excluded by policy with the reason). A connector with the same `name` replaces the entry.
* **An official register of a country:** add a `[[country.source]]` to `data/jurisdictions/*.toml` (`kind`, `url`,
  `access`, `name_search`, `person_data`, `api`, `connector` if automated), then run
  `python -m sextant.tools.verify_catalog --only jur:<id>` from `backend/`. Several `[[country]]` tables for one
  country are merged, so additions can live in their own file (see `supplement.toml`).
* **A country connector:** set `supported_countries=("XX",)` and `jurisdictions=("XX",)` in the manifest and use
  `org_accepts("XX")` / `seed_person("XX")`. That alone makes the Global Coverage screen count it for the country,
  pre-selects it when an investigation points to the country (even if it is off by default), and keeps it out
  of other countries' SEARCH EVERYTHING runs. See `connectors/companies/open_registers.py` (Latvia, Slovakia,
  Israel, Taiwan, Colombia) for open-data APIs (CKAN datastore, Socrata, OData).
* **A platform with a public user API — no code:** in its `data/platforms/*.toml` entry set `check = "probe"` and
  declare the endpoint; the `profile_probes` connector runs every declared probe:

  ```toml
  check = "probe"
  probe = { url = "https://api.example.org/users/{username}", require = "id",
            fields = { display_name = "name", bio = "about" }, docs = "https://api.example.org/docs" }
  ```

  Options: `method = "POST"` + `body` (JSON with `{username}`), `missing_status = [404, 204]`,
  `list_path` + `match` for search endpoints, `accept_empty_list`, `username_case = "upper" | "lower"`,
  `format = "text"` (status code only), `accept` (Accept header). Only documented public endpoints with an
  unambiguous "not found" qualify — never internal APIs of the site's own web app.
* **Health probe:** add `[probe.<connector>]` with a public test input to `data/health/probes.toml` and run
  `python -m sextant.tools.health_check --only <connector>`; the result becomes the connector's "last verified".

## 8. Checklist

- [ ] Manifest complete: access, cost, credentials with `obtain_url`, rate limit, licence, disclosure, capabilities
- [ ] Every entity, property and link cites `out.source(...)`
- [ ] Honest statuses: not found ≠ blocked ≠ unavailable
- [ ] Names never linked to the target person; local-only data never sent
- [ ] Offline test with a recorded response (+ a live test if the API is public)
- [ ] `pytest` and `python -m pyflakes backend/sextant tests` pass
- [ ] Platform or jurisdiction entry added if applicable, with a `check`/`connector` pointing at your connector
