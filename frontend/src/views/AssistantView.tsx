import { useState } from "react";
import { api, inv } from "../api";
import { CandidateTable, CostTier, EvidenceClass, VALUE_KIND } from "../components/Assistant";
import { BudgetHoldBanner, ChatPanel, CostDashboard, DiscoveryStages, IdentifiersTable, NotesPanel, ProposalCard, fmtMoney } from "../components/Copilot";
import type { BudgetHold, ChatTurn, Costs, Discovery, IdentifierRow, Note, Proposal, ProviderInfo } from "../components/Copilot";
import { Badge, ErrorBox, Ext, RunStatus, Spinner, fmtDate, useApi } from "../ui";

interface Member { id: string; label: string; type_title: string; platform_title: string | null; username: string | null;
  url: string | null; status: string; verdict: string; verdict_title: string; verdict_kind: string; score: number;
  independent_signals: string[]; note: string; found_under_generated_handle: boolean;
  origin: { class: string; title: string; url?: string | null; source_kind?: string; query?: string };
  support: { text: string; weight: number; strength?: string }[]; contradictions: { text: string; weight: number }[];
  explanation?: { confidence: string; for: { text: string; strength: string }[]; against: { text: string; strength: string }[]; rule: string } }
interface Identity { id: string; letter: string; title: string; verdict: string; verdict_title: string; verdict_kind: string;
  score: number; members: Member[]; grouped_because: string[] }
interface Action { id: string; kind: string; title: string; reason: string; expected_value: string; cost_tier: string;
  executable: boolean; requires_confirmation: boolean; entity_id?: string; url?: string; connector?: string; count?: number;
  handles?: string[]; estimated_cost_usd?: number | null; potential_value?: string; transforms?: string[] }
interface PlatformRow { platform: string; candidate: string; check: string; status: string; candidate_origin: string;
  evidence: string; verdict: string; verdict_kind: string; confidence: number | null; url: string | null; account_id: string | null }
type LogItem = ChatTurn & { title?: string; reason?: string; by?: string; why?: string; estimated_cost_usd?: number | null };
interface Verification { connector: string; title: string; url: string; at: string; entity: string; entity_label: string; message: string }
interface Payload {
  query: string; status: string; depth: string; max_depth: number; mode: { id: string; title: string; help: string };
  headline: string; counts: Record<string, number>; progress: { step: string; status: string; note?: string }[];
  identities: Identity[]; platforms: PlatformRow[]; next_actions: Action[]; verification: Verification[];
  generated: { names: string[]; candidates: Parameters<typeof CandidateTable>[0]["items"]; testable: number };
  searched: { runs: { at: string; connector: string; transform: string; input: string; sent: string; destinations: string[];
    status: string; message: string; trigger: string }[]; total_runs: number;
    by_connector: { title: string; runs: number; statuses: Record<string, number> }[] };
  rejected: Member[]; decisions: { at: string; action: string; note?: string; label?: string }[];
  log: LogItem[]; paid_spend_usd: number; legend: Record<string, string>; disclaimer: string;
  provider: ProviderInfo; proposals: Proposal[]; notes: Note[]; budget_hold: BudgetHold | null; costs: Costs;
  discovery: Discovery; identifiers: { items: IdentifierRow[]; counts: Record<string, number>; states: Record<string, string> };
  source_kinds: Record<string, string>;
}

const STEP_ICON: Record<string, string> = { done: "✓", running: "●", pending: "○", skipped: "–" };

function Why({ m }: { m: Member }) {
  const x = m.explanation;
  if (x) {
    return (
      <div className="small" style={{ marginTop: 6 }}>
        <div><b>Confidence: {x.confidence}</b></div>
        {x.for.map((r, i) => <div key={`f${i}`} className="reason support"><span className="w">+ {r.strength}</span><span>{r.text}</span></div>)}
        {x.against.map((r, i) => <div key={`a${i}`} className="reason contradiction"><span className="w">− {r.strength}</span><span>{r.text}</span></div>)}
        {!x.for.length && !x.against.length && <span className="faint">No evidence either way yet.</span>}
        {m.note && <div className="muted" style={{ marginTop: 4 }}>{m.note}</div>}
        <div className="faint" style={{ marginTop: 4 }}>{x.rule}</div>
      </div>
    );
  }
  return (
    <div className="small" style={{ marginTop: 6 }}>
      {m.support.map((r, i) => <div key={`s${i}`} className="reason support"><span className="w">+{r.weight}</span><span>{r.text}</span></div>)}
      {m.contradictions.map((r, i) => <div key={`c${i}`} className="reason contradiction"><span className="w">{r.weight}</span><span>{r.text}</span></div>)}
      {!m.support.length && !m.contradictions.length && <span className="faint">No evidence either way yet.</span>}
      {m.note && <div className="muted" style={{ marginTop: 4 }}>{m.note}</div>}
    </div>
  );
}

function IdentityCard({ g, onOpen }: { g: Identity; onOpen: (id: string) => void }) {
  const [open, setOpen] = useState<string | null>(g.members[0]?.id ?? null);
  return (
    <div className="src-card" style={{ cursor: "default" }}>
      <div className="row between">
        <b>Candidate {g.letter}: {g.title}</b>
        <span className="row" style={{ gap: 6 }}><Badge kind={g.verdict_kind}>{g.verdict_title}</Badge><span className="small faint">{g.score}</span></span>
      </div>
      {g.grouped_because.length > 0 && <div className="small faint">Grouped because {g.grouped_because.join("; ")}.</div>}
      {g.members.map((m) => (
        <div key={m.id} style={{ borderTop: "1px dashed var(--line)", paddingTop: 6 }}>
          <div className="row between wrap">
            <span><a onClick={() => onOpen(m.id)} style={{ cursor: "pointer" }}>{m.label}</a> <span className="small faint">{m.type_title}</span>
              {m.url && <> · <Ext href={m.url}>open ↗</Ext></>}</span>
            <span className="row" style={{ gap: 6 }}>
              <Badge kind={m.verdict_kind}>{m.verdict_title}</Badge>
              <button className="btn ghost sm" onClick={() => setOpen(open === m.id ? null : m.id)}>{open === m.id ? "hide why" : "why?"}</button>
            </span>
          </div>
          <div className="row wrap small" style={{ gap: 6 }}>
            <EvidenceClass cls={m.origin.class} /><span className="faint">{m.origin.title}</span>
            {m.origin.source_kind && <Badge>{m.origin.source_kind.replace(/_/g, " ")}</Badge>}
            {m.explanation && <Badge kind={m.verdict_kind}>Confidence: {m.explanation.confidence}</Badge>}
            {m.independent_signals.length > 0 && <span className="faint">· {m.independent_signals.length} independent signal(s)</span>}
          </div>
          {open === m.id && <Why m={m} />}
        </div>
      ))}
    </div>
  );
}

export default function AssistantView({ invId, version, onOpen }: { invId: string; version: number; onOpen: (id: string) => void }) {
  const { data, error, reload } = useApi<Payload>(inv(invId, "/assistant"), [Math.floor(version / 3)]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [platFilter, setPlatFilter] = useState<"found" | "all" | "manual">("found");
  const [preview, setPreview] = useState<string | null>(null);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;

  async function act(a: Action, confirmPaid = false) {
    setBusy(a.id);
    setErr(null);
    try {
      const r = await api.post<{ needs_confirmation?: boolean }>(inv(invId, "/assistant/actions"), { action_id: a.id, confirm_paid: confirmPaid });
      if (r.needs_confirmation) setConfirm(a.id);
      else setConfirm(null);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }
  async function manualOnly(connector: string) {
    await api.put(`/api/sources/${connector}`, { manual_only: true });
    await reload();
  }
  const money = data.costs?.money;
  const big = (a: Action) => (a.estimated_cost_usd ?? 0) >= 0.05 || (a.transforms?.length ?? 0) > 8;
  const c = data.counts;
  const plats = data.platforms.filter((p) => platFilter === "all" ? p.check !== "manual" : platFilter === "manual" ? p.check === "manual"
    : p.check === "found");
  const chats = data.log.filter((x) => x.kind === "chat");
  const pendingOutside = data.proposals.filter((p) => p.status === "pending" && !chats.some((c) => (c.proposals ?? []).some((q) => q.proposal_id === p.proposal_id)));
  return (
    <div className="page wide assistant">
      <div className="row between wrap">
        <div>
          <h1 style={{ marginBottom: 4 }}>AI investigation assistant</h1>
          <div className="small muted">Query: <b>{data.query}</b> · mode {data.mode.title} (depth {data.depth}, up to {data.max_depth} hops)
            {" "}· estimated cost {fmtMoney(data.costs.totals.estimated, money)}
            {data.costs.budget.limit_usd !== null && ` of ${fmtMoney(data.costs.budget.limit_usd, money)} budget`}</div>
        </div>
        <button className="btn sm" onClick={() => void reload()}>Refresh</button>
      </div>
      <div className={`notice ${data.counts.strong || data.counts.confirmed ? "" : "warn"}`} style={{ margin: "12px 0" }}>{data.headline}</div>
      {data.budget_hold && <BudgetHoldBanner invId={invId} hold={data.budget_hold} money={money} onDone={() => void reload()} />}
      <ErrorBox error={err} />

      <h3 style={{ marginTop: 6 }}>AI assistant</h3>
      <ChatPanel invId={invId} turns={chats} provider={data.provider} proposals={data.proposals} money={money} onChange={() => void reload()} />
      {pendingOutside.length > 0 && <div className="col" style={{ gap: 8, marginTop: 8 }}>
        {pendingOutside.map((p) => <ProposalCard key={p.proposal_id} invId={invId} p={p} onDone={() => void reload()} />)}</div>}

      <div className="grid2">
        <div className="card pad">
          <h4>Progress</h4>
          {data.progress.map((p) => (
            <div key={p.step} className={`step ${p.status}`}><span className="ico">{STEP_ICON[p.status] ?? "○"}</span>{p.step}
              {p.note && <span className="faint small"> · {p.note}</span>}</div>
          ))}
        </div>
        <div className="kpis">
          <div className="kpi"><b>{c.identities}</b><span>candidate identities</span></div>
          <div className="kpi"><b>{c.strong + c.confirmed}</b><span>strong / confirmed</span></div>
          <div className="kpi"><b>{c.likely + c.possible}</b><span>likely / possible</span></div>
          <div className="kpi"><b>{c.insufficient}</b><span>insufficient evidence</span></div>
          <div className="kpi"><b>{c.platform_found}</b><span>platform matches</span></div>
          <div className="kpi"><b>{c.websites + c.documents}</b><span>web references</span></div>
          <div className="kpi"><b>{c.organizations}</b><span>organizations</span></div>
          <div className="kpi"><b>{c.domains}</b><span>domains</span></div>
        </div>
      </div>

      <h3>Next best action</h3>
      {data.next_actions.length === 0 && <p className="small muted">Nothing to suggest right now.</p>}
      <div className="col" style={{ gap: 8 }}>
        {data.next_actions.slice(0, 10).map((a, i) => (
          <div key={a.id} className={`card pad action${i === 0 ? " first" : ""}`}>
            <div className="row between wrap">
              <b>{a.title}</b>
              <span className="row" style={{ gap: 6 }}><Badge kind={VALUE_KIND[a.expected_value]}>Potential value: {a.potential_value ?? a.expected_value}</Badge>
                <CostTier tier={a.cost_tier} />
                {a.estimated_cost_usd !== undefined && <span className="small faint">est. {fmtMoney(a.estimated_cost_usd, money, "—")}</span>}</span>
            </div>
            <div className="small muted">{a.reason}</div>
            {a.handles && <div className="small faint mono">{a.handles.join(" · ")}</div>}
            <div className="row wrap" style={{ marginTop: 6, gap: 6 }}>
              {a.kind === "verification" && <>
                <Ext href={a.url ?? ""}><span className="btn sm">Open in browser ↗</span></Ext>
                {a.connector && <button className="btn sm" onClick={() => void manualOnly(a.connector!)}>Mark source manual-only</button>}
              </>}
              {a.kind === "review" && a.entity_id && <button className="btn sm" onClick={() => onOpen(a.entity_id!)}>Review in graph</button>}
              {a.kind === "jurisdiction" && a.url && <a className="btn sm" href={a.url}>Open the country page</a>}
              {a.executable && !a.requires_confirmation && (preview === a.id ? (
                <span className="row small" style={{ gap: 6 }}>You're about to run {a.transforms?.length ?? 1} source(s), estimated
                  {" "}{fmtMoney(a.estimated_cost_usd ?? 0, money)}.
                  <button className="btn primary sm" onClick={() => { setPreview(null); void act(a); }}>Run</button>
                  <button className="btn ghost sm" onClick={() => setPreview(null)}>Cancel</button></span>
              ) : (
                <button className="btn primary sm" disabled={busy === a.id} onClick={() => big(a) ? setPreview(a.id) : void act(a)}>
                  {busy === a.id ? <Spinner /> : null}{a.kind === "fallback" ? "Try another configured source" : "Run"}</button>))}
              {a.kind === "advice" && a.url && <a className="btn sm" href={a.url}>Open Data Sources</a>}
              {a.requires_confirmation && (confirm === a.id ? (
                <span className="row small" style={{ gap: 6 }}>This uses your paid subscription.
                  <button className="btn warn sm" onClick={() => void act(a, true)}>Confirm and run</button>
                  <button className="btn ghost sm" onClick={() => setConfirm(null)}>Cancel</button></span>
              ) : a.executable ? <button className="btn sm" onClick={() => setConfirm(a.id)}>Run (paid)…</button>
                : <span className="small faint">Paid sources are switched off in Settings</span>)}
            </div>
          </div>
        ))}
      </div>

      <h3>Identity discovery</h3>
      <DiscoveryStages d={data.discovery} onOpen={onOpen} />

      <h3>Identifiers</h3>
      <p className="small muted" style={{ marginTop: 0 }}>Every identifier with its state: Provided by you · Generated (hypothesis) · Discovered in a
        search result · Observed by a source · Possible match · Verified by you · Rejected. Generated and discovered are never mixed up.</p>
      <IdentifiersTable items={data.identifiers.items} counts={data.identifiers.counts} states={data.identifiers.states} onOpen={onOpen} />

      <h3>Cost</h3>
      <CostDashboard c={data.costs} />

      <h3>Notes</h3>
      <NotesPanel invId={invId} notes={data.notes} onChange={() => void reload()} />

      <h3>Candidate identities</h3>
      <p className="small muted" style={{ marginTop: 0 }}>Grouped profiles with the evidence for and against each. A name, a similar
        handle, a similar photo or the same city alone never makes a match: those stay at “Insufficient evidence”.</p>
      {data.identities.length === 0 ? <p className="small muted">No candidate profile yet.</p> : (
        <div className="grid2">{data.identities.slice(0, 24).map((g) => <IdentityCard key={g.id} g={g} onOpen={onOpen} />)}</div>
      )}

      <div className="row between wrap" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Platforms</h3>
        <div className="row">{(["found", "all", "manual"] as const).map((k) => (
          <button key={k} className={`btn sm ${platFilter === k ? "primary" : ""}`} onClick={() => setPlatFilter(k)}>
            {k === "found" ? "Found" : k === "all" ? "All checks" : "Check by hand"}</button>))}</div>
      </div>
      <div className="card" style={{ marginTop: 8, maxHeight: 420, overflow: "auto" }}>
        <table className="data">
          <thead><tr><th>Platform</th><th>Candidate</th><th>Status</th><th>Evidence</th><th>Identity</th></tr></thead>
          <tbody>{plats.slice(0, 300).map((p, i) => (
            <tr key={`${p.platform}:${p.candidate}:${i}`}>
              <td>{p.url ? <Ext href={p.url}>{p.platform}</Ext> : p.platform}</td>
              <td className="mono small">{p.candidate}<div className="faint">{p.candidate_origin}</div></td>
              <td><RunStatus status={p.check === "found" ? "found" : p.check === "manual" ? "manual" : p.check} /><div className="small faint">{p.status}</div></td>
              <td className="small muted">{p.evidence}</td>
              <td>{p.verdict ? <Badge kind={p.verdict_kind}>{p.verdict}</Badge> : <span className="faint small">—</span>}
                {p.account_id && <div><a className="small" onClick={() => onOpen(p.account_id!)} style={{ cursor: "pointer" }}>open</a></div>}</td>
            </tr>))}
            {plats.length === 0 && <tr><td colSpan={5} className="small faint">Nothing here yet.</td></tr>}</tbody>
        </table>
      </div>

      {data.generated.candidates.length > 0 && (
        <>
          <h3>Usernames derived from the name</h3>
          <p className="small muted" style={{ marginTop: 0 }}>Hypotheses from common handle patterns, never evidence. An account found under one
            of them is only a lead: the handle came from the name, so it says nothing about who owns the account.</p>
          <div className="card"><CandidateTable items={data.generated.candidates} showTested /></div>
        </>
      )}

      <h3>What was searched</h3>
      <p className="small muted" style={{ marginTop: 0 }}>{data.searched.total_runs} source runs: every one with what was sent and where.
        Assistant actions and AI tool calls are listed below them.</p>
      <details><summary className="small">By source ({data.searched.by_connector.length})</summary>
        <table className="data"><tbody>{data.searched.by_connector.map((b) => (
          <tr key={b.title}><td>{b.title}</td><td className="small">{b.runs} run(s)</td>
            <td className="small muted">{Object.entries(b.statuses).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ")}</td></tr>))}</tbody></table>
      </details>
      <details><summary className="small">Every run ({data.searched.runs.length} most recent)</summary>
        <table className="data"><thead><tr><th>When</th><th>Source</th><th>Input</th><th>Sent → to</th><th>Outcome</th></tr></thead>
          <tbody>{data.searched.runs.map((r, i) => (
            <tr key={i}><td className="small faint">{fmtDate(r.at)}</td><td className="small">{r.connector}<div className="faint">{r.trigger}</div></td>
              <td className="small">{r.input}</td><td className="small muted">{r.sent} → {r.destinations.join(", ") || "local"}</td>
              <td><RunStatus status={r.status} /><div className="small faint">{r.message}</div></td></tr>))}</tbody></table>
      </details>
      <details><summary className="small">Assistant actions and your decisions ({data.log.filter((x) => x.kind === "action").length + data.decisions.length})</summary>
        <ul className="small">
          {data.log.filter((x) => x.kind === "action").map((x) => <li key={x.seq}>{fmtDate(x.at)} · <b>{x.title}</b> by {x.by}
            {x.why ? ` (“${x.why}”)` : ""} — {x.reason}</li>)}
          {data.decisions.map((d, i) => <li key={`d${i}`}>{fmtDate(d.at)} · you: {d.action} {d.label ?? ""} {d.note ?? ""}</li>)}
        </ul></details>
      {data.rejected.length > 0 && <details><summary className="small">Rejected candidates ({data.rejected.length})</summary>
        <ul className="small">{data.rejected.map((r) => <li key={r.id}>{r.label} — {r.verdict_title}</li>)}</ul></details>}
      <p className="small faint" style={{ marginTop: 14 }}>{Object.entries(data.legend).map(([k, v]) => `${k}: ${v}`).join(" · ")}. {data.disclaimer}</p>
    </div>
  );
}
