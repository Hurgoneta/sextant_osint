// Settings: AI providers (one abstraction, several accounts, failover), search-provider routing, the pricing table and the
// scoped Hard Reset (catalog / investigation history / AI settings / API credentials as separate choices).

import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, ErrorBox, Spinner, useApi } from "../ui";

interface Profile { id: string; title: string; kind: string; enabled: boolean; model: string; base_url: string; key_name: string; key_set: boolean;
  destination: string; local: boolean; ready: boolean; problem: string; temperature: number | null; max_output: number; reasoning: string;
  reasoning_options: string[]; timeout: number; primary: boolean; failover_position: number | null;
  capabilities: Record<string, boolean | string> }
interface ProvidersPayload { providers: Profile[]; primary: string; failover: string[] }
interface ModelRow { id: string; name: string; context_length?: number; input_per_mtok?: number; output_per_mtok?: number;
  supports_tools?: boolean | null }

function ProviderRow({ p, onSaved }: { p: Profile; onSaved: (d: ProvidersPayload) => void }) {
  const [f, setF] = useState({ model: p.model, base_url: p.base_url, temperature: p.temperature === null ? "" : String(p.temperature),
    max_output: String(p.max_output), reasoning: p.reasoning, timeout: String(p.timeout), key: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<ModelRow[]>([]);
  useEffect(() => {
    if (p.kind === "openai") api.get<{ models: ModelRow[] }>(`/api/ai/providers/${p.id}/models`).then((d) => setModels(d.models)).catch(() => undefined);
  }, [p.id, p.kind]);
  async function save(extra: Record<string, unknown> = {}) {
    setErr(null);
    try {
      const body: Record<string, unknown> = { model: f.model || null, base_url: f.base_url || null, reasoning: f.reasoning || null,
        max_output: f.max_output ? Number(f.max_output) : null, timeout: f.timeout ? Number(f.timeout) : null,
        temperature: f.temperature === "" ? null : Number(f.temperature), ...(f.key ? { key: f.key } : {}), ...extra };
      onSaved(await api.put<ProvidersPayload>(`/api/ai/providers/${p.id}`, body));
      setF({ ...f, key: "" });
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function test() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.post<{ ok: boolean; message: string; model?: string }>(`/api/ai/providers/${p.id}/test`);
      setMsg(r.ok ? `✓ ${r.model ?? ""} answered: ${r.message}` : `✗ ${r.message}`);
    } finally {
      setBusy(false);
    }
  }
  async function listModels() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ models: ModelRow[]; count: number; prices_stored: number }>(`/api/ai/providers/${p.id}/models`);
      setModels(r.models);
      setMsg(`${r.count} model(s) listed${r.prices_stored ? `; ${r.prices_stored} published price(s) stored in the pricing table` : ""}.`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const chosen = models.find((m) => m.id === f.model);
  return (
    <details className="card pad" open={p.primary}>
      <summary className="row between wrap" style={{ cursor: "pointer" }}>
        <b>{p.title}</b>
        <span className="row" style={{ gap: 6 }}>
          {p.primary && <Badge kind="info">primary</Badge>}
          {p.failover_position && <Badge>failover #{p.failover_position}</Badge>}
          {!p.enabled && <Badge kind="bad">disabled</Badge>}
          <Badge kind={p.ready ? "good" : "warn"}>{p.ready ? `ready · ${p.model}` : p.problem}</Badge>
        </span>
      </summary>
      <div className="grid3" style={{ marginTop: 10 }}>
        <label className="field">Model
          <input type="text" list={`models-${p.id}`} value={f.model} placeholder={p.kind === "anthropic" ? "claude-opus-5-5" : "model id"}
            onChange={(e) => setF({ ...f, model: e.target.value })} />
          <datalist id={`models-${p.id}`}>{models.slice(0, 500).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</datalist>
          {chosen && <span className="small faint">{chosen.context_length ? `${chosen.context_length.toLocaleString()} context · ` : ""}
            {chosen.input_per_mtok !== undefined ? `$${chosen.input_per_mtok}/$${chosen.output_per_mtok} per M tokens` : "price not listed"}
            {chosen.supports_tools === false && " · no tool calling"}</span>}
        </label>
        {p.kind !== "anthropic" && <label className="field">Endpoint<input type="text" value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} /></label>}
        <label className="field">API key ({p.key_name})<input type="password" autoComplete="off" value={f.key}
          placeholder={p.key_set ? "•••••••• set (leave empty to keep)" : p.local ? "optional for local servers" : "key"}
          onChange={(e) => setF({ ...f, key: e.target.value })} /></label>
        {p.reasoning_options.length > 1 && <label className="field">Reasoning / effort
          <select value={f.reasoning} onChange={(e) => setF({ ...f, reasoning: e.target.value })}>
            {p.reasoning_options.map((r) => <option key={r}>{r}</option>)}</select></label>}
        {p.kind !== "anthropic" && <label className="field">Temperature<input type="number" min={0} max={2} step={0.1} value={f.temperature}
          placeholder="provider default" onChange={(e) => setF({ ...f, temperature: e.target.value })} /></label>}
        <label className="field">Maximum output (tokens)<input type="number" min={64} max={128000} value={f.max_output}
          onChange={(e) => setF({ ...f, max_output: e.target.value })} /></label>
        <label className="field">Timeout (s)<input type="number" min={5} max={900} value={f.timeout} onChange={(e) => setF({ ...f, timeout: e.target.value })} /></label>
      </div>
      <div className="row wrap" style={{ marginTop: 8 }}>
        <button className="btn primary sm" onClick={() => void save()}>Save</button>
        <label className="check small"><input type="checkbox" checked={p.enabled} onChange={(e) => void save({ enabled: e.target.checked })} />enabled</label>
        {!p.primary && <button className="btn sm" onClick={() => void save({ make_primary: true })}>Make primary</button>}
        {p.key_set && <button className="btn ghost sm" onClick={() => void save({ clear_key: true })}>Remove key</button>}
        <button className="btn sm" disabled={busy || !p.ready} onClick={() => void test()}>{busy ? <Spinner /> : null}Test connection</button>
        {p.kind === "openai" && <button className="btn sm" disabled={busy} onClick={() => void listModels()}>List models</button>}
      </div>
      <span className="small faint">Sends chat messages and investigation findings to <b>{p.destination || "—"}</b>{p.local ? " (this computer)" : ""}.
        {" "}Cost: {String(p.capabilities.cost_metadata)}.</span>
      {msg && <div className="small">{msg}</div>}
      <ErrorBox error={err} />
    </details>
  );
}

export function AIProvidersCard() {
  const { data, error, setData } = useApi<ProvidersPayload>("/api/ai/providers");
  const [order, setOrder] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (data) setOrder(data.failover.join(", "));
  }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  async function saveOrder() {
    try {
      setData(await api.put<ProvidersPayload>("/api/ai/failover", { order: order.split(",").map((x) => x.trim()).filter(Boolean) }));
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <div className="col" style={{ gap: 8 }}>
      <span className="small muted">The assistant tab works without any AI. With a provider you can talk to it; it acts only through the
        application's tools. Each provider has its own model, endpoint, key and limits. When the primary is unavailable, the failover order is
        tried; a disabled or unconfigured provider is never used, and every answer says which provider and model handled it.</span>
      {data.providers.map((p) => <ProviderRow key={p.id} p={p} onSaved={setData} />)}
      <div className="row wrap">
        <label className="field" style={{ flex: 1 }}>Failover order (provider ids, after the primary)
          <input type="text" value={order} placeholder="e.g. openai, anthropic" onChange={(e) => setOrder(e.target.value)} /></label>
        <button className="btn sm" onClick={() => void saveOrder()}>Save order</button>
      </div>
      <span className="small faint">Ids: {data.providers.map((p) => p.id).join(", ")}</span>
      <ErrorBox error={err} />
    </div>
  );
}

interface Price { key: string; provider: string; provider_title: string; service: string; connector?: string; model?: string; per_request?: number;
  input_per_mtok?: number; output_per_mtok?: number; cached_input_per_mtok?: number; reasoning_per_mtok?: number; origin: string;
  verified: boolean; verified_date?: string; source_url?: string; note?: string; effective_date?: string }

export function PricingCard() {
  const { data, error, reload } = useApi<{ prices: Price[]; note: string }>("/api/pricing");
  const [f, setF] = useState<Record<string, string>>({ provider: "", service: "web_search", connector: "", model: "", per_request: "",
    input_per_mtok: "", output_per_mtok: "", cached_input_per_mtok: "", source_url: "", verified_date: "" });
  const [err, setErr] = useState<string | null>(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  async function save() {
    try {
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(f)) {
        if (v === "") continue;
        body[k] = ["per_request", "input_per_mtok", "output_per_mtok", "cached_input_per_mtok"].includes(k) ? Number(v) : v;
      }
      await api.put("/api/pricing", body);
      setErr(null);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function del(key: string) {
    await api.del(`/api/pricing?key=${encodeURIComponent(key)}`);
    await reload();
  }
  const usd = (v?: number) => (v === undefined || v === null ? "—" : `$${v}`);
  return (
    <div className="card pad col">
      <span className="small muted">{data.note} Edit a row to override it (your value wins over the shipped one; removing your override restores it).
        Prices listed by a provider's own API (OpenRouter's model list) are stored with the date they were read.</span>
      <div style={{ maxHeight: 360, overflow: "auto" }}>
        <table className="data small">
          <thead><tr><th>Provider</th><th>Service / model</th><th>Per request</th><th>Input / output per M tokens</th><th>Cached</th><th>Verified</th><th /></tr></thead>
          <tbody>{data.prices.map((p) => (
            <tr key={p.key}>
              <td>{p.provider_title}<div className="faint">{p.origin}</div></td>
              <td>{p.service}{p.connector ? ` · ${p.connector}` : ""}{p.model ? <div className="mono">{p.model}</div> : null}</td>
              <td>{usd(p.per_request)}</td><td>{usd(p.input_per_mtok)} / {usd(p.output_per_mtok)}</td><td>{usd(p.cached_input_per_mtok)}</td>
              <td>{p.verified ? <span title={p.source_url}>{p.verified_date}</span> : <Badge kind="warn">not verified</Badge>}</td>
              <td className="row" style={{ gap: 4 }}>
                <button className="btn ghost sm" onClick={() => setF({ provider: p.provider, service: p.service, connector: p.connector ?? "",
                  model: p.model ?? "", per_request: p.per_request?.toString() ?? "", input_per_mtok: p.input_per_mtok?.toString() ?? "",
                  output_per_mtok: p.output_per_mtok?.toString() ?? "", cached_input_per_mtok: p.cached_input_per_mtok?.toString() ?? "",
                  source_url: p.source_url ?? "", verified_date: p.verified_date ?? "" })}>edit</button>
                {p.origin === "user" && <button className="btn ghost sm" onClick={() => void del(p.key)}>reset</button>}
              </td>
            </tr>))}</tbody>
        </table>
      </div>
      <div className="grid3">
        {Object.keys(f).map((k) => (
          <label key={k} className="field">{k.replace(/_/g, " ")}<input type={k === "verified_date" ? "date" : "text"} value={f[k]}
            onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>))}
      </div>
      <div><button className="btn sm primary" disabled={!f.provider} onClick={() => void save()}>Save price</button></div>
      <ErrorBox error={err} />
    </div>
  );
}

interface SearchProv { name: string; title: string; status: string; note: string; per_query_usd: number | null; price_known: boolean; rank: number }

export function SearchProvidersCard() {
  const { data, error } = useApi<{ providers: SearchProv[]; mode: string; unavailable_message: string }>("/api/search-providers");
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  return (
    <div className="card pad col">
      {data.unavailable_message && <div className="notice warn small">{data.unavailable_message}</div>}
      <table className="data small">
        <thead><tr><th>#</th><th>Provider</th><th>Status</th><th>Price per query</th></tr></thead>
        <tbody>{data.providers.map((p) => (
          <tr key={p.name}><td>{p.status === "available" ? p.rank : ""}</td><td>{p.title}<div className="faint mono">{p.name}</div></td>
            <td><Badge kind={p.status === "available" ? "good" : ""}>{p.status.replace(/_/g, " ")}</Badge></td>
            <td>{p.price_known ? `$${p.per_query_usd}` : <span className="faint">unknown — set it under Pricing</span>}</td></tr>))}</tbody>
      </table>
      <span className="small faint">Mode: {data.mode}. Automatic searches go to the first available provider and fail over to the next; change the
        order with “Search provider preference” above, add keys under <a href="#/sources">Data sources → Search engines</a>.</span>
    </div>
  );
}

interface ResetSummary { scopes: Record<string, { title: string; removes: Record<string, number>; also: string; keeps: string; names?: string[] }> }

export function ResetPanel() {
  const { data, reload } = useApi<ResetSummary>("/api/reset");
  const [scopes, setScopes] = useState<string[]>([]);
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!data) return <Spinner />;
  async function go() {
    try {
      await api.post("/api/reset", { scopes, confirm });
      setMsg(`Reset: ${scopes.join(", ")}.`);
      setScopes([]);
      setConfirm("");
      setErr(null);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <div className="card pad col">
      <span className="small muted">Each part is a separate, explicit choice: nothing outside the parts you tick changes. API keys and investigations
        are never removed unless you tick them.</span>
      {Object.entries(data.scopes).map(([k, s]) => (
        <label key={k} className="check" style={{ alignItems: "flex-start" }}>
          <input type="checkbox" checked={scopes.includes(k)} onChange={(e) => setScopes(e.target.checked ? [...scopes, k] : scopes.filter((x) => x !== k))} />
          <span><b>{s.title}</b> — removes {Object.entries(s.removes).filter(([, v]) => v).map(([n, v]) => `${v} ${n.replace(/_/g, " ")}`).join(", ") || "nothing yet"}.
            {" "}<span className="small muted">{s.also} Keeps: {s.keeps}</span></span>
        </label>))}
      <div className="row">
        <input type="text" placeholder="Type RESET" value={confirm} onChange={(e) => setConfirm(e.target.value)} style={{ maxWidth: 160 }} />
        <button className="btn bad" disabled={!scopes.length || confirm !== "RESET"} onClick={() => void go()}>Reset the selected parts</button>
      </div>
      {msg && <span className="small">{msg}</span>}
      <ErrorBox error={err} />
    </div>
  );
}
