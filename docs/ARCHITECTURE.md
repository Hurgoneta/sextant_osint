# Sextant architecture

Sextant is a single local process: a Python engine (FastAPI on `127.0.0.1`) that serves a React UI and talks
only to the sources the investigator enables. Nothing runs on a server operated by this project.

```
┌────────────── browser (React + Cytoscape, served by the engine) ───────────────┐
│ Home · Live · Graph · Accounts · Records · Entities · Timeline · Coverage ·    │
│ Tasks · Report  |  Global coverage · Data Sources · Coverage matrix ·          │
│ Platforms · Jurisdictions                                                       │
└───────────────▲──────────────── X-Sextant-Token, same origin ──────────────────┘
                │ REST + long-poll events
┌───────────────┴──────────────── engine (Python 3.11+) ─────────────────────────┐
│ api/          FastAPI routes, guard middleware (Host, token, Origin, CSP)      │
│ engine/       input parser · orchestrator (priority queue) · limits · events · │
│               coverage tracker · merge of transform results                    │
│ resolution/   target profile · log-likelihood scorer · value propagation       │
│ model.py      Graph: entities, observations, relationships, sources, checks   │
│ registry/     connectors · countries (195) · jurisdictions · discovered ·      │
│               platforms (curated + datasets) · health · coverage_model         │
│ sdk/          Manifest · Transform · TransformContext · TransformResult ·      │
│               SafeHttpClient (SSRF guard) · errors · testing helpers          │
│ connectors/   implemented connectors in 30+ categories + catalog entries       │
│ storage/      SQLite (WAL) · Fernet column encryption · HMAC blind indexes      │
│ reporting/    JSON / CSV / HTML report with [S#] citations                      │
└───────────────┬────────────────────────────────────────────────────────────────┘
                │ only what each manifest's Disclosure declares
          public APIs · official registers · datasets · local tools (adapters)
```

## 1. Data flow of one investigation

1. **Input understanding** (`engine/input.py`). Free text or structured fields become a `ParsedInput`: the
   target (person, organization or none), aliases, typed seeds (email, phone, username, domain, URL → account,
   IP, location, organization) and a local-only birth year. Ambiguous input produces an error or note instead of
   a guess. For example, a national phone number without a default region is rejected.
2. **Seeding** (`Engine.create`). The target becomes `person:target` / `organization:target`, never keyed by
   name. Seeds are linked to it (USES / OWNS / LOCATED_IN) with the user's input as the source.
3. **Planning** (`_plan`, `_enqueue`). For each expandable entity below the depth limit, every transform
   whose `input_types` match is considered. A transform is queued only if all of these hold:
   * its connector is *available* (installed, enabled, credentials present; see §4);
   * it may run automatically (manifest `auto_run`, per-source preference, investigation options);
   * the entity is a seed or its **investigative value** reaches the expansion threshold of the depth preset;
   * `accepts()` agrees (e.g. national registers only for organizations in their country);
   * the connector's per-investigation cap is not reached.

   Every (transform, entity) pair is planned at most once.
4. **Scheduling.** One heap per connector. Priority =
   `2.0·[user-triggered] + 0.55·value(entity) + 0.3·priority(transform) + 0.15·(1 − depth/7)`.
   Workers take the best task of the connector that is not rate-limited or saturated
   (`max_concurrency` overall, per-connector concurrency, token-bucket rate limits).
5. **Execution** (`_execute`). The transform receives a read-only `EntityView` with local-only properties
   removed, plus a `TransformContext`. HTTP goes through the shared `SafeHttpClient`, which enforces the
   request budget. Exceptions map to honest run statuses: `found / empty / not_found / needs_credentials /
   blocked / rate_limited / budget_exhausted / skipped / error / timeout / cancelled` (an unreachable or
   misbehaving source is `error`, with the reason in the message, and never counts as "not found").
6. **Merge** (`engine/merge.py`). The `TransformResult` (sources, entities, observations, relationships, checks)
   is validated and merged. Entities are deduplicated by normalized value. New entities get `depth = input + 1`.
7. **Resolution** (`resolution/scorer.py`), incremental and debounced. It rebuilds the target profile from
   anchors (seeds, confirmed accounts, and accounts ≥ 85% with independent strong evidence), scores every
   candidate, and propagates identity confidence and investigative value along edges.
8. **Replanning.** Entities whose value rose past the threshold since they were last planned are planned
   again. The loop ends when the queue is empty, a limit is hit (`limit_reached`; *Continue* resumes with
   raised limits), or the investigator pauses or stops it.
9. **Persistence** (`storage/db.py`). Only *dirty* entities, observations, relationships, runs and tasks are
   flushed, every ~2 s and at the end. A restart reloads the graph and the queue exactly.

### Modes

* **Auto (INVESTIGATE):** steps 3–8 run recursively within the depth preset.
* **Manual:** nothing runs until the investigator picks transforms from an entity's menu (or RUN ALL).
  Results are merged and scored, but not expanded further.
* **Auto-investigate from here:** a manual action that switches on automatic expansion, bounded to *N* more
  levels below the chosen entity.

### Depth presets (`engine/limits.py`)

| Preset | Max depth | Expansion threshold | Entities | Requests | Time |
|---|---|---|---|---|---|
| 1 Direct results | 1 | (none expanded) | 600 | 2,500 | 8 min |
| 2 Direct + related | 2 | 0.50 | 1,500 | 6,000 | 15 min |
| 3 Deep correlation | 3 | 0.40 | 3,500 | 12,000 | 30 min |
| 4 Aggressive | 5 | 0.25 | 8,000 | 25,000 | 60 min |
| Custom | any value up to the hard ceiling (depth 8, 20k entities, 60k requests, 4 h) | | | | |

## 2. Identity confidence vs investigative value

These are two separate numbers:

* **Identity confidence**: how likely an entity belongs to the target. It comes from the log-likelihood
  scorer, with signed, human-readable reasons. Tier: *high* when ≥ 0.8, or ≥ 0.5 with a strong feature (shared
  email, phone, verified link…); *medium* when ≥ 0.45 or backed by a medium feature; otherwise *low*.
* **Investigative value**: how worth expanding an entity is. Resolvable entities use their confidence, with
  tier floors (high → 0.8, medium → 0.55) so medium leads still get followed. Identifiers inherit
  `neighbour value × edge confidence × EXPAND_FACTOR[type]` (email 1.0, username 0.95, domain 0.95, name 0.8,
  IP 0.6, location 0.3, ASN 0.25…). Generic platform domains (github.com, gmail.com…) are never expanded.

This separation lets the engine follow a promising website without claiming it belongs to the target.

## 3. Evidence model (summary; details in ONTOLOGY.md)

```
Source ──< Observation >── Entity ──< Relationship (evidence[]) >── Entity
  connector, url,          prop, value,      type, value (normalized), props = aggregate(observations),
  retrieved_at, request,   confidence,       confidence, tier, value_score, match_label, reasons[], status
  licence, reliability     evidence text
```

* Every entity has a sighting observation (`prop "@"`), so "where was this seen" is always answerable.
* Properties are never stored directly. They are aggregated from observations, so a merge is reversible
  (`separate` replays the snapshot) and each value lists the sources that support it.
* Investigator decisions (confirm / separate / ignore / reset / merge / unmerge / reject link) are written
  to `user_decisions` with a timestamp and note, and appear in the report's audit log.

## 4. Connector availability (`registry/connectors.py: connector_status`)

`excluded` (policy) → `manual` (links only) → `catalog_only` → `retired` → `not_installed` (adapters whose tool
is missing) → `needs_subscription` / `needs_credentials` → `disabled` (by the user or off by default) →
`available`. The same status drives the Data Sources screen, the transform menu, the plan shown before an
investigation, and the coverage report, so all four always agree.

## 5. Coverage

`engine/coverage.py` records, per connector and investigation: runs by status, plus why it was skipped
(needs credentials, needs subscription, disabled, not installed, manual only, excluded by policy, cap reached,
not applicable to any entity). The report and Coverage tab group these by category (social, developer,
infrastructure, legal & public records…) and always carry the disclaimer that a missing result is not proof
of absence.

`global_coverage()` adds the investigation-level block: countries evaluated (195), the countries the
investigation points to with their country connectors and what those did, platforms registered / searched /
unavailable / skipped / not searched (from the per-platform checks), public databases queried / failed /
unavailable, and commercial providers configured / not configured. Every number is counted, never estimated,
and the block states that Sextant searched *all currently configured and applicable sources available to this
installation*, not the Internet.

## 5a. Global registries (countries, sources, platforms)

```
UN M49 + Wikidata ──► data/countries/countries.json ──► registry/countries.py   195 sovereign states + territories
curated TOML ───────► data/jurisdictions/*.toml ──────► registry/jurisdictions.py  official sources per country,
                                                                                   regional portals, blocs (EU, CoE…)
GLEIF RA list, OpenSanctions index, Wikidata,  ──► data/discovered/*.json ──► registry/discovered.py
IANA root zone + RDAP bootstrap, RIR stats, GDELT                                  potential sources, with provenance
WhatsMyName, Maigret, Sherlock site lists ──► data/platforms/imported/*.json ┐
curated TOML (capabilities) ────────────────► data/platforms/*.toml ─────────┴► registry/platforms.py
probes, real runs ──► data/health/_health.json + table source_health ──────────► registry/health.py
everything above + configuration ─────────────────────────────────────────────► registry/coverage_model.py
```

* **Countries.** `tools/import_datasets countries` builds the list from the UN M49 table and Wikidata, with curated
  overrides (short names, investigation regions, aliases, FIPS codes, territories). A loader rule forbids
  continent-wide placeholders: a source shared by several countries is a `[[regional]]` entry attached to each
  listed country, and supranational sources reach member states through `[[bloc]]` memberships.
* **Sources per country** come in four layers, kept apart everywhere they are shown: country connectors
  (automation, via `supported_countries`), curated official sources (links), regional portals, and *potential*
  sources discovered in maintained datasets (`tools/import_datasets`). `tools/promote_discovered` proposes curated
  entries from the discovered layer (core institutions only, plausibility rules, live link check) and marks their
  search capabilities as not yet reviewed. Investigators add their own sources, links, categories and platforms in
  the UI (the catalog's user layer, §5d).
* **Coverage level** of a country counts only country-specific sources: *connectors* → *links* → *discovered* →
  *none* (shown as "No connector currently available — No verified public/authorized API discovered").
* **Platforms**: curated entries carry capability metadata (public profile and search, official API and its
  requirements, private accounts, service status, country/region, how Sextant checks it); the community datasets
  add thousands of sites. `search_status()` turns that and the configuration into what this installation can
  actually do — *registered is not searchable*.
* **Adapters** (Maigret, Sherlock) are on by default when installed. Their own requests bypass `SafeHttpClient`, so
  `connectors/_policy.py` (shared with the WhatsMyName engine) filters the tool's site database before every run.
  Maigret uses its current database through its own update channel (version check, SHA-256, daily cache); the
  filtered copy is test-loaded before the scan, and results are discarded if Maigret fell back to its unfiltered
  bundled list. Platform counts follow the database actually used (Maigret: the imported snapshot of its current
  database, or the bundled one if so configured; Sherlock: the installed one). Maigret sites beyond the per-run
  top N count as "checked with SEARCH EVERYTHING".
* **Health**: probes (`tools/health_check`, public test inputs) and the outcomes of real runs give each connector
  an operational status (operational, degraded, authentication or subscription required, temporarily unavailable,
  deprecated, broken, not publicly searchable, country restricted, unknown) with last-verified and
  last-successful-query dates. Only statuses and timestamps are stored, never what was searched.

## 5b. SEARCH EVERYTHING

The start screen builds a plan before anything runs: the countries inferred from the input (or picked by the
investigator), their country connectors and official links, every connector that applies with its status
(ready, key missing, subscription missing, unavailable), the opt-in sources the run will switch on, and the paid
sources that are configured. Starting it sets `search_everything`: all categories and platforms, opt-in sources
that are global switch on (country-specific ones only for the investigation's countries), Maigret runs on all
sites, and the depth is at least 3, so the queue keeps expanding as new entities appear, within the limits.

## 5c. Back-off from providers that refuse

A source that answers 403/bot challenge (`blocked`) or keeps rate-limiting after the client's own Retry-After-aware
retries (`rate_limited`) three runs in a row gets no further automatic requests in that investigation: its queued
tasks are skipped with the reason, and the coverage view says why (`_track_refusals` in the orchestrator). Nothing is
retried around a refusal. The investigator can still run one of its transforms by hand; a success lifts the stop.

## 5d. The catalog: built-in layer and user layer

```
system (read-only, shipped)                      user (SQLite, catalog_* tables)
connectors + catalog TOML ──┐                    catalog_sources ──< catalog_source_jurisdictions (code)
data/jurisdictions/*.toml ──┤                         │         └─< catalog_source_platforms (platform id)
data/platforms ─────────────┤                         └── category → built-in or catalog_categories
category vocabularies ──────┘                    catalog_platforms · catalog_audit_ignores
             └──────────────── registry/catalog.py merges both ────────────────┘
   Data Sources · Coverage matrix · Jurisdictions · country pages · Global coverage · Platforms · audit · counts
```

* **One record per source.** A source the investigator adds (from Data Sources, a jurisdiction, a country page or an
  audit suggestion) is one row; its jurisdictions and platforms are relationships. Every screen computes from the same
  two layers, and every item says `origin: system | user`. Nothing is copied per screen, so counts agree.
* **Definition ≠ execution.** A user source is metadata: name, address, category, type (reference link, manual
  source, official/public API without connector, dataset, local), access, jurisdictions, platforms, inputs, notes. The
  API refuses any other field. It never runs. It may reference an existing connector; then that connector's status
  applies everywhere. Its health is "Unknown (not yet verified)" until the investigator runs a link check (one GET
  through the guarded client).
* **System data is never stored as user data.** Built-in records come from the installed version, so new defaults
  appear after an update and the user layer cannot overwrite them; the built-in records cannot be edited or deleted
  (connectors can still be switched off, as before).
* **Duplicates** (`find_duplicates`): same normalised address, same website, same name in an overlapping jurisdiction,
  same connector or platform. They are checked against connectors, official sources, platforms and the user layer.
  Likely duplicates need "Continue anyway". On edit, only duplicates the edit introduces count.
* **Jurisdiction audit** (`registry/catalog_audit.py`): read-only. It rates each group (automated by a country
  connector, automatable, global connectors only, links only, unreviewed dataset entries only, gap) and lists
  duplicates, improvements and suggestions. Suggestions come only from shipped datasets or the last live connector
  search, never invented, minus everything already in the catalog. Each has provenance, a confidence with its reasons,
  and "Not verified". "Add…" opens the normal form, prefilled.
* **Catalog reset** (`POST /api/catalog/reset`, Settings or Data Sources, after a summary and typing RESET) deletes the
  user layer, the on/off overrides of built-in sources, ignored suggestions and saved connector searches in one
  transaction. Investigations, API keys and settings stay. The **Hard reset** (`GET/POST /api/reset`, Settings) offers
  four separate scopes — `catalog` (the above + saved source-discovery results), `history` (every investigation with its
  graph, assistant memory, notes, caches, raw evidence and usage records), `ai_settings` (provider profiles, failover
  order, AI options, price overrides and provider-listed prices) and `credentials` (stored API keys) — and touches only
  the scopes ticked. Caches are keyed on a fingerprint of the user layer, so no
  removed record reappears.
* **Your platforms in recognition.** A user platform with a profile-URL template is recognised like a built-in one,
  through a context-local list (`platforms.user_platforms_in_scope`) that the API sets while parsing input and the
  engine sets around each transform run and identity resolution. So a pasted profile link becomes an account plus its
  username, a link to it found on a page becomes an account, and resolution credits links to it. Built-in platforms
  are tried first and are never shadowed. Recognition never adds automation: account transforms are gated on built-in
  platform ids. Templates whose username is a `?parameter` (`/member.php?u={username}`) are recognised by host, path and
  that parameter (`platforms._derive_query_rule`).
* **Export / import** (`GET /api/catalog/export`, `POST /api/catalog/import`; Settings, and the reset dialog offers the
  export first). The file (`format: sextant-catalog`, version 1) holds the user layer, on/off overrides of built-in
  sources and ignored suggestions, never API keys, investigations or settings. An import is a preview until confirmed
  (`dry_run` defaults to true) and is written in one transaction. The file is untrusted: each record is validated like
  form input, unknown fields are ignored, and references this installation lacks (connector, platform, jurisdiction)
  are dropped with a warning. Records already present (same id, or a source with the same address) are skipped unless
  "replace" is chosen (same id only). Categories with an existing name are merged. Importing the same file twice
  changes nothing.
* **Migration** (schema 3): sources added on country pages before this version (`source_capabilities.custom = 1`)
  move to the user layer with their ids, in one transaction.

## 5f. AI investigation assistant (`assistant/`)

An orchestration and reasoning layer over the engine, not a second engine. It reads the same registries and evidence
graph and acts only through existing engine operations.

```
input ─► planner.py  (identifiers, name readings, candidate handles, sources per input with reason/cost/value,
                      jurisdictions inferred or chosen + audit coverage, platforms, what will not run and why)
graph ─► analysis.py (verdicts, candidate identities, platform table, generated handles, next best actions,
                      progress, what was searched)                    ─► Assistant tab, plan on the start screen
       ─► copilot.py (run an action on your click · optional chat: the model acts only through tools)
       ─► providers.py (one AIProvider interface: Anthropic SDK · OpenAI · OpenRouter · local · compatible; failover, §5i)
```

* **No AI required.** Plans, verdicts, platform tables and next best actions are computed. The chat is optional
  (Settings → AI assistant provider, off by default) and replaceable. It sends the findings to the configured provider,
  never local-only data or keys; a local model keeps everything on this computer.
* **Candidate usernames** (`names.py`): common patterns over every plausible reading of the name (given/family order is
  ambiguous), labelled *Generated candidate · Not verified*. No invented numbers, never the local-only birth year, no
  nickname dictionary. They are tested only when the investigator approves ("Test N name-derived handles", or depth 4);
  an account found under one is a lead, and the handle match does not count as identity evidence (circular).
* **Verdicts** read the resolver conservatively. *Strong* needs a high score from two or more independent non-name
  signals; one strong signal is *Likely*; a name, similar handle, photo, city or country alone stays at
  *Insufficient evidence*. Every candidate shows its supporting (+) and contradicting (−) reasons. Profiles are grouped
  into candidate identities (cross-links, the resolver's SAME_AS, a shared handle) with the reason for the grouping.
* **Evidence classes**: provided (by you), observed (a source), derived (computed locally), generated (hypotheses),
  inferred (the resolver). Every graph edge opens "Why are these connected?" with each item's class and source.
* **Next best actions** are derived from the graph: a discovered handle, email or domain to search; a likely profile to
  read in full; handles to test; a candidate to review or corroborate; a source that asked for verification; a
  configured paid source (only with confirmation); official links of the investigation's countries.
* **Chat tools** (`copilot.TOOLS`, §5i): reading the investigation, acting on it and changing the catalog through
  proposals. The model cannot add identifiers of its own (only values the investigator typed), cannot run paid sources,
  and every call is stored.
* **Memory**: `assistant_log` (encrypted, append-only, deleted with the investigation) keeps approved actions, chat
  turns and tool calls; runs record what each source was sent; decisions stay in `user_decisions`.
* **Cost**: *Allow paid sources*, *Maximum estimated paid cost per investigation* and the per-investigation budget (§5j)
  are enforced where the engine decides availability and queues work.
* **Compliant anti-bot handling**: any response that is a CAPTCHA, bot check or browser challenge (Cloudflare,
  Turnstile, reCAPTCHA, hCaptcha, DataDome, PerimeterX…) ends the run as *Verification required — manual action needed*.
  It is never retried around. The investigator gets *Open in browser*, *Mark source manual-only* and *Try another
  configured source*. *Enhanced anti-bot handling* (Settings) adds half-rate pacing, patient Retry-After-aware retries
  and the automatic switch to another configured free source. It never solves or bypasses challenges, never uses
  logged-in sessions, and never automates a browser against platforms that forbid it.
* **Commercial providers** are catalog entries (`provider_class: commercial`, pricing, authentication, terms, data
  types) that show *Contact provider* until a connector configured with the investigator's own licence exists.

## 5g. Raw evidence

Every response a transform receives is fingerprinted (SHA-256) in the transform context. When the result is
merged, each source records the digests of the responses its facts came from (`Source.raw_refs`), and those
bodies (≤ 1 MB each) are stored encrypted in `raw_evidence`. A fact therefore keeps: source, URL, timestamp,
connector, confidence and a reference to the exact bytes it was extracted from
(`GET /api/investigations/<id>/raw/<sha256>`).

## 5h. Identity discovery (`discovery/`)

```
full name ─► name variants ─► plan_person: "name" site:<platform> (ranked, capped) ─► routed search provider
         ─► results with rank ─► parse_profile (URL → platform + handle; sub-pages, boards, ?param templates)
         ─► account (display name from the title, rank, query, provider) ─USES─► username (discovered)
         ─► pivot: plan_handle "handle" site:<platform> + platform checks ─► more accounts ─► resolver ─► identities
```

* **Targets** are data (`data/platforms/discovery.toml`): platform, priority, `site:` filter, countries where it is
  common, whether the first path segment is always the account; plus content path segments that are never handles and
  profile sub-pages to trim. Your own platforms with a profile-URL template become targets automatically.
* **Query planning** (`queries.py`): the full name, the name without diacritics and shorter display forms × platforms,
  scored by platform priority × name-variant prior × country boost (× a boost for login-walled platforms when the input
  is a handle), minus platforms already found; cut at the mode's budget (6 / 12 / 20 / 24 queries per person, 2–8 per
  handle). A focused request ("check Pinterest") restricts the plan to those platforms.
* **Transform**: `profile_discovery` on every search provider (`connectors/search/engines.py`), alongside `person_web`,
  `identifier_web` and `documents`. Every result that is a profile emits an account and a `username` entity with
  `discovery: search_result`, `search_rank`, `search_query`, `search_provider`; a handle surfaced by a *name* search whose
  title or handle echoes the name is marked `pivot` and searched further automatically (bounded by
  `discovery_pivot_cap`), the others are offered as next actions. Each query is written to the investigation's search
  journal (`TransformContext.journal`).
* **Routing** (`router.py`): automatic search runs go to one provider — your preference, then price per query, then
  reliability — and a provider that errors, refuses or lacks a key hands the same search to the next one
  (`Engine._search_failover`). `search_provider_mode = all` runs every provider.
* **Stages** (`stages.py`) report the eleven steps from the graph, the runs and the journal, and say "Web discovery
  unavailable — API-only search was used" when no provider was available.
* **Evidence**: a top-3 rank is a weak signal (`search_rank`, +0.3) that never counts alone; the explanation lists each
  signal as strong / medium / weak and what is missing ("no second independent corroborating source").
* **Identifier layer** (`assistant/identifiers.py`): a view over graph entities with the states Provided, Generated,
  Discovered, Observed, Possible, Verified and Rejected — the same records, never a parallel store.

## 5i. AI providers and the copilot

* `assistant/providers.py`: one `AIProvider` interface (`complete` → text, tool calls, native transcript, usage with
  prompt / completion / cached / reasoning tokens and, when billed, cost; `capabilities`; `provider_id`; `model`).
  `AnthropicProvider` (official SDK) and `OpenAICompatibleProvider` (chat completions through the guarded client) serve
  five profiles — anthropic, openai, openrouter, local, openai_compatible — configured in the `ai_providers` setting with
  their own keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`…). `ai_provider` is the primary,
  `ai_failover_order` the chain; disabled or unconfigured providers are never called. OpenRouter requests ask for usage
  accounting and its model list (`GET /models`, with published prices) feeds the pricing table.
* `assistant/copilot.py`: the chat loop tries the primary, then the chain, before any tool ran; a provider change keeps
  the conversation (other providers' turns are replayed as text). Each turn carries a compact state digest. Tools read the
  investigation (overview, identities, identifiers, discovery report, searches, costs, notes), act on it (next actions,
  search an identifier, discover profiles, add an identifier the investigator typed, notes) and on the catalog (audit,
  search, vocabulary, platform lookup, inspect a URL, discover sources, add/edit/link sources, categories, platforms,
  deletions). Catalog writes are proposals (`assistant/catalog_ops.py`) applied through `CatalogStore` — the same
  validation and duplicate checks as the form — on the investigator's click, or at once for non-destructive changes when
  `ai_auto_apply_catalog` is on; destructive ones always wait. Actions above `ai_action_preview_usd` or 20 calls become
  previews. Every model request is recorded in `usage_records`.
* `registry/source_discovery.py`: `inspect_url` (one guarded GET → title, description, ccTLD jurisdiction, category,
  official / commercial / API signals, duplicates, a draft) and `discover` (audit gaps + request → ≤ 4 queries through the
  primary search provider → candidates deduplicated by site, classified, ranked, minus what the catalog already has).

## 5j. Costs (`costs/`)

* `pricing.py`: the shipped table (`data/pricing/pricing.toml`: provider, service, connector or model, per-request and
  per-token prices, effective date, source URL, verified date), prices read from a provider's API, and your overrides
  (setting `pricing_overrides`), in that order of precedence. Unknown prices stay unknown.
* `ledger.py`: per investigation, connector runs (requests × price) and recorded usage (AI) grouped by normalized provider
  and by category (AI, search, APIs, commercial), with estimated / actual / pending / unknown, the ordered chain of
  billable steps, the forecast from the plan, and the budget check used by the engine (`Engine._budget_hold`: the
  held work waits in `budget_hold` until Continue raises the limit or Stop drops it) and by the copilot.
* Display currency (EUR by default) uses the USD→EUR rate you set; it is labelled as a conversion, not a live rate.

## 6. Storage

* SQLite in WAL mode in the data directory (`~/.local/share/sextant` by default).
* Tables: `investigations, entities, entity_properties, relationships, evidence, sources, connectors,
  transform_runs, search_tasks, observations, confidence_scores, api_credentials, jurisdictions,
  source_capabilities` (a mirror of the official sources, re-seeded at start), the catalog's user layer
  `catalog_sources, catalog_source_jurisdictions, catalog_source_platforms, catalog_categories, catalog_platforms,
  catalog_audit_ignores` (§5d), `usage_records` (billable usage per provider, no personal data) and `investigation_notes`
  (encrypted), plus `meta, checks,
  user_decisions, http_cache, raw_evidence` (encrypted response bodies) and `source_health` (statuses and
  timestamps only).
* Values that identify people (entity values and labels, property values, URLs, requests, credentials) are
  encrypted with Fernet. Lookups use HMAC-SHA256 blind indexes. The master key comes from
  `SEXTANT_PASSPHRASE` (scrypt) or a random key file with 0600 permissions. A test checks that no seeded
  identifier appears in plaintext anywhere in the data directory.
* The HTTP cache is per investigation and encrypted. Deleting an investigation deletes its cache.

## 7. Security model

| Threat | Control |
|---|---|
| A web page calling the local API (CSRF, DNS rebinding) | Host allow-list (421), random per-launch token in a custom header (401), Origin check (403); token passed in the URL fragment, never sent to servers |
| Hostile content in results (XSS) | React escaping, strict CSP (`default-src 'self'`, no inline script), only `data:` image thumbnails, external links `rel=noopener noreferrer` |
| SSRF from attacker-controlled URLs (profile links, redirects) | `SafeHttpClient`: scheme/port/credential checks, IP validation **at connect time** (blocks DNS rebinding and mixed answers), redirect re-validation, cross-host credential stripping, size caps, streaming downloads with caps |
| Parser attacks | defusedxml (no DTDs or entities), bounded strings, lists and data URLs in `TransformResult` |
| Adapter command injection | No shell. Arguments are lists; identifiers are normalized (a value starting with `-` cannot be a username); minimal environment; temporary working directory; timeout and output cap |
| Over-disclosure | Each manifest declares destinations and what is sent, shown before running. Local-only props are stripped from `EntityView` and from exports. Sanctions lists are downloaded and matched locally |
| Exported data abuse | CSV cells starting with `= + - @` are prefixed; JSON and HTML exports omit local-only data |

## 8. Frontend

React 19 + TypeScript (strict) + Vite, with Cytoscape.js + fcose for the graph. Hash routing
(`#/inv/<id>/graph`). The workspace keeps an entity/relationship store that is filled once, then updated from
the long-poll event stream (`entity`, `relationship`, `score`, `run`, `task`, `status`, `stats`,
`graph_changed`), so the graph grows live
during an investigation without re-rendering everything.

## 9. Desktop packaging (not done)

The engine already behaves like a desktop backend: random port, local token, browser launch. Wrapping it
with Tauri (sidecar Python executable built with PyInstaller; the webview loads `http://127.0.0.1:<port>/#token=…`)
is the planned route. It needs a Rust toolchain and per-OS signing, so it is not part of this prototype.

## 10. Extending

* New source → a connector (see CONNECTOR_SDK.md). New entity or relationship type → `register_entity_type` /
  `RELATION_TYPES` in `ontology.py`, plus a normalizer and a test.
* New source for a country → *Add source* / *Add link* in the UI (user layer), or, to ship it as a default,
  `data/jurisdictions/*.toml`; run
  `python -m sextant.tools.verify_catalog --only jur:<id>`. Shared portals → `[[regional]]` in `regional.toml`.
* Refresh the datasets → `python -m sextant.tools.import_datasets all`, then
  `python -m sextant.tools.promote_discovered --out data/jurisdictions/curated_global.toml` and review.
* New platform → `data/platforms/*.toml` (URL patterns are derived automatically; reserved paths are excluded). A
  platform with a documented public user API needs no code: declare `check = "probe"` and a `[platform.probe]`.
* Health and audit → `python -m sextant.tools.health_check [--all]`, `python -m sextant.tools.coverage_audit --out …`.
