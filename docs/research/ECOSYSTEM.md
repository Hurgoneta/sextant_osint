# Phase 1 — OSINT ecosystem research (checked 2026-10-06)

This document records what was verified before the platform was designed, and the integration
decision taken for each project or source. Every status below was checked on **2026-10-06**:

* **Projects:** GitHub API (licence, last push, archived flag, latest release) plus the projects' own CLI
  definitions.
* **Free APIs:** a live request with a neutral query (company names, GitHub's `octocat` mascot, `example.com`).
* **Commercial APIs:** vendor pricing and policy pages.

Re-run the checks before relying on them: `python -m sextant.tools.verify_catalog` re-checks
every catalog URL and prints the dead ones.

## 1. Maintained open-source OSINT projects

| Project | Licence | Activity | Interface | Decision |
|---|---|---|---|---|
| **Maigret** (soxoj/maigret) | MIT | v0.6.6 (2026-09-18), active | Python package + CLI; `-J simple` JSON report, `--folderoutput` | **Adapter (subprocess), on by default when installed** (since 2026-10-06, at the user's request). Each run gets a filtered copy of Maigret's **current** database, fetched through Maigret's own update channel (`db_meta.json`: `min_maigret_version` check, SHA-256 verification, daily cache; falls back to the bundled database), passed as `--db … --no-autoupdate`. The filtered copy is test-loaded with Maigret's own loader first, and a run whose output shows Maigret fell back to its unfiltered bundled list is discarded: platforms whose terms forbid automation, their third-party mirrors (ImgInn, Picuki, Nitter…) and, unless enabled, adult/dating sites are removed. Normal runs check the top N sites (`maigret_top_sites`, default 500); SEARCH EVERYTHING checks all. The adapter disables Maigret's recursion and **never passes `--cloudflare-bypass`, `--use-disabled-sites`, `--cookies-jar-file` or proxies**. |
| **Sherlock** (sherlock-project/sherlock) | MIT | v0.16.2 (2026-09-08), active | CLI; `--csv`, `--folderoutput`, `--timeout` | **Adapter (subprocess), on by default when installed** (since 2026-10-06). Runs on a policy-filtered copy of its own site list (`--json`); NSFW sites stay off (`--nsfw` is never passed). |
| **WhatsMyName** (WebBreacher/WhatsMyName) | CC BY-SA 4.0 (data) | pushed 2026-09-16 | JSON dataset (`wmn-data.json`, ≈260 KB, 700+ sites) | **Native engine**: we evaluate the published detection rules ourselves, so requests pass through our SSRF guard, rate limiter and robots.txt policy. Attribution is shown in the catalog. |
| **theHarvester** (laramies/theHarvester) | **GPL-2.0** | 4.11.1 (2026-06-03), active; **requires Python ≥ 3.14** | CLI; `-f NAME` writes NAME.json/.xml | **Adapter (subprocess only)**. Running it as a separate program keeps our MIT code free of GPL obligations. Only *passive* sources are passed with `-b`. The adapter never uses `--dns-brute`, `--take-over`, `--screenshot`, `--shodan` or `--api-scan`. |
| **SpiderFoot** (smicallef/spiderfoot) | MIT | last release v4.0 (**2022-04-07**), last code commit 2023-11 | `sf.py -s TARGET -m modules -o json -q` | **Adapter (subprocess), off by default**. It is effectively unmaintained, but its 200+ modules are still useful to people who already run it. Event types are mapped to our ontology. |
| Recon-ng | GPL-3.0 | active | interactive console + modules | Not integrated. The module workflow is console-driven and has no stable JSON output. |
| OWASP Amass | Apache-2.0 | v5.1.1 (2026-04-07) | Go CLI | Catalogued as an optional infrastructure adapter (future). Passive mode only. |
| subfinder (ProjectDiscovery) | MIT | active | Go CLI, `-oJ` | Catalogued (future adapter); CT logs + Certspotter already cover passive subdomains. |
| PhoneInfoga | GPL-3.0 | last release v2.11.0 (2024-02) | Go CLI/API | Not integrated: stale, and its scanners are mostly search-engine dorks we already generate. |
| **holehe** | GPL-3.0 | last push 2024-09 | Python | **Excluded by policy.** It infers account existence by triggering *password-reset / registration* flows on third-party sites, which abuses authentication endpoints. |
| **GHunt** | AGPL-3.0 | active | Python | **Excluded by policy.** It requires the investigator's Google session cookies and queries internal Google APIs. |
| Osintgram, Instagram scrapers | GPL-3.0 / various | — | — | **Excluded.** They automate a logged-in Instagram session against Meta's terms. |
| twint | MIT | **archived** | — | Excluded (dead; scraped X). |
| social-analyzer | AGPL-3.0 | 2026-01 | Node/Python | Not integrated (AGPL, overlaps Maigret/WMN). |
| h8mail, DeHashed/IntelX-style tools | various | — | — | **Excluded.** They retrieve raw leaked credentials. We handle breach *metadata* only. |

## 2. Libraries

| Need | Choice | Licence | Notes |
|---|---|---|---|
| HTTP | httpx + httpcore | BSD-3 | Custom network backend validates IPs at connect time (SSRF/DNS-rebinding guard). |
| DNS | dnspython 2.8 | ISC | Passive lookups only, no zone transfers or brute force. |
| Phone | phonenumbers 9 | Apache-2.0 | Offline metadata (region, carrier of allocation, line type). |
| Domains | tldextract 5 (offline PSL snapshot) | BSD-3 | Registered-domain detection without network fetches. |
| XML | defusedxml | PSF | Sanctions lists and SOAP responses. |
| Images | Pillow (optional) | HPND | Perceptual hashes (dHash + pHash) for profile-photo correlation. |
| Entity resolution | **built-in** log-likelihood scorer | — | Splink (MIT, active) and dedupe (MIT) target bulk table deduplication. Our problem is scoring each candidate against *one* target, using heterogeneous evidence that has to be explained. A transparent Fellegi–Sunter-style weight table fits better, and every weight can be reviewed. |
| Graph UI | Cytoscape.js 3.34 + cytoscape-fcose 2.2 | MIT | Compound nodes (grouping), many layouts, manual positioning. sigma.js (WebGL) was considered for >20k nodes; investigations are capped well below that. |
| UI | React 19 + TypeScript + Vite | MIT | Built once; the Python engine serves the static bundle, so Node is not needed at runtime. |
| Browser automation | Playwright (Apache-2.0) | — | **Not used.** Pages that need a real browser are almost always behind login walls or bot protection, and we do not evade those. |

## 3. Data sources verified live (no key needed)

| Source | Endpoint checked | Result |
|---|---|---|
| Shodan InternetDB | `internetdb.shodan.io/{ip}` | 200, ports/hostnames/CPEs |
| crt.sh | `crt.sh/?q=%.domain&output=json` | **502** (chronically overloaded) — kept, with Certspotter as fallback |
| Certspotter (SSLMate) | `api.certspotter.com/v1/issuances` | 200 (unauthenticated quota) |
| GLEIF LEI | `api.gleif.org/api/v1/fuzzycompletions` | 200 |
| Wikidata | `wbsearchentities` | 200 |
| ORCID public API | `pub.orcid.org/v3.0/expanded-search` | 200 |
| Crossref | `api.crossref.org/works?query.author=` | 200 |
| OpenAlex | `api.openalex.org/authors?search=` | 200 |
| Wayback CDX / availability | `web.archive.org/cdx/search/cdx` | 200 |
| GDELT DOC 2.0 | `api.gdeltproject.org/api/v2/doc/doc` | 429 unless ≤ 1 request / 5 s → connector rate-limited accordingly |
| RIPEstat | `stat.ripe.net/data/whois` | 200 |
| Nominatim (OSM) | `nominatim.openstreetmap.org/search` | 200 (policy: ≤ 1 req/s, identifying UA) |
| Bluesky AppView | `public.api.bsky.app/xrpc/app.bsky.actor.*` | 200 |
| keys.openpgp.org | `/vks/v1/by-email/` | 404 = no key (clean signal) |
| Stack Exchange | `api.stackexchange.com/2.3/users?inname=` | 200 |
| Hugging Face | `huggingface.co/api/users/{u}/overview` | 200 |
| npm registry | `registry.npmjs.org/-/v1/search?text=maintainer:` | 200 |
| crates.io | `crates.io/api/v1/users/{u}` | 200 |
| Lobsters | `lobste.rs/~{u}.json` | 200 |
| Mastodon (any server) | WebFinger + `/api/v1/accounts/lookup` | 200 |
| Discourse (any forum) | `/u/{u}.json` | 200 |
| Lemmy (any instance) | `/api/v3/user?username=` | 200 |
| Keybase | `user/lookup.json` | 200 |
| GitHub / GitLab / Codeberg | REST APIs | 200 |
| **Reddit** | `/user/{u}/about.json` unauthenticated | **403**. See §4 |
| France — Recherche d'entreprises | `recherche-entreprises.api.gouv.fr/search` | 200, includes directors (`dirigeants`) |
| Norway — Brønnøysund | `data.brreg.no/enhetsregisteret/api/enheter` | 200 |
| Czechia — ARES | `ares.gov.cz/.../ekonomicke-subjekty/vyhledat` (POST) | 200 |
| Poland — KRS | `api-krs.ms.gov.pl/api/krs/OdpisAktualny/{krs}` | 200 (lookup by KRS number only, no name search) |
| Finland — PRH/YTJ | `avoindata.prh.fi/opendata-ytj-api/v3/companies` | 200 |
| Estonia — e-Business Register | `ariregister.rik.ee/est/api/autocomplete` | 200 |
| Brazil — CNPJ (BrasilAPI mirror of Receita Federal open data) | `brasilapi.com.br/api/cnpj/v1/{cnpj}` | 200, includes partners (QSA) |
| US — SEC EDGAR full-text search | `efts.sec.gov/LATEST/search-index` | 200 (requires a UA with contact) |
| UK — Find Case Law | `caselaw.nationalarchives.gov.uk/atom.xml?query=` | 200 (Open Justice Licence; *bulk computational analysis* needs a separate licence) |
| US — CourtListener | `/api/rest/v4/search/?type=r` | 200 anonymously |
| EU — TED procurement | `api.ted.europa.eu/v3/notices/search` | 200 |
| US — USAspending | `api.usaspending.gov/api/v2/autocomplete/recipient/` | 200 |
| UK — Contracts Finder | `/Published/Notices/OCDS/Search` | 200 |
| Romania — data.gov.ro (CKAN) | `package_search` | 200 |
| EU / UN / OFAC / UK sanctions | full-list downloads | 200 (EU list ≈ 25 MB) |
| OpenSanctions bulk | `data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv` | 200, **74 MB** (`default` 465 MB, `peps` 180 MB), **CC BY-NC 4.0** |

## 4. Sources that need credentials, approval or payment

| Source | Status on 2026-10-06 | How the platform treats it |
|---|---|---|
| UK Companies House | free API key | Connector; *API key required* until configured |
| OpenCorporates | API token (401 without) | Connector; *API key required* |
| OpenSanctions API | key required. Free keys go to journalism, civil society and academia; commercial use €0.10/query | Connector (key). Bulk CSV is matched locally without a key, for **non-commercial use** only (licence shown in the catalog) |
| Swiss Zefix REST | 401 (credentials since 2025) | Catalog + connector interface (credentials) |
| Australia ABN Lookup | free GUID registration | Connector; *registration required* |
| Denmark CVR (cvrapi.dk) | 403 to automated clients | Catalog only (official Erhvervsstyrelsen access requires registration) |
| **Reddit** | Responsible Builder Policy (Nov 2025): **new API access requires manual approval** | Connector works with approved credentials; otherwise *credentials required*, never "not found" |
| **X (Twitter)** | Pay-per-use since 2026-01; free tier closed to new developers 2026-02-06; user lookup ≈ $0.01 | Connector; *paid (pay-per-use)*. Cost is shown before running |
| Instagram | Graph API *Business Discovery* only (business/creator accounts, needs a Meta app + IG business token) | Connector for Business Discovery; otherwise a manual link |
| Threads | API returns only the authorised user's own profile | Manual link only |
| TikTok | Research API restricted to approved academics | Catalog only (manual link) |
| LinkedIn, Facebook | no lookup API; ToS forbid automation | Manual links only |
| YouTube Data API v3 | free key, 10,000 units/day | Connector (key) |
| Twitch Helix | free client credentials | Connector (key) |
| Steam Web API | free key | Connector (key) |
| **Brave Search API** | free tier replaced (Feb 2026) by $5 monthly credit (≈1,000 queries), card required | Connector; *metered API* |
| **Google Custom Search JSON API** | closed to new customers; **shuts down 2027-01-01** | Connector kept for existing keys, flagged *sunsetting* |
| Bing Web Search API | retired Aug 2025 | Catalog: *retired* |
| SerpAPI | paid | Connector (key) |
| SearXNG | self-hosted (free) | Connector, the most private search option |
| Have I Been Pwned | paid tiers from $3.95/month (Pwned 1, 10 RPM) | Connector (key); breach metadata only |
| XposedOrNot | free, third-party aggregator | Connector, opt-in |
| Hudson Rock Cavalier (community) | free | Connector, opt-in; info-stealer *metadata* only |
| Shodan / Censys / SecurityTrails / VirusTotal / urlscan / AbuseIPDB / GreyNoise / ipinfo | free keys or paid tiers | Connectors (key) |
| Hunter.io / EmailRep | key (free tiers) | Connectors (key) |
| Twilio Lookup / NumVerify (apilayer) | paid / key | Connectors (key) |
| People Data Labs / Pipl | commercial subscriptions (KYC) | Connectors (key); the user's own subscription |
| TinEye | paid key | Connector (key) |
| LexisNexis, Westlaw, ZoomInfo, Social Links, D&B | enterprise contracts, no public API docs | Catalog only: *contact provider* |

## 4a. Maintained datasets behind the global registries (checked 2026-10-06)

| Dataset | Used for | Licence | Size on 2026-10-06 |
|---|---|---|---|
| UN M49 standard country/area codes | the 195-country registry (ISO codes, regions, LDC/LLDC/SIDS) | UN terms (free, attribution) | 249 areas |
| Wikidata (SPARQL) | native names, official languages, FIPS codes; national institutions with official websites (gazettes, statistics offices, courts, parliaments, registers, regulators, archives, news agencies, open-data portals…) | CC0 | 2,775 institutions |
| GLEIF Registration Authorities list (v1.9) | business / company registers worldwide | GLEIF, free of charge | 1,178 registers, 234 jurisdictions |
| GLEIF LEI API | legal entities with an LEI per country | CC0 (LEI data) | 209 counts |
| OpenSanctions dataset index | official sanctions, PEP, debarment, wanted and regulatory publishers per country | CC BY-NC 4.0 (metadata) | 439 source datasets |
| IANA root zone database + RDAP bootstrap | ccTLD registry operators, WHOIS servers, RDAP availability | public registry data | 208 ccTLDs, 58 with RDAP |
| RIR delegated statistics (ARIN, RIPE NCC, APNIC, LACNIC, AFRINIC) | internet number registry and allocated resources per country | public statistics | 242 country codes |
| GDELT lookup tables | news filtered by the outlet's country (FIPS) | open, attribution | 209 countries |
| WhatsMyName / Sherlock / Maigret site lists | platform registry (metadata only; detection stays with the tools) | CC BY-SA 4.0 / MIT / MIT | 715 / 478 / 7,918 sites |

All of them are refreshed by `python -m sextant.tools.import_datasets all`; the snapshot files record the source
URL, licence and retrieval time. Not usable for discovery: the OpenCorporates jurisdictions list (needs an API
token), the Open Contracting data registry (no public API found).

### Country APIs verified live and implemented (2026-10-06)

| Country | Source | Interface | Connector |
|---|---|---|---|
| Latvia | Register of Enterprises open data (companies + board members) | CKAN datastore (data.gov.lv), CC0 | `lv_ur` |
| Slovakia | Register of Legal Entities (RPO) incl. statutory bodies | REST, CC BY 4.0 | `sk_rpo` |
| Israel | Registrar of Companies dataset | CKAN datastore (data.gov.il) | `il_companies` |
| Taiwan | GCIS company registration (active companies, representative) | OData-style REST | `tw_gcis` |
| Colombia | SECOP II electronic contracts | Socrata (datos.gov.co), CC BY-SA 4.0 | `co_secop` |

Checked and **not** implemented: Singapore ACRA open data (split into per-letter datasets, no unified search);
Sweden, Croatia, Switzerland, Ireland, Denmark, Japan (Houjin Bangou, gBizINFO), South Korea (Open DART), New Zealand
— official APIs exist but need a registration/key that could not be verified here; Nigeria (CAC), Kenya (BRS),
Germany (Handelsregister), Russia (EGRUL) — only the site's internal endpoints or CAPTCHA-protected pages, which
Sextant does not use. These countries show their official sources as links.

### Public user APIs used as declarative probes

Mojang (Minecraft), Roblox users API, Scratch, Modrinth, Qiita, Hex, MetaCPAN, Liberapay, Open Library, Kitsu,
Gitea.com, Launchpad, RuneScape hiscores, Codewars, RubyGems owners, Packagist vendors, Audius and the Hive
blockchain RPC: each answered a known account and an unknown one correctly on 2026-10-06. Rejected: Kick, Wattpad
and Duolingo JSON endpoints (internal endpoints of the sites' own apps), hashnode GraphQL (blocked).

## 5. Decisions that follow from the research

1. **Native first, adapters second.** Every native connector goes through the guarded HTTP client
   (SSRF guard, per-source rate limits, honest User-Agent, response caps, per-investigation cache).
   External tools run as subprocesses. The UI says plainly that their traffic does **not** pass
   through those guards.
2. **No evasion, ever.** No CAPTCHA solving, no Cloudflare bypass, no spoofed browser identity, no
   logged-in scraping, no third-party mirrors of platforms that forbid automation, no
   password-reset probing. When a source refuses access, the run is recorded as *blocked*, never as
   *not found*.
3. **Paid means paid.** Commercial sources are connectors that stay *unavailable* until the user adds
   their own credentials. Cost is part of every manifest and shown before a run.
4. **Licences travel with the data.** WhatsMyName (CC BY-SA), OpenSanctions bulk (CC BY-NC) and the
   UK Find Case Law terms are recorded in connector manifests and shown in the catalog and report.
5. **Coverage is explicit.** The platform never claims to have searched "the internet". Every
   investigation reports which sources were queried, unavailable, credential-gated, paid or not
   applicable.
