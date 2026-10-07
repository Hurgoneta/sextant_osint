import { useMemo, useState } from "react";
import { api } from "../api";
import type { SourceDraft, SourceManifest } from "../types";
import type { SourceHealth } from "../types";
import { Badge, ErrorBox, Ext, SourceStatus, Spinner, fmtDate, navigate, useApi, useOntology } from "../ui";
import { AutomationBadge, CategoryManager, LinkCheckBadge, OriginBadge, ResetCatalogDialog, SourceForm, UserSourceActions } from "../components/Catalog";
import { DiscoverSources } from "../components/SourceDiscovery";

const HEALTH_KIND: Record<string, string> = { operational: "good", degraded: "warn", auth_required: "warn", subscription_required: "violet",
  temporarily_unavailable: "warn", deprecated: "bad", broken: "bad", not_publicly_searchable: "", country_restricted: "warn",
  excluded: "bad", unknown: "" };

export function HealthBadge({ h }: { h: SourceHealth }) {
  return <Badge kind={HEALTH_KIND[h.status] ?? ""} title={h.note}>{h.title}</Badge>;
}

interface Payload {
  groups: { category: string; title: string; group: string; sources: SourceManifest[] }[];
  counts: Record<string, number>;
  total: number;
  origins: { system: number; user: number };
  access_classes?: Record<string, string>;
  access_class_counts?: Record<string, number>;
}
const CLASS_KIND: Record<string, string> = { FREE_PUBLIC: "good", FREE_API: "good", FREE_TIER: "info", PAID_API: "violet", SUBSCRIPTION: "violet",
  ENTERPRISE: "violet", MANUAL: "", REFERENCE_ONLY: "", UNAVAILABLE: "bad", NOT_VERIFIED: "warn" };

const FILTERS: [string, string][] = [["", "All"], ["available", "Available"], ["needs_credentials", "API key required"],
  ["needs_subscription", "Subscription required"], ["disabled", "Disabled"], ["not_installed", "Not installed"],
  ["catalog_only", "Contact provider"], ["manual", "Manual"], ["excluded", "Excluded"]];

type FormState = { mode: "source" | "link"; initial?: SourceDraft | null; editId?: string } | null;

export default function Sources({ focus }: { focus?: string }) {
  const { data, error, reload, setData } = useApi<Payload>("/api/sources");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [origin, setOrigin] = useState("");
  const [category, setCategory] = useState("");
  const [juris, setJuris] = useState("");
  const [aclass, setAclass] = useState("");
  const [discover, setDiscover] = useState(false);
  const [form, setForm] = useState<FormState>(null);
  const [cats, setCats] = useState(false);
  const [reset, setReset] = useState(false);
  const open = focus ?? null;
  const all = useMemo(() => (data?.groups ?? []).flatMap((g) => g.sources), [data]);
  const jurisdictions = useMemo(() => [...new Set(all.flatMap((s) => s.jurisdictions))].sort(), [all]);
  const current = all.find((s) => s.name === open);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;

  function replace(updated: SourceManifest) {
    if (!data) return;
    setData({ ...data, groups: data.groups.map((g) => ({ ...g, sources: g.sources.map((s) => (s.name === updated.name ? updated : s)) })) });
  }
  const jq = juris.trim().toUpperCase();
  const match = (s: SourceManifest) => (!status || s.status === status) && (!origin || (s.origin ?? "system") === origin) &&
    (!aclass || (s.access_classes ?? []).includes(aclass)) &&
    (!category || s.category === category) && (!jq || s.jurisdictions.includes(jq)) &&
    (!q || `${s.title} ${s.description} ${s.capabilities.join(" ")} ${s.category_title} ${s.website} ${s.jurisdictions.join(" ")}`
      .toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="page wide">
      <div className="row between wrap">
        <h1>Data sources</h1>
        <div className="row">
          <button className="btn primary" onClick={() => setForm({ mode: "source" })}>＋ Add source</button>
          <button className="btn" onClick={() => setForm({ mode: "link" })} title="A website to remember: not automated">＋ Add link</button>
          <button className="btn" onClick={() => setDiscover(!discover)} title="Find candidate sources with your web-search provider">Discover sources…</button>
          <button className="btn" onClick={() => setCats(true)}>Categories…</button>
          <button className="btn ghost" onClick={() => setReset(true)} title="The one catalog reset (also in Settings)">Reset catalog…</button>
        </div>
      </div>
      <p className="sub">
        {data.origins.system} built-in connectors{data.origins.user ? ` and ${data.origins.user} source(s) you added` : ""}. The software is free; open
        and public sources are free; some APIs need a free key; commercial sources work only with your own subscription; restricted sources are not
        available. Nothing is bypassed. Sources you add are information for you: Sextant never queries them unless a connector of this installation does.
      </p>
      <div className="row wrap" style={{ marginBottom: 10 }}>
        <input type="search" placeholder="Search sources, capabilities, addresses…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 300 }} />
        <select value={origin} onChange={(e) => setOrigin(e.target.value)} style={{ maxWidth: 170 }}>
          <option value="">Built-in + yours</option><option value="system">Built-in only</option><option value="user">Yours only ({data.origins.user})</option>
        </select>
        <select value={category} onChange={(e) => setCategory(e.target.value)} style={{ maxWidth: 240 }}>
          <option value="">All categories</option>
          {data.groups.map((g) => <option key={g.category} value={g.category}>{g.title} ({g.sources.length})</option>)}
        </select>
        <input type="search" list="src-juris" placeholder="Jurisdiction (e.g. SE)" value={juris} onChange={(e) => setJuris(e.target.value)} style={{ maxWidth: 170 }} />
        <datalist id="src-juris">{jurisdictions.map((j) => <option key={j} value={j} />)}</datalist>
      </div>
      {discover && <DiscoverSources onClose={() => setDiscover(false)} onAdd={(d) => setForm({ mode: "source", initial: d })} />}
      <div className="row wrap" style={{ marginBottom: 8, gap: 4 }}>
        <span className="small muted">Access:</span>
        <button className={`btn sm ${aclass === "" ? "primary" : ""}`} onClick={() => setAclass("")}>Any</button>
        {Object.entries(data.access_classes ?? {}).map(([k, v]) => (
          <button key={k} className={`btn sm ${aclass === k ? "primary" : ""}`} onClick={() => setAclass(k)}>
            {v}{data.access_class_counts?.[k] ? ` · ${data.access_class_counts[k]}` : ""}</button>))}
      </div>
      <div className="row wrap" style={{ marginBottom: 16 }}>
        {FILTERS.map(([k, label]) => (
          <button key={k} className={`btn sm ${status === k ? "primary" : ""}`} onClick={() => setStatus(k)}>
            {label}{k && data.counts[k] ? ` · ${data.counts[k]}` : !k ? ` · ${data.total}` : ""}
          </button>
        ))}
      </div>
      {data.groups.map((g) => {
        const list = g.sources.filter(match);
        if (!list.length) return null;
        return (
          <div key={g.category} style={{ marginBottom: 22 }}>
            <h3 style={{ margin: "0 0 8px" }}>{g.title} <span className="faint small">{list.length}</span></h3>
            <div className="grid3">
              {list.map((s) => (
                <div key={s.name} className={`src-card${s.status === "disabled" && s.origin === "user" ? " inactive" : ""}`}
                  onClick={() => navigate(`/sources/${s.name}`)}>
                  <div className="row between">
                    <b>{s.title}</b>
                    <span className="row" style={{ gap: 4 }}><OriginBadge origin={s.origin} /><SourceStatus status={s.status} /></span>
                  </div>
                  {s.health && <div className="row wrap small" style={{ gap: 6 }}><HealthBadge h={s.health} />
                    <span className="faint">{s.origin === "user" ? (s.health.last_verified ? `checked ${s.health.last_verified.slice(0, 10)}` : "never checked")
                      : s.health.last_success ? `last success ${s.health.last_success.slice(0, 10)}` : "no verified query yet"}</span></div>}
                  <span className="small muted" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{s.description}</span>
                  {s.transparency && <div className="row wrap small" style={{ gap: 6 }}>
                    <Badge kind={s.transparency.requirement === "Free" ? "good" : s.transparency.requirement === "Unavailable" ? "bad"
                      : s.transparency.requirement.startsWith("Subscription") ? "violet" : "warn"}>{s.transparency.requirement}</Badge>
                    <Badge kind={s.transparency.search === "Publicly searchable" || s.transparency.search === "Searchable by name" ? "info" : ""}>{s.transparency.search}</Badge>
                  </div>}
                  <div className="row wrap small faint" style={{ gap: 6 }}>
                    {s.origin === "user" ? <><span>{s.record?.source_type_title}</span><AutomationBadge a={s.automation} quiet />
                      {s.jurisdictions.length > 0 && <span>{s.jurisdictions.join(", ")}</span>}</> : <span>{s.access_title}</span>}
                    {s.public_record && <Badge kind="info">official / public record</Badge>}
                    {s.access_class && <Badge kind={CLASS_KIND[s.access_class] ?? ""}>{s.access_class_title}</Badge>}
                    {s.kind === "adapter" && <Badge>adapter</Badge>}
                    {s.status === "available" && !s.auto_allowed && s.origin !== "user" && <Badge>manual / opt-in in auto mode</Badge>}
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}
      {current && current.origin === "user" && current.record && (
        <UserSourceDrawer s={current} onClose={() => navigate("/sources")} onReload={() => void reload()}
          onEdit={() => setForm({ mode: "source", initial: current.record, editId: current.name })} />
      )}
      {current && current.origin !== "user" && <SourceDrawer s={current} onClose={() => navigate("/sources")} onChange={replace} onReload={reload} />}
      {form && <SourceForm mode={form.mode} initial={form.initial} editId={form.editId} onClose={() => setForm(null)}
        onSaved={(s) => { setForm(null); void reload(); navigate(`/sources/${s.id}`); }} />}
      {cats && <CategoryManager onClose={() => setCats(false)} onChanged={() => void reload()} />}
      {reset && <ResetCatalogDialog onClose={() => setReset(false)} onDone={() => { navigate("/sources"); void reload(); }} />}
    </div>
  );
}

function UserSourceDrawer({ s, onClose, onReload, onEdit }: { s: SourceManifest; onClose: () => void; onReload: () => void; onEdit: () => void }) {
  const o = useOntology();
  const r = s.record!;
  const [err, setErr] = useState<string | null>(null);
  async function review(value: string) {
    try {
      await api.patch(`/api/catalog/sources/${r.id}`, { review: value });
      onReload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <>
      <div className="drawer-bg" onClick={onClose} />
      <div className="drawer">
        <div className="row between">
          <span className="small muted">{s.category_title} · {r.source_type_title}</span>
          <button className="btn ghost sm" onClick={onClose}>✕</button>
        </div>
        <h2 style={{ margin: "6px 0" }}>{r.title}</h2>
        <div className="row wrap"><OriginBadge origin="user" /><SourceStatus status={s.status} /><AutomationBadge a={s.automation} quiet />
          {r.official && <Badge kind="info">official / public record</Badge>}</div>
        <p className="small muted">{s.automation?.title}. Added by you on {fmtDate(r.created_at)}; it stays until you delete it or reset the catalog.</p>
        <div className="card pad col" style={{ marginBottom: 14 }}>
          <UserSourceActions s={r} onEdit={onEdit} onChanged={onReload} />
          <label className="field" style={{ maxWidth: 280 }}>Status
            <select value={r.review} onChange={(e) => void review(e.target.value)}>
              <option value="not_verified">Not verified</option><option value="verified">Verified by you</option>
              <option value="unavailable">Marked unavailable</option><option value="excluded">Excluded (your policy decision)</option>
            </select></label>
          <ErrorBox error={err} />
        </div>
        {r.description && <p>{r.description}</p>}
        <div className="kv">
          <span className="k">Address</span><span><Ext href={r.url} /></span>
          <span className="k">Link check</span><span><LinkCheckBadge v={r.link_check} /> {r.link_check?.status ? `HTTP ${r.link_check.status}` : ""}
            {r.link_check?.error && <span className="small faint"> {r.link_check.error}</span>}</span>
          {s.health && <><span className="k">Health</span><span><HealthBadge h={s.health} /> <span className="small muted">{s.health.note}</span></span></>}
          <span className="k">Jurisdictions</span><span>{r.jurisdictions.length ? r.jurisdictions.map((c) => (
            <a key={c} href={c.length === 2 ? `#/coverage/${c}` : "#/jurisdictions"} style={{ marginRight: 6 }}>{c}</a>)) : "Global"}</span>
          <span className="k">Category</span><span>{r.category_title} <span className="faint small">(jurisdiction views: {r.group_title})</span></span>
          {r.kind_title && <><span className="k">Official source type</span><span>{r.kind_title}</span></>}
          <span className="k">Access</span><span>{r.access_title}</span>
          <span className="k">Automation</span><span>{s.automation?.title}{r.connector && <> · <a href={`#/sources/${r.connector}`}>open connector</a></>}</span>
          <span className="k">Platforms</span><span>{r.platforms.length ? r.platforms.map((p) => r.platform_titles[p] ?? p).join(", ") : "—"}</span>
          <span className="k">Can be searched by</span><span>{(r.inputs ?? []).map((t) => o.title(t)).join(", ") || "—"}</span>
          <span className="k">Name search</span><span>{r.name_search ? "anyone can search it by a person's name" : "no / unknown"}</span>
          {r.person_data && <><span className="k">Personal data</span><span>{r.person_data}</span></>}
          <span className="k">Receives</span><span>{s.disclosure.sends}</span>
          {r.notes && <><span className="k">Notes</span><span className="wrapword">{r.notes}</span></>}
          <span className="k">Last changed</span><span>{fmtDate(r.updated_at)}</span>
        </div>
      </div>
    </>
  );
}

function SourceDrawer({ s, onClose, onChange, onReload }: { s: SourceManifest; onClose: () => void; onChange: (s: SourceManifest) => void;
  onReload: () => void }) {
  const o = useOntology();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save(clear?: string) {
    setBusy(true);
    setError(null);
    try {
      const body = clear ? { [clear]: null } : Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
      onChange(await api.put<SourceManifest>(`/api/sources/${s.name}/credentials`, { values: body }));
      setValues({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function prefs(p: { enabled?: boolean; auto_run?: boolean; reset?: boolean; manual_only?: boolean }) {
    try {
      onChange(await api.put<SourceManifest>(`/api/sources/${s.name}`, p));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const canConfigure = s.credentials.length > 0;
  return (
    <>
      <div className="drawer-bg" onClick={() => { onClose(); onReload(); }} />
      <div className="drawer">
        <div className="row between">
          <span className="small muted">{s.category_title} · {s.kind}</span>
          <button className="btn ghost sm" onClick={onClose}>✕</button>
        </div>
        <h2 style={{ margin: "6px 0" }}>{s.title}</h2>
        <div className="row wrap"><SourceStatus status={s.status} /><Badge>{s.access_title}</Badge>{s.public_record && <Badge kind="info">official source</Badge>}</div>
        {s.status_note && <p className="small" style={{ color: "var(--warn)" }}>{s.status_note}</p>}
        {(s.access_class_titles ?? []).length > 0 && <div className="row wrap small" style={{ gap: 4, marginTop: 6 }}>
          {(s.access_classes ?? []).map((c, i) => <Badge key={c} kind={CLASS_KIND[c] ?? ""}>{(s.access_class_titles ?? [])[i]}</Badge>)}</div>}
        {s.commercial && (
          <div className="kv" style={{ margin: "10px 0" }}>
            <span className="k">Access model</span><span>{(s.commercial.access_models ?? []).join(", ") || "—"}</span>
            <span className="k">API</span><span>{s.commercial.api ?? s.commercial.api_available ?? "unknown"}</span>
            <span className="k">Web application</span><span>{s.commercial.web_app ? "yes" : "no / unknown"}</span>
            <span className="k">Data categories</span><span>{(s.commercial.data_categories ?? s.commercial.data_types ?? []).join(", ") || "—"}
              <span className="faint small"> (as the provider describes them)</span></span>
            <span className="k">Known limitations</span><span>{s.commercial.limitations || "—"}</span>
            <span className="k">Last verification</span><span>{s.commercial.last_verified || <Badge kind="warn">Not verified</Badge>}</span>
            <span className="k">Connector</span><span>{s.status === "available" ? "Configured" : "Not configured — Sextant cannot query it"}</span>
          </div>
        )}
        {s.health && (
          <div className="kv" style={{ margin: "10px 0" }}>
            <span className="k">Status</span><span><HealthBadge h={s.health} /> {s.health.note && <span className="small muted">{s.health.note}</span>}</span>
            <span className="k">Last verified</span><span>{fmtDate(s.health.last_verified)}{s.health.evidence ? ` (${s.health.evidence})` : ""}</span>
            <span className="k">Last successful query</span><span>{fmtDate(s.health.last_success)}</span>
            <span className="k">Last failure</span><span>{fmtDate(s.health.last_failure)}</span>
            <span className="k">Runs recorded here</span><span>{s.health.runs}</span>
          </div>
        )}
        <p>{s.description}</p>

        {s.capabilities.length > 0 && (
          <div className="card pad" style={{ marginBottom: 14 }}>
            <b>This data source can provide:</b>
            <ul style={{ margin: "6px 0 0", paddingLeft: 20 }}>{s.capabilities.map((c) => <li key={c}>{c}</li>)}</ul>
          </div>
        )}

        {(canConfigure || s.status === "needs_subscription" || s.status === "needs_credentials") && (
          <div className="card pad" style={{ marginBottom: 14 }}>
            <b>{s.requires_subscription ? "Subscription / API key" : "API key / configuration"}</b>
            <p className="small muted" style={{ margin: "4px 0 10px" }}>
              Stored encrypted on this computer (or set as environment variables). Keys are never shown again and are only sent
              to {s.disclosure.destinations.join(", ") || "the provider"}.
            </p>
            {s.credentials.map((c) => (
              <label key={c.key} className="field" style={{ marginBottom: 10 }}>
                <span>{c.label} {c.required ? "" : <span className="faint">(optional)</span>} {c.is_set && <Badge kind="good">set</Badge>}</span>
                <div className="row">
                  <input type={c.secret ? "password" : "text"} autoComplete="off" placeholder={c.is_set ? "•••••••• (leave empty to keep)" : c.key}
                    value={values[c.key] ?? ""} onChange={(e) => setValues({ ...values, [c.key]: e.target.value })} />
                  {c.is_set && <button className="btn sm" onClick={() => void save(c.key)}>Remove</button>}
                </div>
                <span className="small faint">{c.help} {c.obtain_url && <Ext href={c.obtain_url}>Get one ↗</Ext>}</span>
              </label>
            ))}
            <div className="row">
              <button className="btn primary" disabled={busy || !Object.values(values).some((v) => v.trim())} onClick={() => void save()}>Configure</button>
              {s.website && <Ext href={s.website}><span className="btn">Open provider ↗</span></Ext>}
            </div>
            <ErrorBox error={error} />
          </div>
        )}

        {s.manual_only && (
          <div className="notice warn" style={{ marginBottom: 14 }}>You marked this source manual-only (it asked for verification). It is not
            queried automatically. <button className="btn sm" onClick={() => void prefs({ manual_only: false })}>Automate it again</button></div>
        )}
        <TestConnection name={s.name} />
        {s.implemented !== false && s.kind !== "catalog" && !["excluded", "retired", "catalog_only", "manual"].includes(s.status) && (
          <div className="card pad col" style={{ marginBottom: 14 }}>
            <b>Use</b>
            <label className="check"><input type="checkbox" checked={s.status !== "disabled"}
              onChange={(e) => void prefs({ enabled: e.target.checked })} />Enabled</label>
            <label className="check"><input type="checkbox" checked={s.auto_allowed}
              onChange={(e) => void prefs({ auto_run: e.target.checked })} />May run automatically in auto-investigation</label>
            <button className="btn sm ghost" style={{ alignSelf: "flex-start" }} onClick={() => void prefs({ manual_only: true })}
              title="Stop automatic queries (e.g. after a CAPTCHA); you can undo it here">Mark manual-only</button>
            <button className="btn sm ghost" style={{ alignSelf: "flex-start" }} onClick={() => void prefs({ reset: true })}
              title="Only this source's two switches. The whole catalog is reset under Settings.">Restore default switches</button>
          </div>
        )}

        <div className="kv">
          <span className="k">Receives</span><span>{s.disclosure.sends} {s.disclosure.mode !== "plaintext" && <Badge>{s.disclosure.mode}</Badge>}
            {s.disclosure.note && <div className="small faint">{s.disclosure.note}</div>}</span>
          <span className="k">Destinations</span><span>{s.disclosure.destinations.join(", ") || "none (local)"}</span>
          <span className="k">Input entities</span><span>{s.input_entities.map((t) => o.title(t)).join(", ") || "—"}</span>
          <span className="k">Output entities</span><span>{s.output_entities.map((t) => o.title(t)).join(", ") || "—"}</span>
          <span className="k">Requires API key</span><span>{s.requires_api_key ? "yes" : "no"}</span>
          <span className="k">Requires subscription</span><span>{s.requires_subscription ? "yes" : "no"}</span>
          <span className="k">Cost</span><span>{s.cost}</span>
          <span className="k">Rate limit</span><span>{s.rate_limit.per_second}/s (burst {s.rate_limit.burst}){s.rate_limit.note ? ` — ${s.rate_limit.note}` : ""}</span>
          {s.max_calls_per_investigation && <><span className="k">Cap per investigation</span><span>{s.max_calls_per_investigation} calls</span></>}
          <span className="k">Jurisdictions</span><span>{s.jurisdictions.join(", ")}</span>
          <span className="k">Supported countries</span><span>{s.supported_countries.join(", ")}</span>
          <span className="k">Legal / terms</span><span>{s.legal || "—"} {s.terms_url && <Ext href={s.terms_url}>terms</Ext>}</span>
          <span className="k">Official API</span><span>{s.official_api ? "yes" : "no"} · {s.access_method}</span>
          <span className="k">Website</span><span>{s.website ? <Ext href={s.website} /> : "—"}</span>
          <span className="k">Documentation</span><span>{s.documentation ? <Ext href={s.documentation} /> : "—"}</span>
          <span className="k">Data licence</span><span>{s.license || "—"}</span>
          <span className="k">Reliability</span><span>{s.reliability}</span>
          <span className="k">Coverage</span><span>{s.coverage || "—"}</span>
          {s.kind === "adapter" && <><span className="k">Installed</span><span>{s.installed ? `yes (${s.install_note})` : s.install_note}</span></>}
        </div>

        {s.transforms.length > 0 && (
          <>
            <h3 style={{ marginTop: 18 }}>Transforms</h3>
            {s.transforms.map((t) => (
              <div key={t.name} className="small" style={{ padding: "4px 0", borderBottom: "1px dashed var(--line)" }}>
                <b>{t.title}</b> <span className="faint mono">{t.name}</span>
                <div className="faint">{t.input_types.map((x) => o.title(x)).join(", ")} → {t.output_types.map((x) => o.title(x)).join(", ") || "properties"}</div>
              </div>
            ))}
          </>
        )}
      </div>
    </>
  );
}

function TestConnection({ name }: { name: string }) {
  const [res, setRes] = useState<{ ok: boolean | null; status: string; message: string; test_input?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    try {
      setRes(await api.post(`/api/sources/${name}/test`));
    } catch (e) {
      setRes({ ok: false, status: "error", message: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="row wrap small" style={{ marginBottom: 12, gap: 8 }}>
      <button className="btn sm" disabled={busy} onClick={() => void run()}
        title="Runs the source once on a public test input (a mascot account, an example domain…) with your configured keys">
        {busy ? <Spinner /> : null}Test connection</button>
      {res && <span style={{ color: res.ok ? "var(--good)" : res.ok === null ? "var(--muted)" : "var(--bad)" }}>
        {res.ok ? "✓" : res.ok === null ? "–" : "✗"} {res.status.replace(/_/g, " ")}{res.test_input ? ` on ${res.test_input}` : ""}: {res.message}</span>}
    </div>
  );
}
