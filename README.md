# Sextant

**A local-first OSINT investigation platform.** One input (a name, email, phone number, username, domain, IP,
profile URL or organization) starts an autonomous, multi-stage investigation. Transforms turn entities into new
entities. Every finding goes into an evidence graph with full source provenance, and identity resolution stays
conservative and explained. The result is a report that says exactly what was searched, what wasn't, and why.

```
input ─► entity extraction ─► transform selection ─► priority queue ─► connectors ─► normalisation
      ─► entity resolution ─► evidence graph ─► ranking ─► next transforms … ─► intelligence report
```

Think Maltego, but automated and beginner-friendly: press **INVESTIGATE**, watch the graph grow, then review,
confirm, separate and expand by hand.

> **Status: working prototype (v0.2).** Every part of the pipeline is real: there is no demo data and no simulated
> searches. Coverage depends on which sources you enable and which keys you configure, and the app shows that on
> every screen. A missing result never proves that information does not exist.

---

## What it does

| | |
|---|---|
| **Transform engine** | 146 transforms (entity → entities) in 123 connectors (105 implemented, 18 catalog-only or excluded). 68 connectors work with no key at all. Each transform declares its input and output entity types; the engine picks the ones that apply. |
| **Recursive investigation** | Priority queue: input boost × entity investigative value × transform priority × depth decay. Depth presets 1–4 or custom limits (depth, entities, requests, time, runs, per-connector caps, search-query caps). Every (transform, entity) pair is deduplicated, and the engine re-plans when new evidence raises an entity's value. |
| **Evidence model** | Every fact is an *observation* with a source (connector, URL, retrieval time, what was sent, licence, reliability). Entity properties are aggregated from observations, so merges are reversible and every value can be traced. |
| **Entity resolution** | A Fellegi–Sunter-style log-likelihood scorer with explained, signed reasons (+ supporting, − contradicting). Identity confidence is kept separate from investigative value. CONFIRM / SEPARATE / IGNORE decisions anchor the graph and are audit-logged. **A name match is never an identity match.** |
| **Graph workspace** | Cytoscape: zoom, pan, search, filters by type, confidence, status and year range, grouping, six layouts plus manual positions, expand, merge/unmerge, reject edges, evidence and source views, and a Maltego-style transform menu with RUN ALL and auto-investigate from here. |
| **Global coverage** | All **195 sovereign states** (193 UN members, the Holy See, the State of Palestine) are individual entries, plus 14 territories with their own registers, listed separately. For each country Sextant shows four layers it never mixes up: **country connectors** (automation), **curated official sources** (links), **regional portals** (PacLII, AfricanLII, HUDOC…), and **potential sources** discovered in maintained datasets (GLEIF registration authorities, OpenSanctions, Wikidata, IANA, the five RIRs, GDELT). A country with nothing is shown as *No connector currently available*, with the reason. The **Global coverage** screen and `python -m sextant.tools.coverage_audit` compute every count from the registries. |
| **Platforms** | About 8,300 registered: ~590 curated with capability data (category, country/region, public profile and public search support, official API and its requirements, private accounts, service status, how Sextant checks it) plus the WhatsMyName, Maigret and Sherlock site lists. Categories run from social, messaging and microblogging through gaming, music, dating, marketplaces, freelance, academic and genealogy, with regional platforms for China, Japan, Korea, Russia, Latin America, Africa and others. **Registered ≠ searchable:** each platform shows what this installation can do with it right now. Checks use official APIs, declarative public-API probes (no code per platform), the WhatsMyName rule engine (robots.txt respected), and optional Maigret/Sherlock adapters. Platforms whose terms forbid automation get manual links. |
| **Country connectors** | Company registers, courts and procurement with official APIs for the UK, US, France, Norway, Czechia, Finland, Estonia, Poland, Brazil, Australia, Latvia, Slovakia, Israel, Taiwan and Colombia, plus Romania's court portal and EU TED for EU/EEA states. They switch on automatically when an investigation points to their country. |
| **Public records** | Company registers, courts, gazettes, insolvency, sanctions, PEP and procurement sources per country. Records show *Public record found / Possible name match / Confirmed identity match / Unable to verify*; a name match is never an identity match. |
| **Identity discovery** | Finds handles nobody could guess from the name. For a person it asks the configured web-search provider for the name **on each platform** (`"Full Name" site:pinterest.com`, shorter display-name forms, regional platforms for the investigation's countries), reads the profile URLs in the results (sub-pages, boards and `?user=` templates included), and turns each into an account plus a *discovered* handle that keeps its rank, query and provider. Discovered handles are then searched on the other platforms (and platform-checked), correlated and ranked. Eleven stages, each reported with what actually ran; without a search provider the result says *"Web discovery unavailable — API-only search was used"* instead of "no username found". Searches are routed to one provider (preference, then price) with failover, never billed twice. |
| **AI investigation copilot** | A persistent assistant attached to each investigation: it remembers the conversation (across providers too), knows the identifiers, searches, results, costs and budget, and acts only through the application's tools — discovery ("check Pinterest and TikTok"), lookups (DNS, RDAP, certificate transparency, platform checks), audits, catalog search, URL inspection, source discovery, notes, cost reports. Catalog changes ("add this Swedish court to the legal sources") are validated proposals you apply with one click (optionally automatic for non-destructive changes; deletions always ask); large or costly actions show a preview first. Providers: **OpenAI, Anthropic, OpenRouter, a local model or any compatible endpoint**, each with its own model, key, temperature, output limit, reasoning setting and timeout, plus a failover order; every answer says which provider and model handled it. Everything still works without AI: plans, verdicts, explained evidence, identifier states, next best actions with estimated cost. |
| **Costs & budgets** | One ledger per investigation: AI, search, APIs and commercial providers, per normalized provider (one account, many services), as *estimated* (requests/tokens × the editable pricing table, with source URL and verification date), *actual* (the billed amount when a provider reports it, e.g. OpenRouter), *pending* or *unknown* (never a guessed amount). A forecast before you start, a per-investigation budget (€0.10 … unlimited) and "ask before exceeding": the next billable step that would pass the limit stops and asks (Continue / Stop). |
| **Your own catalog** | Add sources, reference links, categories and platforms in the UI: from Data Sources, from any jurisdiction, or from an audit suggestion. One record shows up everywhere it belongs (Data Sources, Coverage matrix, the jurisdiction, global coverage, filters, counts), persists, and can be edited, disabled or deleted from any of those places. A saved address is information, never automation; it can only reference an existing connector. Your platforms' profile links are recognised when pasted as input or found on pages, like built-in ones. Duplicates are flagged before saving. **Discover sources** (Data Sources) finds candidates through your web-search provider for a request ("commercial contact data providers covering Sweden") or a jurisdiction's gaps, classified and checked against the catalog, never added silently. Commercial providers (ZoomInfo, Apollo, Lusha, Kaspr, UpLead, Lead411, Acxiom, Experian, Equifax, Nielsen, Spokeo, Whitepages, Intelius, PeopleFinders… and any you add) carry access model, API, data categories as described by the provider, limitations and last verification, and are never queried without a connector configured with your licence. Every source has an access class (free public, free API, free tier, paid API, subscription, enterprise, manual, reference only, unavailable, not verified) you can filter on. **Audit jurisdiction** rates coverage and suggests dataset-backed additions (never invented, always "Not verified"); **Hard reset** (Settings) offers *reset catalog*, *reset investigation history*, *reset AI settings* and *reset API credentials* as separate choices, after a summary and typing RESET; **Export catalog** / **Import catalog** back it up to a JSON file and restore it (with a preview, skipping what you already have). |
| **SEARCH EVERYTHING** | A plan before anything runs: the inferred countries and their sources, every connector that applies with its status (ready / key missing / subscription missing / unavailable), the opt-in sources it will switch on, and the paid sources you configured. Then it runs every applicable category, connector and platform, recursively. |
| **Source health** | Each connector reports *Operational, Degraded, Authentication required, Subscription required, Temporarily unavailable, Deprecated, Broken, Not publicly searchable, Country restricted* or *Unknown*, with last-verified and last-successful-query dates from probes (`tools/health_check`) and real runs. |
| **Coverage honesty** | Per investigation: countries evaluated and applicable, platforms searched / unavailable / not searched, public databases queried / unavailable, commercial providers configured / not configured, and per source: queried, failed or blocked, needing credentials or a subscription, disabled, not installed, manual only, excluded by policy, not applicable. The UI and the report say that Sextant searched *all currently configured and applicable sources available to this installation*, never "the entire Internet". |
| **Evidence** | Every fact keeps its source, URL, retrieval time, connector and confidence, plus a SHA-256 reference to the exact raw response it came from (stored encrypted, viewable from the inspector). |
| **Report** | An 18-section HTML intelligence report with [S#] citations and an evidence appendix, plus full JSON (with provenance) and CSV exports. |

## Principles (enforced in code and tests)

* **Local-first.** The engine runs on your machine and binds to `127.0.0.1` behind a per-launch token, a Host
  allow-list and a strict CSP. The database is encrypted at rest (Fernet; HMAC blind indexes for lookups). There is
  no server run by this project. Each source receives only what its manifest declares, and you see that before it
  runs.
* **No bypassing.** No CAPTCHA solving, no bot-protection evasion, no spoofed browsers, no logged-in scraping, no
  password-reset probing, no leaked-credential retrieval. A 403 is reported as *blocked*, never as *not found*, and a
  source that refuses three runs in a row gets no more automatic requests in that investigation.
  A CAPTCHA or bot check is reported as *Verification required — manual action needed*, with the page to open
  yourself; *Enhanced anti-bot handling* only paces, waits and switches to another configured source.
  Paid sources run only with **your** credentials. Subscription sources stay out of auto-investigation unless you
  switch them on per source. The exceptions are HIBP (flat monthly fee) and the web-search APIs (Brave, Serper, Tavily,
  Google): once you add a key they run automatically, routed to one provider at a time, within the per-investigation
  query cap and your budget, both shown before you start.
* **Conservative identity.** Records link only to *name strings*, never to the target person. Birth year is
  local-only: it is used to rule namesakes out, is never sent to a source, and never appears in an export. Criminal
  and court records show the role, jurisdiction and official URL, and are always labelled *Possible name match*
  until **you** confirm them.
* **Excluded by policy:** holehe, GHunt, DeHashed, IntelX and PimEyes are listed in the catalog with the reason.
  Logged-in scrapers (Osintgram and similar) are not integrated at all. LinkedIn is offered as manual-check links only.

## Quick start

Requirements: Python ≥ 3.11. Node is **not** needed to run the app, because the built UI is in `frontend/dist`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional: add any API keys you have (all optional)
python run.py                   # opens http://127.0.0.1:<port>/#token=… in your browser
```

Options: `--port 8765`, `--data-dir PATH`, `--no-browser`, `--env-file PATH`, `--debug`.
Data lives in `~/.local/share/sextant` (Windows: `%APPDATA%\Sextant`) unless `SEXTANT_DATA_DIR` is set. Set
`SEXTANT_PASSPHRASE` to derive the database key from a passphrase instead of a key file.

### Optional extras

| Extra | How |
|---|---|
| More coverage | **Data Sources → Configure** on any source, or keys in `.env`. Every key is listed in `.env.example`, grouped by free / free-with-registration / approval / pay-per-use / paid. |
| Private web search | Run your own SearXNG: `deploy/searxng/` (Docker, bound to 127.0.0.1). |
| Maigret / Sherlock | `pipx install maigret` / `pipx install sherlock-project`; detected on `PATH` and **on by default once installed**. Their requests do not pass through Sextant's guards, so each run uses a policy-filtered copy of the tool's site list (no platforms whose terms forbid automation, no mirrors of them, no adult/dating sites unless enabled). Maigret uses its **current** site database (fetched through Maigret's own update channel: version check + SHA-256, refreshed daily; Settings → *Maigret site database* switches to the bundled one), checks its top 500 sites per run (Settings → *Maigret sites per normal run*) and all of them with SEARCH EVERYTHING. Switch either off under Data Sources. |
| theHarvester | Needs Python ≥ 3.14; install it separately and set `THEHARVESTER_PATH`. Passive sources only. |
| SpiderFoot | Unmaintained since 2023, so off by default; set `SPIDERFOOT_PATH` to the existing `sf.py`. |

### Rebuilding the UI

```bash
cd frontend
npm ci && npm run build         # on filesystems without symlinks (e.g. VirtualBox shares): npm ci --no-bin-links
```

`npm run dev` serves the UI with hot reload and proxies `/api` to `127.0.0.1:8765` (start the engine with
`python run.py --port 8765 --no-browser` and open `http://localhost:5173/#token=…`).

## Tests

```bash
pip install -r requirements-dev.txt
pytest                          # 400+ offline tests (recorded responses and fixture search indexes, no network)
pytest -m live                  # opt-in checks against real public APIs (GitHub, RDAP, DNS, RIPEstat, GLEIF, Wikidata, Codeforces)
python -m sextant.tools.verify_catalog [--discovered]   # re-check catalog URLs (run from backend/)
python -m sextant.tools.health_check [--all]           # probe connectors with public test inputs ("last verified")
python -m sextant.tools.import_datasets all            # refresh countries, discovered sources and platform lists
python -m sextant.tools.coverage_audit --out ../docs/audit/global-coverage-audit.md
```

The suite covers the SSRF guard (DNS rebinding, redirects, credential stripping), the evidence model
(reversible merges), namesake safety (a name-only record stays below 40%, contradictions are kept), the engine
(depth, deduplication, budgets, continue, manual mode, persistence), connector parsers, encryption at rest (no
plaintext identifiers in the data folder), the API guard and the exports (no local-only data), and the identity-discovery
pipeline end to end on fixture search results (`tests/test_discovery.py`: full name → site-restricted query → result URL →
platform → handle → candidate → evidence → pivot to other platforms, including handles no template could produce).

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, scheduling, storage, security.
* [docs/ONTOLOGY.md](docs/ONTOLOGY.md): entity and relationship types, the evidence model, resolution.
* [docs/CONNECTOR_SDK.md](docs/CONNECTOR_SDK.md): how to write connector #1000 (with tests).
* [docs/research/ECOSYSTEM.md](docs/research/ECOSYSTEM.md): which projects, APIs and datasets were evaluated, and why
  each was integrated, adapted, catalogued or excluded.
* [docs/audit/](docs/audit/): the coverage audit before the global refactor and the generated audit after it.


## Screenshot

<img width="1920" height="968" alt="Screenshot_2026-10-07_10_56_12" src="https://github.com/user-attachments/assets/1c252ce5-e3ab-42c8-ad33-0a9881d8e32f" />


## Use responsibly

Sextant is for lawful investigations: due diligence, journalism, security research, self-audit, and fraud and
sanctions screening. Check the laws that apply to you (e.g. GDPR purpose limitation) before investigating a person.
Record a purpose when you create an investigation, and treat every unconfirmed match as unverified.

## Licence

Sextant is free for everyone and must never be sold. It is released under the
[PolyForm Noncommercial License 1.0.0](LICENSE): you may use, study, modify and share it for any non-commercial purpose
(personal use, research, journalism, education, charities, public institutions), and no one may use it commercially or
sell it. The author's intent is that the project stays free and non-commercial for everyone, including the author.

Third-party data bundled with it (WhatsMyName, Maigret and Sherlock site lists, public datasets) stays under its own
licence, shown per source.
