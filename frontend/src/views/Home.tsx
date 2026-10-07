import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AssistantPlan } from "../components/Assistant";
import { fmtMoney } from "../components/Copilot";
import type { Money } from "../components/Copilot";
import type { CountryPlan, ParsedInput, PlanItem, Preset } from "../types";
import { Badge, ErrorBox, SourceStatus, Spinner, TypeDot, navigate, useApi, useDebounced, useOntology } from "../ui";

interface ParseResult {
  input: ParsedInput;
  title: string;
  plan: (PlanItem & { opt_in?: boolean; preselected?: boolean })[];
  countries: CountryPlan[];
  plan_counts: Record<string, number>;
  disclaimer: string;
}
interface CountryOpt { code: string; name: string; region: string; sovereign: boolean }
interface Forecast {
  lines: { provider: string; title: string; category: string; requests: number; estimated: number; verified: boolean }[];
  total_estimated: number; unknown: string[]; money: Money; note: string;
  ai: { provider: string; model: string; per_message: number | null; assumption: string; price_known: boolean } | null;
}
interface PlanLite { forecast: Forecast; web_discovery_unavailable: string; default_budget_usd: number | null;
  search_providers: { name: string; title: string; status: string }[] }
const BUDGETS = ["0.10", "0.50", "1", "5", "unlimited", "custom"];
const CAT_TITLE: Record<string, string> = { search: "Search", api: "Platform & other APIs", commercial: "Commercial providers", ai: "AI" };

const EXAMPLES = ["John Michael Smith", "john@example.com", "+358 40 123 4567", "johnsmith123", "example.com",
  "Jane Doe, jdoe92, location: Oulu, Finland"];
const PLATFORM_GROUPS: [string, string][] = [
  ["social", "Social networks"], ["microblogging", "Microblogging"], ["messaging", "Messaging & channels"],
  ["forums", "Forums"], ["qa", "Q&A"], ["developer", "Developer"], ["packages", "Package registries"], ["gaming", "Gaming"],
  ["music", "Music"], ["podcasts", "Podcasts"], ["video", "Video"], ["photography", "Photography"], ["art", "Art & design"],
  ["writing", "Blogs & writing"], ["newsletters", "Newsletters"], ["professional", "Professional"], ["jobs", "Jobs"],
  ["freelance", "Freelance"], ["marketplaces", "Marketplaces"], ["creator", "Creator & donation"], ["crowdfunding", "Crowdfunding"],
  ["events", "Events"], ["academic", "Academic"], ["education", "Education"], ["fitness", "Fitness"], ["travel", "Travel"],
  ["books", "Books"], ["film", "Film & TV"], ["reviews", "Reviews"], ["identity", "Link pages / identity"],
  ["directories", "Directories"], ["reference", "Wikis"], ["genealogy", "Genealogy"], ["other", "Other"],
];
const SOURCE_GROUPS: [string, string[]][] = [
  ["Search engines", ["search"]],
  ["Social & platforms", ["social", "usernames", "forums", "developer_platforms", "gaming", "music", "video", "blogs",
    "marketplaces", "professional_networks", "adapters"]],
  ["Email, phone & breaches", ["email", "phone", "breach_metadata"]],
  ["Domains & infrastructure", ["domains", "dns", "rdap", "certificates", "infrastructure", "reputation",
    "technology_detection", "websites", "archives"]],
  ["Registries & public records", ["companies", "organizations", "public_records", "legal_records", "sanctions", "procurement"]],
  ["Publications, news & documents", ["publications", "news", "documents"]],
  ["Images & places", ["images", "geolocation"]],
  ["Commercial people search", ["people_search"]],
];

const MODE_TITLE: Record<string, string> = { "1": "Quick", "2": "Balanced", "3": "Deep", "4": "Deep +" };

export default function Home() {
  const [text, setText] = useState("");
  const [depth, setDepth] = useState("2");
  const [purpose, setPurpose] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [platformMode, setPlatformMode] = useState<"all" | "some">("all");
  const [platformCats, setPlatformCats] = useState<string[]>(PLATFORM_GROUPS.map((p) => p[0]));
  const [groupsOn, setGroupsOn] = useState<string[]>(SOURCE_GROUPS.map((g) => g[0]));
  const [respectRobots, setRespectRobots] = useState(true);
  const [adult, setAdult] = useState(false);
  const [autoPaid, setAutoPaid] = useState(false);
  const [custom, setCustom] = useState<Record<string, number>>({});
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [parsing, setParsing] = useState(false);
  const [showPlan, setShowPlan] = useState(false);
  const [showAssistant, setShowAssistant] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [countriesSel, setCountriesSel] = useState<string[]>([]);
  const [everything, setEverything] = useState(false);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [includePaid, setIncludePaid] = useState(true);
  const [budget, setBudget] = useState("1");
  const [customBudget, setCustomBudget] = useState("");
  const [askAbove, setAskAbove] = useState(false);
  const [askAboveValue, setAskAboveValue] = useState("1");
  const [planLite, setPlanLite] = useState<PlanLite | null>(null);
  const presets = useApi<Preset[]>("/api/depth-presets");
  const countryList = useApi<CountryOpt[]>("/api/countries");
  const o = useOntology();
  const debounced = useDebounced(text, 350);

  useEffect(() => {
    if (!debounced.trim()) {
      setParsed(null);
      return;
    }
    let live = true;
    setParsing(true);
    api.post<ParseResult>("/api/parse", { text: debounced, countries: countriesSel, search_everything: everything })
      .then((r) => live && setParsed(r))
      .catch((e) => live && setError((e as Error).message))
      .finally(() => live && setParsing(false));
    return () => {
      live = false;
    };
  }, [debounced, countriesSel, everything]);

  useEffect(() => {
    if (!debounced.trim()) {
      setPlanLite(null);
      return;
    }
    let live = true;
    const mode = depth === "1" ? "quick" : depth === "2" || depth === "custom" ? "balanced" : "deep";
    api.post<PlanLite>("/api/assistant/plan", { text: debounced, countries: countriesSel, mode })
      .then((r) => live && setPlanLite(r)).catch(() => live && setPlanLite(null));
    return () => {
      live = false;
    };
  }, [debounced, countriesSel, depth]);

  const money = planLite?.forecast.money;
  const toUsd = (v: string) => Number(v) / (money?.rate || 1);
  function budgetBody(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    if (budget === "unlimited") out.unlimited_budget = true;
    else if (budget === "custom") { if (customBudget) out.budget_usd = toUsd(customBudget); }
    else out.budget_usd = toUsd(budget);
    if (askAbove && askAboveValue) out.confirm_above_usd = toUsd(askAboveValue);
    return out;
  }
  const categories = useMemo(() => SOURCE_GROUPS.filter((g) => groupsOn.includes(g[0])).flatMap((g) => g[1]), [groupsOn]);
  const planSummary = useMemo(() => {
    const p = parsed?.plan ?? [];
    const sel = p.filter((x) => categories.includes(x.category));
    const count = (s: string) => sel.filter((x) => x.status === s).length;
    return { total: sel.length, ready: sel.filter((x) => x.status === "available" && x.auto).length, keys: count("needs_credentials"),
      paid: count("needs_subscription"), disabled: count("disabled") + count("not_installed"), sel };
  }, [parsed, categories]);
  const preset = presets.data?.find((p) => p.id === depth);
  const canGo = !!parsed && (parsed.input.seeds.length > 0 || !!parsed.input.target_name) && !busy;

  async function start(mode: "auto" | "manual", all = false) {
    setBusy(true);
    setError(null);
    try {
      const options: Record<string, unknown> = all ? {
        respect_robots: respectRobots, include_adult: adult, auto_paid: includePaid, connectors_disabled: excluded,
      } : {
        categories: groupsOn.length === SOURCE_GROUPS.length ? null : categories,
        platforms: platformMode === "all" ? null : platformCats,
        respect_robots: respectRobots, include_adult: adult, auto_paid: autoPaid,
      };
      const overrides = depth === "custom" ? custom : undefined;
      const d = all && (depth === "1" || depth === "2") ? "3" : depth;
      const r = await api.post<{ id: string }>("/api/investigations", { text, depth: d, overrides, options, purpose, mode,
        countries: countriesSel, search_everything: all, ...budgetBody() });
      navigate(`/inv/${r.id}/${mode === "auto" ? "assistant" : "graph"}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="hero">
      <h1>Who or what are you investigating?</h1>
      <p className="lead">
        Enter a name, email, phone number, username, URL or domain — or several. Sextant works out what it can derive,
        which sources apply, and expands the investigation step by step into an evidence graph. Every fact keeps its source.
      </p>
      <div className="bigbox">
        <textarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"John Michael Smith\njohn@example.com · +358 40 123 4567 · johnsmith123"}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && canGo) void start("auto");
          }}
        />
        <div className="row wrap" style={{ minHeight: 30, padding: "4px 4px 0" }}>
          {parsing && <Spinner />}
          {!text && (
            <span className="faint small">
              Try: {EXAMPLES.map((x) => (
                <button key={x} className="btn ghost sm" onClick={() => setText(x)}>{x}</button>
              ))}
            </span>
          )}
          {parsed && (
            <div className="chips">
              {parsed.input.target_name && (
                <span className="chip">
                  <TypeDot type={parsed.input.target_type === "organization" ? "organization" : "person"} />
                  <span className="t">{parsed.input.target_type === "organization" ? "Organization" : "Person"}</span>
                  {parsed.input.target_name}
                </span>
              )}
              {parsed.input.aliases.map((a) => (
                <span className="chip" key={a}><span className="t">alias</span>{a}</span>
              ))}
              {parsed.input.seeds.map((s) => (
                <span className="chip" key={`${s.type}:${s.value}`} title={s.note}>
                  <TypeDot type={s.type} />
                  <span className="t">{o.title(s.type)}</span>
                  {s.label}
                </span>
              ))}
              {parsed.input.birth_year && <span className="chip"><span className="t">born</span>{parsed.input.birth_year} · local only</span>}
            </div>
          )}
        </div>
        <div className="row wrap small" style={{ padding: "8px 4px 0", gap: 6 }}>
          <span className="muted">Countries:</span>
          {(parsed?.countries ?? []).map((c) => (
            <span key={c.code} className="chip" title={`${c.level_title} · ${c.why.join(", ")}`}>
              <span className="t">{c.code}</span>{c.name}
              {countriesSel.includes(c.code) && <button className="btn ghost sm" onClick={() => setCountriesSel(countriesSel.filter((x) => x !== c.code))}>✕</button>}
            </span>
          ))}
          {!(parsed?.countries ?? []).length && <span className="faint">none inferred — global sources only</span>}
          <select value="" onChange={(e) => e.target.value && setCountriesSel([...countriesSel, e.target.value])} style={{ maxWidth: 220 }}>
            <option value="">+ add a country…</option>
            {(countryList.data ?? []).filter((c) => !countriesSel.includes(c.code)).map((c) => (
              <option key={c.code} value={c.code}>{c.name}{c.sovereign ? "" : " (territory)"}</option>))}
          </select>
        </div>
        {parsed && [...parsed.input.errors, ...parsed.input.notes].length > 0 && (
          <div className="col small" style={{ padding: "8px 4px 0" }}>
            {parsed.input.errors.map((e) => <span key={e} style={{ color: "#ffb0b0" }}>⚠ {e}</span>)}
            {parsed.input.notes.map((n) => <span key={n} className="muted">ℹ {n}</span>)}
          </div>
        )}
      </div>

      <div style={{ marginTop: 22 }}>
        <div className="row between" style={{ marginBottom: 8 }}>
          <b>Investigation depth</b>
          <button className={`btn sm ${depth === "custom" ? "primary" : ""}`} onClick={() => setDepth("custom")}>Custom limits…</button>
        </div>
        <div className="depths">
          {(presets.data ?? []).map((p) => (
            <div key={p.id} className={`depth ${depth === p.id ? "active" : ""}`} onClick={() => setDepth(p.id)}>
              <b>{p.id} — {p.title}</b> <span className="faint small">{MODE_TITLE[p.id]}</span>
              <small>{p.description}</small>
            </div>
          ))}
        </div>
        {preset && depth !== "custom" && (
          <p className="small muted" style={{ marginTop: 8 }}>
            Expands up to {preset.max_depth} hop(s) · at most {preset.max_entities.toLocaleString()} entities ·
            {" "}{preset.max_requests.toLocaleString()} requests · {preset.max_minutes} min · per-source rate limits always apply.
          </p>
        )}
        {depth === "custom" && (
          <div className="grid3 card pad" style={{ marginTop: 10 }}>
            {([["max_depth", "Max hops", 1, 8], ["expand_threshold", "Expansion threshold (0–1)", 0, 1],
              ["max_entities", "Max entities", 50, 20000], ["max_requests", "Max requests", 100, 60000],
              ["max_minutes", "Max minutes", 1, 240], ["connector_call_cap", "Calls per source", 1, 1000]] as const).map(
              ([k, label, min, max]) => (
                <label key={k} className="field">
                  {label}
                  <input type="number" min={min} max={max} step={k === "expand_threshold" ? 0.05 : 1}
                    placeholder={String((presets.data?.[3] as unknown as Record<string, number>)?.[k] ?? "")}
                    value={custom[k] ?? ""} onChange={(e) => setCustom({ ...custom, [k]: Number(e.target.value) })} />
                </label>
              ))}
          </div>
        )}
      </div>

      <div style={{ marginTop: 18 }}>
        <button className="btn ghost" onClick={() => setAdvanced(!advanced)}>{advanced ? "▾" : "▸"} Sources, platforms & purpose</button>
        {advanced && (
          <div className="grid2" style={{ marginTop: 8 }}>
            <div className="card pad col">
              <b>Source categories</b>
              {SOURCE_GROUPS.map(([g]) => (
                <label key={g} className="check">
                  <input type="checkbox" checked={groupsOn.includes(g)}
                    onChange={(e) => setGroupsOn(e.target.checked ? [...groupsOn, g] : groupsOn.filter((x) => x !== g))} />
                  {g}
                </label>
              ))}
              <div className="hr" />
              <label className="check"><input type="checkbox" checked={respectRobots} onChange={(e) => setRespectRobots(e.target.checked)} />Respect robots.txt (recommended)</label>
              <label className="check"><input type="checkbox" checked={adult} onChange={(e) => setAdult(e.target.checked)} />Include adult & dating sites in username checks</label>
              <label className="check"><input type="checkbox" checked={autoPaid} onChange={(e) => setAutoPaid(e.target.checked)} />Allow configured paid/metered sources in auto mode (caps apply)</label>
            </div>
            <div className="card pad col">
              <b>Social platforms</b>
              <label className="check"><input type="radio" checked={platformMode === "all"} onChange={() => setPlatformMode("all")} />Search all available platforms</label>
              <label className="check"><input type="radio" checked={platformMode === "some"} onChange={() => setPlatformMode("some")} />Only these categories:</label>
              <div className="grid2" style={{ gridTemplateColumns: "1fr 1fr", gap: 4, opacity: platformMode === "all" ? 0.45 : 1 }}>
                {PLATFORM_GROUPS.map(([id, label]) => (
                  <label key={id} className="check small">
                    <input type="checkbox" disabled={platformMode === "all"} checked={platformCats.includes(id)}
                      onChange={(e) => setPlatformCats(e.target.checked ? [...platformCats, id] : platformCats.filter((x) => x !== id))} />
                    {label}
                  </label>
                ))}
              </div>
              <div className="hr" />
              <label className="field">
                Purpose / legal basis (optional — recorded in the report and audit log)
                <textarea rows={3} value={purpose} onChange={(e) => setPurpose(e.target.value)}
                  placeholder="e.g. Due-diligence on a prospective business partner; consent of the subject; journalistic research…" />
              </label>
            </div>
          </div>
        )}
      </div>

      {parsed && (
        <div className="card pad" style={{ marginTop: 18 }}>
          <div className="row between wrap">
            <div className="row wrap">
              <Badge kind="good">{planSummary.ready} sources ready</Badge>
              {planSummary.keys > 0 && <Badge kind="warn">{planSummary.keys} need an API key</Badge>}
              {planSummary.paid > 0 && <Badge kind="violet">{planSummary.paid} need a subscription</Badge>}
              {planSummary.disabled > 0 && <Badge>{planSummary.disabled} disabled / not installed</Badge>}
              <span className="small muted">for the identifiers above; more apply as the graph grows</span>
            </div>
            <button className="btn sm" onClick={() => setShowPlan(!showPlan)}>{showPlan ? "Hide" : "Show"} what each source receives</button>
          </div>
          {showPlan && (
            <div style={{ maxHeight: 340, overflow: "auto", marginTop: 10 }}>
              <table className="data">
                <thead><tr><th>Source</th><th>Status</th><th>Receives</th><th>Destination</th></tr></thead>
                <tbody>
                  {planSummary.sel.map((p) => (
                    <tr key={p.name}>
                      <td>{p.title}<div className="small faint">{p.category_title}</div></td>
                      <td><SourceStatus status={p.status} />{p.status === "available" && !p.auto && <div className="small faint">manual / opt-in</div>}</td>
                      <td className="small">{p.sends}{p.mode === "hashed" && " (hashed)"}{p.mode === "none" && " — nothing leaves this computer"}</td>
                      <td className="small muted">{p.destinations.join(", ") || "local"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {parsed && planLite && (
        <div className="card pad col" style={{ marginTop: 18 }}>
          <div className="row between wrap">
            <b>Cost forecast & budget</b>
            <span className="small faint">{planLite.forecast.money.note}</span>
          </div>
          {planLite.web_discovery_unavailable && <div className="notice warn small">{planLite.web_discovery_unavailable}</div>}
          <table className="data small">
            <tbody>
              {planLite.forecast.lines.map((l) => (
                <tr key={l.provider}><td>{CAT_TITLE[l.category] ?? l.category}</td><td>{l.title}</td><td>up to {l.requests} request(s)</td>
                  <td>~{fmtMoney(l.estimated, money)}{!l.verified && <span className="faint"> (price not verified)</span>}</td></tr>))}
              {planLite.forecast.ai && <tr><td>AI</td><td>{planLite.forecast.ai.provider} / {planLite.forecast.ai.model}</td>
                <td className="faint">{planLite.forecast.ai.assumption}</td>
                <td>{planLite.forecast.ai.per_message !== null ? `~${fmtMoney(planLite.forecast.ai.per_message, money)} per chat message` : "Unknown (no price)"}</td></tr>}
              <tr><td><b>Estimated total</b></td><td colSpan={2} className="faint">automatic sources at this depth{planLite.forecast.unknown.length
                ? ` · no price for ${planLite.forecast.unknown.join(", ")}` : ""}</td><td><b>~{fmtMoney(planLite.forecast.total_estimated, money)}</b></td></tr>
            </tbody>
          </table>
          <div className="row wrap" style={{ gap: 6 }}>
            <span className="small">Maximum cost for this investigation:</span>
            {BUDGETS.map((b) => (
              <button key={b} className={`btn sm ${budget === b ? "primary" : ""}`} onClick={() => setBudget(b)}>
                {b === "unlimited" ? "Unlimited" : b === "custom" ? "Custom…" : fmtMoney(toUsd(b), money)}</button>))}
            {budget === "custom" && <input type="number" min={0} step={0.1} value={customBudget} placeholder={money?.currency ?? ""}
              onChange={(e) => setCustomBudget(e.target.value)} style={{ maxWidth: 110 }} />}
          </div>
          <label className="check small"><input type="checkbox" checked={askAbove} onChange={(e) => setAskAbove(e.target.checked)} />
            Ask before exceeding <input type="number" min={0} step={0.1} value={askAboveValue} disabled={!askAbove}
              onChange={(e) => setAskAboveValue(e.target.value)} style={{ maxWidth: 80, margin: "0 4px" }} />{money?.currency}</label>
          <span className="small faint">{planLite.forecast.note} When the next billable step would pass the limit, it stops and asks you.</span>
        </div>
      )}

      {parsed && (
        <div className="card pad" style={{ marginTop: 18 }}>
          <div className="row between wrap">
            <span><b>Assistant plan</b> <span className="small muted">which sources, why, in what order, at what cost: before anything runs</span></span>
            <button className="btn sm" onClick={() => setShowAssistant(!showAssistant)}>{showAssistant ? "Hide" : "Show"} plan</button>
          </div>
          {showAssistant && <div style={{ marginTop: 10 }}><AssistantPlan text={text} countries={countriesSel} depth={depth} /></div>}
        </div>
      )}

      {everything && parsed && (
        <div className="card pad" style={{ marginTop: 18, borderColor: "var(--accent)" }}>
          <div className="row between wrap">
            <b style={{ letterSpacing: ".06em" }}>SEARCH EVERYTHING — plan</b>
            <button className="btn ghost sm" onClick={() => setEverything(false)}>✕ Close</button>
          </div>
          <ol className="small" style={{ margin: "8px 0", paddingLeft: 18 }}>
            <li><b>Countries:</b> {parsed.countries.length ? parsed.countries.map((c) => `${c.name} (${c.level_title.toLowerCase()}; ${c.connectors.length} country connector(s), ${c.official_links} official links${c.your_sources ? `, ${c.your_sources} of your sources` : ""}, ${c.regional_platforms} regional platforms)`).join("; ") : "none inferred — add one above to include its country-specific sources"}.</li>
            <li><b>Source categories:</b> all of them.</li>
            <li><b>Connectors ready:</b> {parsed.plan_counts.available} (including opt-in sources switched on for this run) · <b>API key missing:</b> {parsed.plan_counts.needs_credentials} · <b>subscription missing:</b> {parsed.plan_counts.needs_subscription} · <b>unavailable:</b> {parsed.plan_counts.unavailable}.</li>
            <li><b>Platforms:</b> every registered platform with an automated check (username sweep, public API probes, adapters if installed); the others are listed as links.</li>
            <li><b>Depth:</b> {depth === "1" || depth === "2" ? "3 (deep correlation)" : depth} — the queue keeps expanding as new entities appear, within the limits.</li>
          </ol>
          {parsed.plan.some((p) => p.opt_in) && (
            <div className="small" style={{ marginBottom: 8 }}>
              <b>Opt-in sources this run switches on</b> (untick to leave one off):
              <div className="row wrap" style={{ gap: 10, marginTop: 4 }}>
                {parsed.plan.filter((p) => p.opt_in).map((p) => (
                  <label key={p.name} className="check" title={p.note}>
                    <input type="checkbox" checked={!excluded.includes(p.name)}
                      onChange={(e) => setExcluded(e.target.checked ? excluded.filter((x) => x !== p.name) : [...excluded, p.name])} />
                    {p.title}
                  </label>
                ))}
              </div>
            </div>
          )}
          {parsed.plan_counts.paid_configured > 0 && (
            <label className="check small"><input type="checkbox" checked={includePaid} onChange={(e) => setIncludePaid(e.target.checked)} />
              Include the {parsed.plan_counts.paid_configured} paid/metered source(s) you configured (charges apply to your account; per-investigation caps still apply)</label>
          )}
          <div className="notice small" style={{ marginTop: 8 }}>{parsed.disclaimer} Private accounts, private messages and login-only content are never accessed.</div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn primary lg" disabled={!canGo} onClick={() => void start("auto", true)}>{busy ? <Spinner /> : "Start SEARCH EVERYTHING"}</button>
          </div>
        </div>
      )}

      <ErrorBox error={error} />
      <div className="row wrap" style={{ marginTop: 22, gap: 14 }}>
        <button className="btn primary xl" disabled={!canGo} onClick={() => void start("auto")}>
          {busy ? <Spinner /> : "INVESTIGATE"}
        </button>
        <button className="btn good lg" disabled={!canGo} onClick={() => setEverything(true)}
          title="Plan a run with every applicable country, category, connector and platform">
          SEARCH EVERYTHING
        </button>
        <button className="btn lg" disabled={!canGo} onClick={() => void start("manual")} title="Create the graph with your input only and choose transforms yourself">
          Manual / transform mode
        </button>
        <span className="small muted" style={{ maxWidth: 360 }}>
          Everything is stored encrypted on this computer. External sources only receive what is listed above, and only
          when their transform runs.
        </span>
      </div>
    </div>
  );
}
