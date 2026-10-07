import { useState } from "react";
import { api } from "../api";
import { OriginBadge, PlatformForm } from "../components/Catalog";
import type { UserPlatform } from "../types";
import { Badge, ErrorBox, Ext, Spinner, useApi, useDebounced } from "../ui";

interface Platform {
  id: string; name: string; category: string; category_title: string; url: string; profile_url: string; check: string;
  api?: string; api_requirements?: string; public_profile?: string; public_search?: string; private_accounts?: boolean;
  search_methods?: string[]; auth?: string; rate_limit?: string; countries?: string[]; region?: string; notes?: string;
  status?: string; status_note?: string; source: string; datasets?: string[]; search_status: string; regional: boolean;
  probe?: { docs?: string }; origin?: "system" | "user"; record?: UserPlatform; user_sources?: { id: string; title: string }[];
  recognizable?: boolean;
}
interface Payload {
  rows: Platform[]; total: number; offset: number; limit: number;
  counts: { curated: number; user: number; imported: number; registered: number; searchable: number; api_only: number; unavailable: number;
    regional_total: number; by_status: Record<string, number>; by_category: Record<string, number> };
  categories: Record<string, string>;
  search_status_titles: Record<string, string>;
  provenance: Record<string, { source_url: string; license: string; retrieved_at: string; items: number }>;
}

const SEARCH_KIND: Record<string, string> = { searchable: "good", api_key_required: "warn", subscription_required: "violet",
  adapter_not_installed: "", adapter_off: "info", search_everything_only: "info", adult_off: "", no_rule: "", manual: "info", not_publicly_searchable: "", defunct: "bad", excluded: "bad",
  disabled: "", catalog_only: "info" };
const CHECK_TITLE: Record<string, string> = { whatsmyname: "WhatsMyName rule (public page)", maigret: "Maigret adapter",
  sherlock: "Sherlock adapter", probe: "Public API probe", manual: "Link for the investigator", none: "No public lookup" };
const PROFILE: Record<string, string> = { yes: "public", partial: "partly public", login_wall: "login required", none: "no public profiles" };

export default function Platforms() {
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [country, setCountry] = useState("");
  const [page, setPage] = useState(0);
  const [form, setForm] = useState<{ initial: UserPlatform | null } | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const dq = useDebounced(q, 300);
  const limit = 150;
  const params = new URLSearchParams({ q: dq, category: cat, source, status, country, offset: String(page * limit), limit: String(limit) });
  const { data, error, reload } = useApi<Payload>(`/api/platforms?${params}`);
  async function remove(id: string) {
    setErr(null);
    try {
      await api.del(`/api/catalog/platforms/${id}`);
      setConfirm(null);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const c = data.counts;
  const pages = Math.ceil(data.total / limit);
  const reset = (fn: () => void) => { fn(); setPage(0); };
  return (
    <div className="page wide">
      <div className="row between wrap">
        <h1>Platform registry</h1>
        <button className="btn primary" onClick={() => setForm({ initial: null })}>＋ Add platform</button>
      </div>
      <p className="sub">
        {c.registered.toLocaleString()} platforms registered: {c.curated} curated with capability data (category, country, public profile and search
        support, API and its requirements, private accounts, how Sextant checks it){c.user ? `, ${c.user} added by you (manual checks only)` : ""} plus
        {" "}{c.imported.toLocaleString()} sites known only from the community datasets (WhatsMyName, Maigret, Sherlock). <b>Registered is not searchable:</b> the status column says what this
        installation can do with each one right now.
      </p>
      <div className="kpis" style={{ marginBottom: 14 }}>
        {(["searchable", "search_everything_only", "adapter_off", "adult_off", "api_key_required", "subscription_required", "adapter_not_installed", "manual", "no_rule", "not_publicly_searchable", "excluded", "defunct"] as const).filter((k) => (c.by_status[k] ?? 0) > 0 || k === "searchable").map((k) => (
          <div key={k} className="kpi" style={{ cursor: "pointer", borderColor: status === k ? "var(--accent)" : undefined }}
            onClick={() => reset(() => setStatus(status === k ? "" : k))}>
            <b>{(c.by_status[k] ?? 0).toLocaleString()}</b><span>{data.search_status_titles[k]}</span>
          </div>
        ))}
      </div>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <input type="search" placeholder="Search platforms…" value={q} onChange={(e) => reset(() => setQ(e.target.value))} style={{ maxWidth: 240 }} />
        <select value={cat} onChange={(e) => reset(() => setCat(e.target.value))} style={{ maxWidth: 260 }}>
          <option value="">All categories</option>
          {Object.entries(data.categories).map(([k, v]) => <option key={k} value={k}>{v} ({c.by_category[k] ?? 0})</option>)}
        </select>
        <select value={source} onChange={(e) => reset(() => setSource(e.target.value))} style={{ maxWidth: 200 }}>
          <option value="">Curated + datasets</option><option value="curated">Curated only</option><option value="user">Yours only</option>
          <option value="whatsmyname">WhatsMyName only</option><option value="maigret">Maigret only</option><option value="sherlock">Sherlock only</option>
        </select>
        <input type="search" placeholder="Country (ISO, e.g. JP)" value={country} maxLength={2}
          onChange={(e) => reset(() => setCountry(e.target.value.toUpperCase()))} style={{ maxWidth: 160 }} />
        <span className="small muted">{data.total.toLocaleString()} match · page {page + 1}/{Math.max(1, pages)}</span>
        <button className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
        <button className="btn sm" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>›</button>
      </div>
      <ErrorBox error={err} />
      <div className="card">
        <table className="data">
          <thead><tr><th>Platform</th><th>Category</th><th>Status here</th><th>How it is checked</th><th>Public profile</th>
            <th>Public search</th><th>Official API</th><th>Region</th><th>Notes</th></tr></thead>
          <tbody>
            {data.rows.map((p) => (
              <tr key={p.id}>
                <td><Ext href={p.url}>{p.name}</Ext> <OriginBadge origin={p.origin} />
                  {p.profile_url && <div className="small faint mono wrapword">{p.profile_url}</div>}
                  {p.origin === "user" && <div className="small faint">{p.recognizable ? "pasted profile links are recognised"
                    : p.profile_url ? "pasted profile links are not recognised" : "no profile links (add a template)"}</div>}
                  {p.source !== "curated" && p.source !== "user" && <div className="small faint">from {p.datasets?.join(", ")}</div>}
                  {p.user_sources && <div className="small">Your sources: {p.user_sources.map((x) => (
                    <a key={x.id} href={`#/sources/${x.id}`} style={{ marginRight: 6 }}>{x.title}</a>))}</div>}
                  {p.origin === "user" && p.record && (confirm === p.id ? (
                    <div className="row small" style={{ gap: 6, marginTop: 4 }}>Delete it? Links from your sources are removed too.
                      <button className="btn bad sm" onClick={() => void remove(p.id)}>Delete</button>
                      <button className="btn ghost sm" onClick={() => setConfirm(null)}>Keep</button></div>
                  ) : (
                    <div className="row" style={{ gap: 4, marginTop: 4 }}>
                      <button className="btn sm" onClick={() => setForm({ initial: p.record ?? null })}>Edit</button>
                      <button className="btn ghost sm" onClick={() => setConfirm(p.id)}>Delete</button></div>
                  ))}</td>
                <td className="small">{p.category_title}</td>
                <td><Badge kind={SEARCH_KIND[p.search_status]}>{data.search_status_titles[p.search_status] ?? p.search_status}</Badge>
                  {p.status === "defunct" && p.status_note && <div className="small faint">{p.status_note}</div>}</td>
                <td className="small">{CHECK_TITLE[p.check] ?? <a href={`#/sources/${p.check}`}>Connector: {p.check}</a>}
                  {p.check === "whatsmyname" && p.search_status !== "searchable" &&
                    <div className="faint">rule missing or disabled in the current WhatsMyName dataset</div>}
                  {p.probe?.docs && <div><Ext href={p.probe.docs}>API docs</Ext></div>}</td>
                <td className="small">{p.public_profile ? PROFILE[p.public_profile] ?? p.public_profile : "—"}
                  {p.private_accounts && <div className="faint">private accounts exist (never accessed)</div>}</td>
                <td className="small">{p.public_search ?? "—"}</td>
                <td className="small">{p.api ?? "—"}{p.api_requirements && <div className="faint">{p.api_requirements}</div>}</td>
                <td className="small muted">{p.regional ? (p.region || p.countries?.join(", ")) : "global"}</td>
                <td className="small muted wrapword">{p.notes}{p.rate_limit ? ` · ${p.rate_limit}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small faint" style={{ marginTop: 12 }}>
        Datasets: {Object.entries(data.provenance).map(([k, v]) => `${k} (${v.items.toLocaleString()} sites, ${v.license}, retrieved ${String(v.retrieved_at).slice(0, 10)})`).join(" · ")}.
        Private messages, private accounts and login-only content are never accessed.
      </p>
      {form && <PlatformForm initial={form.initial} onClose={() => setForm(null)} onSaved={() => { setForm(null); void reload(); }} />}
    </div>
  );
}
