// Shapes returned by the local Sextant API (see backend/sextant/api).

export type EntityStatus = "seed" | "candidate" | "confirmed" | "separated" | "ignored";
export type Tier = "high" | "medium" | "low" | "";

export interface EntitySummary {
  id: string;
  key: string;
  type: string;
  value: string;
  label: string;
  status: EntityStatus;
  depth: number;
  confidence: number;
  tier: Tier;
  value_score: number;
  match_label: string;
  years?: [number, number] | null;   // earliest/latest year among dated facts
  thumb?: string;
  platform?: string;
}

export interface RelSummary {
  id: string;
  source: string;
  target: string;
  type: string;
  confidence: number;
  status: "active" | "rejected" | "confirmed";
  evidence: number;
}

export interface GraphPayload {
  entities: EntitySummary[];
  relationships: RelSummary[];
  stats: Stats;
  target: string | null;
  seq: number;
}

export interface Stats {
  entities: number;
  relationships: number;
  observations: number;
  sources: number;
  high_confidence: number;
  by_type: Record<string, number>;
}

export interface Limits {
  depth: string;
  max_depth: number;
  expand_threshold: number;
  max_entities: number;
  max_requests: number;
  max_minutes: number;
  max_runs: number;
  connector_call_cap: number;
  search_query_cap: number;
  username_guesses: boolean;
}

export interface StatusPayload {
  id: string;
  status: "created" | "running" | "paused" | "completed" | "limit_reached" | "cancelled" | "failed";
  phase: string;
  error: string | null;
  stop_reason: string;
  queued: number;
  running: number;
  runs: number;
  requests: number;
  elapsed: number;
  limits: Limits;
  max_depth: number;
  stats: Stats;
}

export interface InvestigationInfo extends StatusPayload {
  title: string;
  purpose: string;
  input: ParsedInput;
  options: Record<string, unknown>;
  info: { countries: string[]; target_names: string[]; locations: string[] };
  started_at: string;
  finished_at: string;
  target: string | null;
}

export interface Seed {
  type: string;
  value: string;
  label: string;
  note: string;
}

export interface ParsedInput {
  target_type: "person" | "organization" | "none";
  target_name: string | null;
  aliases: string[];
  birth_year: number | null;
  notes: string[];
  errors: string[];
  seeds: Seed[];
}

export interface PlanItem {
  name: string;
  title: string;
  category: string;
  category_title: string;
  status: string;
  status_title: string;
  note: string;
  auto: boolean;
  access: string;
  cost: string;
  sends: string;
  destinations: string[];
  mode: string;
  inputs: string[];
}

export interface SourceRef {
  id: string;
  connector: string;
  kind: string;
  url: string | null;
  title: string;
  retrieved_at: string;
  last_retrieved: string | null;
  reliability: string;
  license: string | null;
  request: string;
  transform: string;
  raw_refs?: string[];
}

export interface Reason {
  description: string;
  weight: number;
  feature: string;
  kind: "support" | "contradiction" | "info";
}

export interface PropValue {
  value: unknown;
  confidence: number;
  observations: { id: string; observed_at: string; evidence: string; source: SourceRef | null }[];
}

export interface EntityDetail {
  entity: EntitySummary & { type_title: string; first_seen: string; last_seen: string; resolvable: boolean };
  properties: { name: string; title: string; kind: string; local_only: boolean; values: PropValue[] }[];
  sightings: { evidence: string; observed_at: string; source: SourceRef | null }[];
  relationships: {
    id: string;
    type: string;
    title: string;
    direction: "in" | "out";
    other: EntitySummary;
    confidence: number;
    status: string;
    props: Record<string, unknown>;
    evidence: { description: string; confidence: number; observed_at: string; source: SourceRef | null }[];
  }[];
  sources: SourceRef[];
  assessment: { confidence: number; tier: Tier; label: string; support: Reason[]; contradictions: Reason[]; info: Reason[]; note: string };
  merged: { key: string; label: string; at: string; note: string }[];
  checks: Check[];
  links: { label: string; url: string }[];
}

export interface TransformOption {
  name: string;
  title: string;
  description: string;
  connector: string;
  connector_title: string;
  category: string;
  category_title: string;
  status: string;
  status_note: string;
  applicable: boolean;
  already_run: boolean;
  access: string;
  cost: string;
  sends: string;
  destinations: string[];
  input_types: string[];
  output_types: string[];
  auto: boolean;
}

export interface Check {
  connector: string;
  platform: string;
  platform_title: string;
  category: string;
  identifier_type: string;
  identifier: string;
  status: "found" | "not_found" | "blocked" | "unknown" | "skipped" | "error";
  detail: string;
  url: string | null;
  account_key: string | null;
  account?: EntitySummary | null;
}

export interface TransformRun {
  id: string;
  task_id: string;
  transform: string;
  connector: string;
  input_key: string;
  input_label: string;
  status: string;
  message: string;
  started_at: string;
  finished_at: string | null;
  requests: number;
  new_entities: number;
  new_relationships: number;
  sent: string;
  destinations: string[];
  depth: number;
  trigger: string;
}

export interface CoverageRow {
  connector: string;
  title: string;
  category: string;
  category_title: string;
  group: string;
  state: string;
  connector_status: string;
  note: string;
  runs: Record<string, number>;
  skips: Record<string, number>;
  queued: number;
  running: number;
  access: string;
  public_record: boolean;
}

export interface CoverageGroup {
  group: string;
  done: number;
  pending: number;
  total: number;
  found: number;
  queried_sources: number;
  unavailable_sources: number;
  progress: number;
}

export interface InvestigationGlobalCoverage {
  countries: { evaluated: number; with_sources: number; with_country_connectors: number; no_connector: number; note: string;
    applicable: { code: string; name: string; level: string; level_title: string; official_links: number; your_sources?: number; potential_sources: number;
      connectors: { connector: string; title: string; state: string; runs: Record<string, number> }[] }[] };
  platforms: { registered: number; evaluated: number; searched: number; found: number; unavailable: number; skipped: number;
    not_searched: number; username_available: boolean; note: string };
  public_databases: { queried: number; failed: number; unavailable: number; not_applicable: number; other: number;
    official_links_for_applicable_countries: number };
  commercial: { configured: number; queried: number; not_configured: number; catalog_only: number };
  disclaimer: string;
}

export interface Coverage {
  summary: Record<string, number>;
  groups: CoverageGroup[];
  connectors: CoverageRow[];
  disclaimer: string;
  global?: InvestigationGlobalCoverage;
}

export interface CoverageSummary {
  countries: { total: number; territories: number; with_country_connectors: number; links_only: number; discovered_only: number;
    none: number; levels: Record<string, number>; level_titles: Record<string, string>;
    by_region: { region: string; countries: number; connectors: number; links: number; discovered: number; none: number }[] };
  platforms: { registered: number; curated: number; imported: number; searchable: number; opt_in?: number; api_only: number; unavailable: number;
    regional_total: number; by_status: Record<string, number>; status_titles: Record<string, string> };
  connectors: { total: number; by_status: Record<string, number>; by_health: Record<string, number>; health_titles: Record<string, string> };
  public_databases: { official_sources: number; automated: number; links_only: number; connectors_available: number;
    connectors_unavailable: number; discovered: number; link_checks: Record<string, number>; links_checked_at: string | null };
  commercial: { configured: number; not_configured: number; catalog_only: number; providers_listed?: number;
    countries_with_providers?: number };
  custom_sources: number;
  disclaimer: string;
}

export interface CountryRow {
  code: string; name: string; region: string; subregion: string; status: string; sovereign: boolean;
  level: string; level_title: string; reason: string; connectors: number; connectors_available: number;
  curated_sources: number; regional_sources: number; discovered_sources: number; custom_sources: number; custom_active: number;
  sources: number; regional_platforms: number; regional_platforms_searchable: number; records: number;
  groups: Record<string, { curated: number; discovered: number; user: number }>;
  dns: { tld: string; rdap: boolean } | null; rir: string; global_capabilities: number; blocs: string[]; last_checked: string | null;
}

export interface CountrySource {
  id: string; title: string; kind: string; kind_title: string; url: string; group: string; group_title: string;
  access?: string; access_title?: string; name_search?: boolean; person_data?: string; api?: string; connector?: string;
  connector_note?: string; anonymized?: boolean; notes?: string; dataset?: string; dataset_title?: string; dataset_ref?: string;
  verification?: { result: string; checked_at?: string } | null; corroborates?: string; bloc?: string; bloc_title?: string;
  provenance?: string; regional?: string;
}

export interface CountryDetailT extends CountryRow {
  country: { status_title: string; languages: string[]; tld: string; fips: string; wikidata: string } | null;
  legal_notes: string; criminal_records_public: boolean; judgments_anonymized: boolean | null;
  country_connectors: { connector: string; title: string; category_title: string; status: string; access_title: string;
    health: string; health_title: string; last_verified: string | null; last_success: string | null }[];
  global_capabilities_detail: { connector: string; what: string; status: string | null }[];
  curated: CountrySource[]; discovered: CountrySource[]; potential_sources: CountrySource[]; bloc_sources: CountrySource[];
  custom: (UserSource & { status: string; status_title: string; automation: Automation; active: boolean;
    verification: { result: string; checked_at?: string } | null })[];
  platforms: { id: string; name: string; category_title: string; url: string; source: string; search_status: string;
    search_status_title: string }[];
  groups_vocabulary: Record<string, string>;
  kinds: Record<string, string>;
  access: Record<string, string>;
  live_discovery: { ran_at: string; wikidata_items: number; new_sources: { id: string; qid: string; kind: string; title: string; url: string }[];
    checks: Record<string, { result: string; checked_at?: string }>; counts: Record<string, number>; notes: string[] } | null;
}

export interface CountryPlan {
  code: string; name: string; why: string[]; level: string; level_title: string; official_links: number; your_sources?: number;
  potential_sources: number; regional_platforms: number;
  connectors: { connector: string; title: string; status: string; health_title: string }[];
}

export interface SourceHealth {
  status: string; title: string; note: string; last_verified: string | null; last_success: string | null;
  last_failure: string | null; runs: number; evidence: string;
}

export interface Credential {
  key: string;
  label: string;
  help: string;
  secret: boolean;
  required: boolean;
  obtain_url: string;
  is_set: boolean;
}

// ---------------------------------------------------------------- catalog: the investigator's own records

export interface CatalogCategory {
  id: string; title: string; group: string; group_title: string; origin: "system" | "user"; description?: string;
  system_sources: number; user_sources: number; progress_group: string;
}

export interface CatalogVocab {
  categories: CatalogCategory[];
  groups: Record<string, string>;
  source_types: Record<string, { title: string; help: string; automation: string }>;
  provider_classes: Record<string, string>;
  access: Record<string, string>;
  kinds: Record<string, string>;
  review: Record<string, string>;
  jurisdictions: { code: string; name: string }[];
  connectors: { name: string; title: string; category_title: string }[];
  platforms: { id: string; name: string; origin: string }[];
  platform_categories: Record<string, string>;
  service_statuses: string[];
  entity_types: { id: string; title: string }[];
  version: number;
}

export interface LinkCheck { result: string; status: number | null; checked_at: string; error: string | null }

export interface UserSource {
  id: string; origin: "user"; title: string; url: string; description: string | null; category: string; category_title: string;
  source_type: string; source_type_title: string; access: string; access_title: string; kind: string | null; kind_title: string;
  jurisdictions: string[]; platforms: string[]; platform_titles: Record<string, string>; connector: string;
  inputs: string[] | null; official: boolean | null; name_search: boolean | null; person_data: string | null; notes: string | null;
  enabled: boolean; review: string; review_title: string; link_check: LinkCheck | null; group: string; group_title: string;
  created_at: string; updated_at: string; provider_class?: string; provider_class_title?: string; pricing?: string | null;
  auth?: string | null; terms_url?: string | null; data_types?: string[] | null; access_models?: string[] | null;
  api_available?: string | null; web_app?: boolean | null; documentation_url?: string | null; coverage?: string | null;
  limitations?: string | null; last_verified?: string | null; access_class?: string; access_classes?: string[];
}

export type SourceDraft = Partial<Pick<UserSource, "title" | "url" | "category" | "source_type" | "access" | "kind" |
  "jurisdictions" | "platforms" | "connector" | "inputs" | "official" | "name_search" | "description" | "person_data" |
  "notes" | "review" | "provider_class" | "pricing" | "auth" | "terms_url" | "data_types" | "access_models" | "api_available" |
  "web_app" | "documentation_url" | "coverage" | "limitations" | "last_verified">>;

export interface Automation { mode: string; title: string; connector?: string; connector_title?: string; connector_status?: string }

export interface DuplicateMatch {
  type: string; id: string; title: string; url: string; origin: string; jurisdictions: string[]; reasons: string[];
  strength: "likely" | "possible"; editable: boolean; href: string;
}

export interface UserPlatform {
  id: string; name: string; url: string; host: string; category: string; profile_url: string; countries: string[];
  region: string; notes: string; status: string; public_profile: string;
}

export interface CommercialMeta {
  access_models?: string[]; api?: string; api_available?: string; web_app?: boolean; auth?: string; data_categories?: string[];
  data_types?: string[]; limitations?: string; last_verified?: string; documentation_url?: string; coverage?: string;
  pricing?: string; terms_url?: string;
}

export interface SourceManifest {
  origin?: "system" | "user";
  access_class?: string;
  access_class_title?: string;
  access_classes?: string[];
  access_class_titles?: string[];
  commercial?: CommercialMeta | null;
  record?: UserSource;
  automation?: Automation;
  transparency?: { requirement: string; search: string };
  health?: SourceHealth;
  name: string;
  title: string;
  category: string;
  category_title: string;
  progress_group: string;
  description: string;
  access: string;
  access_title: string;
  cost: string;
  rate_limit: { per_second: number; burst: number; note: string };
  jurisdictions: string[];
  supported_countries: string[];
  legal: string;
  public_record: boolean;
  official_api: boolean;
  access_method: string;
  website: string;
  documentation: string;
  terms_url: string;
  license: string;
  reliability: string;
  coverage: string;
  capabilities: string[];
  input_entities: string[];
  output_entities: string[];
  disclosure: { destinations: string[]; sends: string; mode: string; note: string };
  status: string;
  status_title: string;
  status_note: string;
  kind: string;
  default_enabled: boolean;
  auto_allowed: boolean;
  manual_only?: boolean;
  requires_api_key: boolean;
  requires_subscription: boolean;
  credentials: Credential[];
  transforms: { name: string; title: string; input_types: string[]; output_types: string[] }[];
  matrix: { free: boolean; api: boolean; paid: boolean; public: boolean };
  installed: boolean;
  install_note: string;
  max_calls_per_investigation: number | null;
  implemented: boolean;
}

export interface EntityTypeSpec {
  id: string;
  title: string;
  plural: string;
  family: string;
  group: string;
  shape: string;
  color: string;
  description: string;
  resolvable: boolean;
}

export interface Ontology {
  entity_types: EntityTypeSpec[];
  relation_types: { id: string; title: string; inverse: string; description: string; family: string }[];
}

export interface Preset extends Limits {
  id: string;
  title: string;
  description: string;
}

export interface EventItem {
  seq: number;
  kind: "entity" | "relationship" | "score" | "run" | "task" | "status" | "stats" | "graph_changed";
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}
