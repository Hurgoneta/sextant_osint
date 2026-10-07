# Coverage audit before the global refactor (2026-10-06)

This audit was taken from the code and data as they were *before* the global-registry work. All numbers come
from loading the registries (`sextant.registry.*`) and the connector manifests, not from estimates.

## 1. Current coverage

### Countries / jurisdictions

* **78 jurisdiction entries**, but only **74 are countries**. The other four are aggregates: `INT`
  (international), `EU`, `AFR` (one "Africa" entry with a single link) and `PAC` (one "Pacific" entry).
* By region: Europe 36, Asia 14, Africa 8 (+1 aggregate), South America 6, North America 3, Middle East 3,
  Central America & Caribbean 2, Oceania 2 (+1 aggregate).
* **370 official sources** in total: company registers 83, open data 58, judgments 56, gazettes 35,
  procurement 33, own-record requests 24, court portals 14, and smaller groups.
* **28 of the 370 are automated by a connector.** Twelve countries have a country-specific connector:
  US (5), GB (3), FR, NO, CZ, FI, EE, PL, BR, AU, RO and MD (one each). The other 62 countries have links only.
* Several requested European countries were thin: Sweden 5 sources and 0 automated, Hungary 3 and 0,
  Germany 8 and 0.

### Social and community platforms

* **117 curated platforms**: developer 22, social 17, blogs 11, art 11, forums 9, video 8, academic 7,
  gaming 7, music 6, identity 5, fitness 5, marketplaces 4, chat 2, professional 2, dating 1.
* How each is checked: 76 by the WhatsMyName rule engine, 28 by dedicated API connectors, 12 manual links only,
  and 3 with no check at all (`xbox`, `tinder`, `discord`).
* Only 11 platforms carry a country or region tag (RU, CN, KR, BY, KZ, UA, DE/AT/CH). Every other platform is `*`.
* WhatsMyName (700+ sites) is merged in at runtime, but only after its first download. Maigret (~3,000 sites) and
  Sherlock (~400) count only if installed, and their site lists never reach the registry.

### Connectors

* **117 connectors, 138 transforms.** With no keys configured: 62 available, 18 need an API key, 11 need a
  subscription, 11 catalog only, 6 disabled, 5 excluded by policy, 2 not installed, 1 retired, 1 manual.

## 2. Expected coverage (the requirement)

* All 195 sovereign states as individual entries: 193 UN members, the Holy See and the State of Palestine.
  Every country appears even with zero sources, and shows its status, reason, potential sources and last check.
* Country-aware source categories: government, company and business registers, filings, courts, professional
  and regulatory registers, licensing, procurement, gazettes, statistics, open data, directories, elections,
  property, academic, news, local search engines, forums, local social platforms, archives, domain registries,
  DNS/infrastructure and commercial providers.
* 100+ platforms (hundreds as the goal), each with capability metadata covering public search, API, key,
  authentication, private accounts and connector status, across the full category list in the request.
* Registered ≠ searchable, shown per source; operational status with last-verified and last-success dates.
* A global coverage screen, a coverage panel, and per-investigation coverage counts (countries, platforms,
  public databases, commercial providers) that are never invented.

## 3. Missing coverage

* **121 sovereign states had no entry at all.** From the user's minimum European list: Albania, Bosnia and
  Herzegovina, Liechtenstein, Montenegro and North Macedonia. Also missing were Andorra, Belarus, Monaco,
  Russia, San Marino and the Holy See. Africa had 8 of 54 states, and Oceania 2 of 14.
* Of the platforms the user listed, 15 were absent from the registry: WhatsApp, Messenger, Signal, Viber, WeChat,
  QQ, Douyin, Kuaishou, LINE, KakaoTalk, BeReal, Clubhouse, Foursquare, Swarm and MySpace.
* Several platform categories did not exist: microblogging, photography, travel, books, film, freelance, jobs,
  education, podcasts, reviews, events, crowdfunding/donation, newsletters, package registries, research
  repositories, Q&A, public chat directories and business directories.
* Several jurisdiction source categories did not exist: statistics, national archives, parliament,
  central bank / securities regulator / stock exchange, data protection authority, election commission,
  domain (ccTLD) registry, internet number registry (RIR), news media, local search engines, people
  directories and commercial providers per country.

## 4. Duplicates, placeholders, fake/demo connectors

* **No fake or demo connectors were found.** The only "demo" string is the FEC connector's documented
  fallback to api.data.gov's public `DEMO_KEY`, which is a real, rate-limited key.
* **Placeholders, declared honestly as `catalog_only` (11):** lexisnexis, westlaw, zoominfo, dnb, crunchbase,
  domaintools, censys, spycloud, social_links, tiktok_research, whoisxml. `bing_web_search` is retired.
  These are labelled "Connector interface only" in the UI, but the coverage counts did not separate them from
  real connectors clearly enough.
* **Platforms registered with no check (3):** xbox, tinder and discord had `check = "none"` and no status
  explaining why.
* **Overlaps (intentional, but not shown):** Sherlock, Maigret and WhatsMyName check many of the same sites.
  `opensanctions` (API) and `opensanctions_bulk` (local CSV) overlap. `reddit` is both a platform and a
  forum-category connector.

## 5. Hard-coded to Romania / EU

* `ro_courts` was the only connector with the `country_preselect` mechanism, so Romania was the only country
  whose sources switched on automatically when an investigation pointed to it.
* The EU had its own aggregate jurisdiction (TED, EU sanctions), and Europe held 36 of the 74 countries.
* `AFR` and `PAC` lumped whole continents into one entry, which is exactly the "Africa connector" pattern
  the requirement rejects.

## 6. Architectural limitations

1. **The jurisdiction registry was hand-written TOML only.** There was no importer from maintained datasets,
   so adding countries meant typing them in, and nothing guaranteed all 195 states existed.
2. **No canonical country list.** Country detection used only the names in the jurisdiction files, so
   locations in the 121 missing countries could never be recognised.
3. **The platform registry was curated-only.** Community datasets were merged only for display, at runtime.
   Platforms had no status, no country data beyond 11 entries, and no capability fields such as
   "unauthenticated search", "private accounts" or "public search".
4. **No source health model.** URL checks existed (`_verification.json`), but connectors had no
   *operational / degraded / broken / deprecated* status and no *last successful query* date.
5. **Every connector was a Python class.** There was no declarative, config-driven connector type, so
   N countries with the same kind of open-data API meant N hand-written classes.
6. **Investigation coverage was per connector only.** It had no country, platform, public-database or
   commercial-provider dimension.
7. **No raw-evidence reference** on sources (the HTTP cache existed, but sources did not point into it).

## 7. Implementation plan

1. **Country registry (195 + territories).** A canonical ISO list with UN status, region and subregion,
   aliases, ccTLD and calling code. Every jurisdiction file merges into it, and a test fails if any of the
   195 is missing.
2. **Data-driven source discovery.** Importers for maintained, licensed datasets, each entry with provenance:
   * IANA root zone database + IANA RDAP bootstrap → ccTLD registry and RDAP availability per country;
   * RIR mapping → internet number registry per country;
   * GLEIF Registration Authorities list → business registers worldwide;
   * OpenSanctions dataset index → official sanctions, PEP, debarment and wanted-list publishers per country;
   * Wikidata (CC0) → statistics offices, gazettes, parliaments, courts, archives, central banks, data-protection
     and election authorities, with official websites.
   Imported entries are labelled *discovered (dataset)* and are never presented as verified.
3. **Platform registry v2.** An expanded vocabulary and capability model, 300+ curated platforms, and
   importers for the WhatsMyName, Sherlock and Maigret site lists (licences recorded). Platforms gain a status
   and per-country data.
4. **Declarative connectors.** A `profile_probe` check for platforms with public JSON endpoints, and generic
   connectors for open-data APIs, both declared in TOML and registered at load time.
5. **Health model.** Operational status per source (Operational, Degraded, Authentication required,
   Subscription required, Temporarily unavailable, Deprecated, Broken, Not publicly searchable, Country
   restricted, Unknown), with last-verified and last-success dates persisted from real runs.
6. **Coverage model and UI.** A Global Coverage screen (195-country matrix, country detail with potential
   sources), coverage panels, SEARCH EVERYTHING with a pre-run plan, and per-investigation counts for
   countries, platforms, public databases and commercial providers. All of it is computed from the registries
   and the runs.
7. **New verified connectors** for countries and platforms that have free public APIs, each checked live.
8. **Tests, documentation, and a final audit generated by** `python -m sextant.tools.coverage_audit`.
