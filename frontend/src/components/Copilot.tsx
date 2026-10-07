// The investigation copilot's pieces: chat with provider attribution, proposals (Apply / Cancel), the identity-discovery
// stages, the identifier layer, notes, the cost dashboard and the budget hold. Used by views/AssistantView.tsx.

import { useState } from "react";
import { api, inv } from "../api";
import { Badge, ErrorBox, Ext, Spinner, fmtDate } from "../ui";

export interface Money { currency: string; symbol: string; rate: number; note: string }

export function fmtMoney(usd: number | null | undefined, m?: Money | null, unknown = "Unknown"): string {
  if (usd === null || usd === undefined) return unknown;
  const sym = m?.symbol ?? "$";
  const v = usd * (m?.rate ?? 1);
  return `${sym}${v.toFixed(v !== 0 && v < 0.1 ? 4 : 2)}`;
}

// ---------------------------------------------------------------- costs

interface CostLine { provider: string; title: string; category: string; services: string[]; models: string[]; requests: number;
  tokens: number; estimated: number; actual: number | null; pending: number; unknown: number; status: string;
  price_verified: boolean | null }
interface ChainStep { at: string; step: string; provider: string; title: string; what: string; requests: number; tokens?: number;
  estimated: number | null; actual?: number | null; status: string }
export interface Costs {
  lines: CostLine[]; by_category: Record<string, { title: string; estimated: number; actual: number | null; pending: number; unknown: number }>;
  chain: ChainStep[]; money: Money; explanation: string;
  totals: { estimated: number; actual: number | null; actual_covers: string; pending: number; unknown_items: number; free_requests: number };
  budget: { limit_usd: number | null; spent_usd: number; remaining_usd: number | null; confirm_above_usd: number | null; hold: BudgetHold | null };
}
export interface BudgetHold { reason: string; current_usd: number; after_usd: number; limit_usd: number; pending: unknown[]; at: string }

export function CostDashboard({ c }: { c: Costs }) {
  const m = c.money;
  return (
    <div className="card pad col">
      <div className="row between wrap">
        <b>Investigation cost</b>
        <span className="small faint">{m.note}</span>
      </div>
      <div className="kpis">
        {Object.entries(c.by_category).map(([k, b]) => (
          <div key={k} className="kpi"><b>{fmtMoney(b.estimated, m)}</b><span>{b.title}{b.actual !== null ? ` · actual ${fmtMoney(b.actual, m)}` : ""}
            {b.pending ? ` · pending ${fmtMoney(b.pending, m)}` : ""}{b.unknown ? ` · ${b.unknown} unknown` : ""}</span></div>
        ))}
      </div>
      <div className="kv">
        <span className="k">Total estimated</span><span><b>{fmtMoney(c.totals.estimated, m)}</b> <Badge>Estimated</Badge></span>
        <span className="k">Actual</span><span>{c.totals.actual !== null ? <><b>{fmtMoney(c.totals.actual, m)}</b> <Badge kind="good">Actual</Badge>
          <span className="small faint"> {c.totals.actual_covers}</span></> : <Badge kind="warn">Not reported by the providers</Badge>}</span>
        <span className="k">Pending</span><span>{c.totals.pending ? <>{fmtMoney(c.totals.pending, m)} <Badge kind="info">Pending</Badge></> : "—"}</span>
        <span className="k">Unknown</span><span>{c.totals.unknown_items ? <Badge kind="warn">{c.totals.unknown_items} billable step(s) without a known price</Badge> : "—"}</span>
        <span className="k">Budget</span><span>{c.budget.limit_usd === null ? "Unlimited" : <>{fmtMoney(c.budget.spent_usd, m)} of {fmtMoney(c.budget.limit_usd, m)}
          {" "}· remaining <b>{fmtMoney(c.budget.remaining_usd, m)}</b></>}</span>
      </div>
      {c.lines.length > 0 && (
        <table className="data">
          <thead><tr><th>Provider</th><th>Used by</th><th>Requests / tokens</th><th>Estimated</th><th>Actual</th><th>Status</th></tr></thead>
          <tbody>{c.lines.map((l) => (
            <tr key={l.provider}>
              <td>{l.title}<div className="small faint">{l.category}{l.models.length ? ` · ${l.models.join(", ")}` : ""}</div></td>
              <td className="small">{l.services.join(", ")}</td>
              <td className="small">{l.requests} req{l.tokens ? ` · ${l.tokens.toLocaleString()} tokens` : ""}</td>
              <td>{fmtMoney(l.estimated, m)}{l.price_verified === false && <div className="small faint">price not verified</div>}</td>
              <td>{l.actual !== null ? fmtMoney(l.actual, m) : <span className="faint">—</span>}</td>
              <td><Badge kind={l.status === "actual" ? "good" : l.status === "unknown" ? "warn" : ""}>{l.status}</Badge></td>
            </tr>))}</tbody>
        </table>
      )}
      {c.chain.length > 0 && (
        <details><summary className="small">Cost chain: {c.chain.length} billable step(s), in order</summary>
          <ol className="small" style={{ paddingLeft: 18 }}>{c.chain.map((s, i) => (
            <li key={i}>{fmtDate(s.at)} · <b>{s.step}</b> · {s.title} — {s.what} · {s.requests} req{s.tokens ? ` · ${s.tokens} tokens` : ""}
              {" "}· {s.actual !== undefined && s.actual !== null ? `${fmtMoney(s.actual, m)} actual` : `${fmtMoney(s.estimated, m)} est.`}</li>))}</ol>
        </details>
      )}
      <span className="small faint">{c.explanation}</span>
    </div>
  );
}

export function BudgetHoldBanner({ invId, hold, money, onDone }: { invId: string; hold: BudgetHold; money?: Money; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState("");
  const [err, setErr] = useState<string | null>(null);
  async function decide(decision: "continue" | "stop") {
    setBusy(true);
    setErr(null);
    try {
      const rate = money?.rate || 1;
      await api.post(inv(invId, "/budget"), { decision, budget_usd: limit ? Number(limit) / rate : null });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="notice warn col" style={{ margin: "12px 0" }}>
      <b>{hold.reason === "ask_before" ? "You asked to be asked before exceeding" : "Budget limit reached"}: {fmtMoney(hold.limit_usd, money)}</b>
      <span>This action may increase the estimated investigation cost from {fmtMoney(hold.current_usd, money)} to approximately
        {" "}{fmtMoney(hold.after_usd, money)}. {hold.pending.length} step(s) are waiting.</span>
      <div className="row wrap">
        <input type="number" min={0} step={0.1} placeholder={`new limit (${money?.currency ?? "USD"}, empty = unlimited)`} value={limit}
          onChange={(e) => setLimit(e.target.value)} style={{ maxWidth: 230 }} />
        <button className="btn primary sm" disabled={busy} onClick={() => void decide("continue")}>Continue</button>
        <button className="btn sm" disabled={busy} onClick={() => void decide("stop")}>Stop</button>
      </div>
      <ErrorBox error={err} />
    </div>
  );
}

// ---------------------------------------------------------------- proposals

export interface Proposal { proposal_id: string; op: string; title: string; summary: [string, string][]; status: string; auto?: boolean;
  destructive: boolean; duplicates?: { title: string; reasons: string[]; strength: string; origin: string }[]; result?: unknown;
  error?: string; preview?: { text: string; potential_calls: number; estimated_cost_display: string | null }; at?: string }

const APPLY_LABEL: Record<string, string> = { add_source: "Add source", update_source: "Save changes", link_source: "Save links",
  unlink_source: "Remove links", add_category: "Create category", add_platform: "Add platform", delete_source: "Delete source",
  delete_category: "Delete category", delete_platform: "Delete platform", delete_note: "Delete note", run_action: "Run" };

export function ProposalCard({ invId, p, onDone }: { invId: string; p: Proposal; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function decide(decision: "apply" | "cancel") {
    setBusy(true);
    setErr(null);
    try {
      await api.post(inv(invId, `/assistant/proposals/${p.proposal_id}`), { decision });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const pending = p.status === "pending";
  return (
    <div className={`card pad col proposal${p.destructive ? " destructive" : ""}`} style={{ gap: 6 }}>
      <div className="row between wrap">
        <b>{p.op === "run_action" ? "Preview: " : pending ? "The assistant proposes: " : ""}{p.title}</b>
        <Badge kind={p.status === "applied" ? "good" : p.status === "pending" ? (p.destructive ? "bad" : "warn") : p.status === "failed" ? "bad" : ""}>
          {p.status}{p.auto ? " (automatic)" : ""}</Badge>
      </div>
      <table className="data small"><tbody>{(p.summary ?? []).map(([k, v], i) => <tr key={i}><td className="faint">{k}</td><td>{v}</td></tr>)}</tbody></table>
      {(p.duplicates ?? []).length > 0 && <div className="small" style={{ color: "var(--warn)" }}>
        Possible duplicate: {(p.duplicates ?? []).map((d) => `${d.title} (${d.reasons.join(", ")})`).join("; ")}</div>}
      {p.destructive && pending && <span className="small" style={{ color: "var(--bad)" }}>Destructive: this cannot be undone.</span>}
      {pending && (
        <div className="row">
          <button className="btn sm" disabled={busy} onClick={() => void decide("cancel")}>Cancel</button>
          <button className={`btn sm ${p.destructive ? "bad" : "primary"}`} disabled={busy} onClick={() => void decide("apply")}>
            {busy ? <Spinner /> : null}{APPLY_LABEL[p.op] ?? "Apply"}</button>
        </div>
      )}
      {p.error && <span className="small" style={{ color: "var(--bad)" }}>{p.error}</span>}
      <ErrorBox error={err} />
    </div>
  );
}

// ---------------------------------------------------------------- discovery stages and identifiers

export interface Discovery {
  stages: { n: number; step: string; status: string; detail: string }[];
  web_discovery: { available: string[]; primary: string | null; failed: string[]; unavailable_message: string; queries_run: number;
    queries_failed: number; failovers: { from: string; to: string | null; status: string }[] };
  discovered_handles: { handle: string; id: string; on: string; rank: number; query: string; provider: string; pivot: boolean;
    searched_further?: boolean; predictable_from_name: boolean }[];
  journal: { at: string; connector: string; query: string; purpose: string; status: string; results: number; profiles: string[]; error?: string }[];
}
const STEP_ICON: Record<string, string> = { done: "✓", running: "●", pending: "○", skipped: "–" };

export function DiscoveryStages({ d, onOpen }: { d: Discovery; onOpen: (id: string) => void }) {
  return (
    <div className="card pad col">
      <div className="row between wrap">
        <b>Identity discovery</b>
        <span className="small">{d.web_discovery.primary ? <>Web search: <b>{d.web_discovery.primary}</b>
          {d.web_discovery.failed.length > 0 && ` (failed: ${d.web_discovery.failed.join(", ")})`} · {d.web_discovery.queries_run} queries</>
          : <Badge kind="warn">Web discovery unavailable</Badge>}</span>
      </div>
      {d.web_discovery.unavailable_message && <div className="notice warn small">{d.web_discovery.unavailable_message} <a href="#/sources">Configure a
        search provider</a></div>}
      <div className="grid2" style={{ gap: 2 }}>{d.stages.map((s) => (
        <div key={s.n} className={`step ${s.status}`}><span className="ico">{STEP_ICON[s.status] ?? "○"}</span>{s.n}. {s.step}
          {s.detail && <span className="faint small"> · {s.detail}</span>}</div>))}</div>
      {d.discovered_handles.length > 0 && (
        <table className="data small">
          <thead><tr><th>Discovered handle</th><th>On</th><th>Rank</th><th>Query</th><th>Provider</th><th>Pivot</th></tr></thead>
          <tbody>{d.discovered_handles.map((h) => (
            <tr key={h.id}><td className="mono"><a style={{ cursor: "pointer" }} onClick={() => onOpen(h.id)}>{h.handle}</a>
              {!h.predictable_from_name && <div className="faint">not derivable from the name</div>}</td>
              <td>{h.on}</td><td>#{h.rank}</td><td className="muted">{h.query}</td><td>{h.provider}</td>
              <td>{h.searched_further ? <Badge kind="good">searched{h.pivot ? " (auto)" : ""}</Badge> : <span className="faint">not yet</span>}</td></tr>))}</tbody>
        </table>
      )}
      {d.journal.length > 0 && <details><summary className="small">Web queries ({d.journal.length} most recent)</summary>
        <table className="data small"><tbody>{d.journal.slice().reverse().map((j, i) => (
          <tr key={i}><td className="faint">{fmtDate(j.at)}</td><td className="mono">{j.query}</td><td>{j.connector}</td>
            <td><Badge kind={j.status === "ok" ? "" : "warn"}>{j.status}</Badge></td>
            <td>{j.results} result(s){j.profiles?.length ? ` · ${j.profiles.join(", ")}` : ""}{j.error ? ` · ${j.error}` : ""}</td></tr>))}</tbody></table>
      </details>}
    </div>
  );
}

export interface IdentifierRow { id: string; key: string; type: string; value: string; state: string; state_title: string; source: string;
  source_kind?: string; source_url?: string | null; discovered_at: string; confidence: number; search_rank?: number | null;
  search_query?: string | null; search_provider?: string | null; platform?: string | null; platforms_found: string[]; pivoted: boolean;
  verdict?: string }
const STATE_KIND: Record<string, string> = { provided: "info", generated: "warn", discovered: "violet", observed: "", possible: "info",
  verified: "good", rejected: "bad" };

export function IdentifiersTable({ items, counts, states, onOpen }: { items: IdentifierRow[]; counts: Record<string, number>;
  states: Record<string, string>; onOpen: (id: string) => void }) {
  const [state, setState] = useState("");
  const rows = items.filter((r) => !state || r.state === state);
  return (
    <div className="card pad col">
      <div className="row wrap" style={{ gap: 6 }}>
        <button className={`btn sm ${state === "" ? "primary" : ""}`} onClick={() => setState("")}>All · {items.length}</button>
        {Object.entries(states).map(([k, v]) => counts[k] ? (
          <button key={k} className={`btn sm ${state === k ? "primary" : ""}`} onClick={() => setState(k)}>{v} · {counts[k]}</button>) : null)}
      </div>
      <div style={{ maxHeight: 380, overflow: "auto" }}>
        <table className="data small">
          <thead><tr><th>Identifier</th><th>Type</th><th>State</th><th>Source</th><th>Search</th><th>Found on</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.key}>
              <td className="mono"><a style={{ cursor: "pointer" }} onClick={() => onOpen(r.id)}>{r.value}</a>{r.verdict && <div className="faint">{r.verdict}</div>}</td>
              <td>{r.type}{r.platform ? <div className="faint">{r.platform}</div> : null}</td>
              <td><Badge kind={STATE_KIND[r.state] ?? ""}>{r.state_title}</Badge>{r.pivoted && <div className="faint">pivoted</div>}</td>
              <td className="muted">{r.source_url ? <Ext href={r.source_url}>{r.source}</Ext> : r.source}{r.source_kind && <div className="faint">{r.source_kind.replace(/_/g, " ")}</div>}</td>
              <td className="muted">{r.search_rank ? `#${r.search_rank} · ${r.search_provider ?? ""}` : "—"}{r.search_query && <div className="faint">{r.search_query}</div>}</td>
              <td>{r.platforms_found.join(", ") || "—"}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- notes

export interface Note { id: string; title: string; body: string; identifiers?: string[]; status?: string; by?: string; updated_at: string }

export function NotesPanel({ invId, notes, onChange }: { invId: string; notes: Note[]; onChange: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [err, setErr] = useState<string | null>(null);
  async function add() {
    try {
      await api.post(inv(invId, "/notes"), { title, body, identifiers: [], status: "info" });
      setTitle("");
      setBody("");
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function del(id: string) {
    await api.del(inv(invId, `/notes/${id}`));
    onChange();
  }
  return (
    <div className="card pad col">
      {notes.length === 0 && <span className="small faint">No notes yet. The assistant keeps notes of important identifiers here too.</span>}
      {notes.map((n) => (
        <div key={n.id} style={{ borderBottom: "1px dashed var(--line)", paddingBottom: 6 }}>
          <div className="row between"><b>{n.title}</b><span className="row small" style={{ gap: 6 }}>{n.status && <Badge>{n.status}</Badge>}
            <span className="faint">{n.by} · {fmtDate(n.updated_at)}</span>
            <button className="btn ghost sm" onClick={() => void del(n.id)} title="Delete note">✕</button></span></div>
          <div className="small" style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>
          {(n.identifiers ?? []).length > 0 && <div className="small mono faint">{(n.identifiers ?? []).join(" · ")}</div>}
        </div>
      ))}
      <div className="row wrap">
        <input type="text" placeholder="Note title" value={title} onChange={(e) => setTitle(e.target.value)} style={{ maxWidth: 220 }} />
        <input type="text" placeholder="Note" value={body} onChange={(e) => setBody(e.target.value)} style={{ flex: 1, minWidth: 200 }} />
        <button className="btn sm" disabled={!title.trim()} onClick={() => void add()}>Add note</button>
      </div>
      <ErrorBox error={err} />
    </div>
  );
}

// ---------------------------------------------------------------- chat

export interface ChatTurn { seq: number; at: string; kind: string; user?: string; answer?: string; note?: string; stop?: string; model?: string;
  destination?: string; provider?: string; handled_by?: { provider: string; title: string; model: string; fallback: boolean } | null;
  attempts?: { provider: string; error: string; skipped?: boolean }[];
  usage?: { prompt_tokens: number; completion_tokens: number; estimated_cost: number | null; actual_cost: number | null; requests: number };
  tool_calls?: { tool: string; input: Record<string, unknown>; error: boolean; result_preview: string }[]; proposals?: Proposal[] }
export interface ProviderInfo { provider: string; title?: string; ready: boolean; any_ready?: boolean; problem: string; destination: string;
  model: string; sends: string; failover?: { id: string; title: string; ready: boolean; model: string }[] }

const PROMPTS = ["Find this person's public profiles", "Which candidate is most likely, and why?", "Search for more usernames",
  "Check Pinterest and TikTok", "Why didn't you find another username?", "Which searches did you perform?",
  "Which provider cost the most?", "What's the next best step?", "Audit Sweden"];

export function ChatPanel({ invId, turns, provider, proposals, money, onChange }: { invId: string; turns: ChatTurn[];
  provider: ProviderInfo; proposals: Proposal[]; money?: Money; onChange: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function send(t: string, confirmBudget = false) {
    if (!t.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.post(inv(invId, "/assistant/chat"), { text: t, confirm_budget: confirmBudget });
      setText("");
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const byId = new Map(proposals.map((p) => [p.proposal_id, p]));
  if (!provider.ready && !provider.any_ready) {
    return (
      <div className="card pad">
        <p className="small muted" style={{ margin: 0 }}>{provider.provider === "none" ? "No AI provider configured: everything on this page works without one." :
          provider.problem} Set one up in <a href="#/settings">Settings → AI providers</a> (OpenAI, Anthropic, OpenRouter, a local model or
          another compatible endpoint).</p>
      </div>
    );
  }
  return (
    <div className="card pad col chat">
      <span className="small faint">Attached to this investigation: it remembers the conversation and acts only through the application's tools.
        Primary: <b>{provider.title ?? provider.provider}</b> ({provider.model}) → {provider.destination}
        {(provider.failover ?? []).length > 0 && <> · failover: {(provider.failover ?? []).map((f) => `${f.title}${f.ready ? "" : " (not ready)"}`).join(" → ")}</>}.
        {" "}Each message sends {provider.sends}</span>
      {turns.map((m) => (
        <div key={m.seq} className="chat-turn">
          <div className="you"><b>You</b> <span className="faint small">{fmtDate(m.at)}</span><div>{m.user}</div></div>
          {(m.tool_calls ?? []).length > 0 && (
            <details className="small"><summary>{m.tool_calls!.length} tool call(s): {m.tool_calls!.map((t) => t.tool).join(", ")}</summary>
              {m.tool_calls!.map((t, i) => <div key={i} className="mono small" style={{ margin: "4px 0" }}>
                {t.error ? "✗" : "✓"} {t.tool}({JSON.stringify(t.input)})<div className="faint">{t.result_preview}</div></div>)}</details>
          )}
          <div className="ai">
            <b>Assistant</b>{m.handled_by && <span className="small faint"> · {m.handled_by.title} / {m.handled_by.model}
              {m.handled_by.fallback && <Badge kind="warn">failover</Badge>}</span>}
            {m.usage && m.usage.requests > 0 && <span className="small faint"> · {m.usage.prompt_tokens + m.usage.completion_tokens} tokens ·
              {" "}{m.usage.actual_cost !== null ? `${fmtMoney(m.usage.actual_cost, money)} actual` : m.usage.estimated_cost !== null
                ? `${fmtMoney(m.usage.estimated_cost, money)} est.` : "cost unknown"}</span>}
            {m.note && <span className="small" style={{ color: "var(--warn)" }}> {m.note}</span>}
            <div style={{ whiteSpace: "pre-wrap" }}>{m.answer || (m.stop === "error" ? "—" : "")}</div>
            {m.stop === "budget" && m.user && <button className="btn sm warn" disabled={busy} onClick={() => void send(m.user!, true)}>
              Continue anyway (this message)</button>}
            {(m.attempts ?? []).length > 0 && <div className="small faint">Tried: {(m.attempts ?? []).map((a) => `${a.provider}${a.skipped ? " (skipped)" : ""}: ${a.error}`).join(" · ")}</div>}
          </div>
          {(m.proposals ?? []).map((p) => {
            const cur = byId.get(p.proposal_id) ?? p;
            return <ProposalCard key={p.proposal_id} invId={invId} p={cur} onDone={onChange} />;
          })}
        </div>
      ))}
      <div className="row wrap" style={{ gap: 4 }}>{PROMPTS.map((p) => (
        <button key={p} className="btn ghost sm" disabled={busy} onClick={() => setText(p)}>{p}</button>))}</div>
      <div className="row">
        <textarea rows={2} value={text} placeholder="Ask, or tell it what to do: “Check the username we just found on Pinterest”, “Add https://… to the Swedish legal sources”"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send(text); }} />
        <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void send(text)}>{busy ? <Spinner /> : null}Send</button>
      </div>
      <ErrorBox error={err} />
    </div>
  );
}
