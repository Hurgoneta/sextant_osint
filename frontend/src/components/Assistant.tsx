// The assistant's shared pieces: the plan shown before an investigation starts, evidence/verdict badges, and the AI
// provider card of Settings. The Assistant tab itself is views/AssistantView.tsx.

import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, ErrorBox, Ext, SourceStatus, Spinner, useDebounced } from "../ui";

export const VALUE_KIND: Record<string, string> = { high: "good", medium: "info", low: "" };
export const TIER_KIND: Record<string, string> = { LOCAL: "", FREE: "good", "API KEY": "warn", PAID: "violet", MANUAL: "" };
export const CLASS_KIND: Record<string, string> = { provided: "info", observed: "good", derived: "", generated: "warn", inferred: "violet" };

export function CostTier({ tier }: { tier: string }) {
  return <Badge kind={TIER_KIND[tier] ?? ""}>{tier}</Badge>;
}

export function EvidenceClass({ cls }: { cls: string }) {
  const titles: Record<string, string> = { provided: "provided by you", observed: "observed", derived: "derived",
    generated: "generated (hypothesis)", inferred: "inferred" };
  return <Badge kind={CLASS_KIND[cls] ?? ""}>{titles[cls] ?? cls}</Badge>;
}

export const DEPTH_MODE: Record<string, string> = { "1": "quick", "2": "balanced", "3": "deep", "4": "deep", custom: "balanced" };

interface PlanSource { name: string; title: string; category_title: string; status: string; note: string; cost_tier: string;
  value: string; reason: string; auto: boolean; sends: string; destinations: string[] }
interface PlanStep { phase: string; title: string; reason: string; cost_tier?: string; sources?: PlanSource[]; input?: string;
  jurisdiction?: string; searchable?: number; manual_count?: number }
interface Candidate { handle: string; pattern_title: string; reading: string; status: string; explanation: string;
  platforms_valid: string[]; platforms_valid_count: number; tested?: { checks: number; found: number; platforms_found: string[] } | null }
interface Jur { code: string; name: string; why: string[]; status: string; connectors: PlanSource[];
  automated: { title: string }[]; manual: { title: string; url: string; nature: string; evidence: string }[];
  yours: { title: string }[]; coverage: { group: string; rating: string; kind: string }[]; gaps: string[] }
export interface PlanT {
  mode: { id: string; title: string; help: string; depth: string }; modes: Record<string, { title: string; help: string; depth: string }>;
  identifiers: { type: string; label: string; origin: string; note: string }[];
  name_analysis: { names: string[]; readings: string[]; note: string } | null;
  candidates: Candidate[]; candidates_testable: number; jurisdictions: Jur[]; jurisdiction_note: string | null;
  steps: PlanStep[]; platforms: { searchable: number; manual: { name: string; category: string }[]; manual_count: number; note: string } | null;
  not_used: PlanSource[]; cost: { free: number; api_key: number; paid: number; paid_blocked: number; allow_paid: boolean; cap_usd: number };
}

export function CandidateTable({ items, showTested }: { items: Candidate[]; showTested?: boolean }) {
  return (
    <table className="data">
      <thead><tr><th>Candidate handle</th><th>Pattern</th><th>Assumes</th><th>Status</th>{showTested && <th>Tested</th>}</tr></thead>
      <tbody>{items.map((c) => (
        <tr key={c.handle}>
          <td className="mono">{c.handle}<div className="small faint">valid on {c.platforms_valid_count} checkable platform(s)</div></td>
          <td className="small">{c.pattern_title}</td>
          <td className="small muted">{c.reading}</td>
          <td><Badge kind="warn">{c.status}</Badge></td>
          {showTested && <td className="small">{c.tested ? <>{c.tested.checks} check(s) · <b>{c.tested.found}</b> found
            {c.tested.platforms_found.length > 0 && <div className="faint">{c.tested.platforms_found.join(", ")}</div>}</> : <span className="faint">not tested</span>}</td>}
        </tr>))}</tbody>
    </table>
  );
}

export function AssistantPlan({ text, countries, depth }: { text: string; countries: string[]; depth: string }) {
  const mode = DEPTH_MODE[depth] ?? "balanced";
  const key = useDebounced(JSON.stringify({ text, countries, mode }), 400);
  const [plan, setPlan] = useState<PlanT | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [openStep, setOpenStep] = useState<string | null>(null);
  useEffect(() => {
    const q = JSON.parse(key) as { text: string; countries: string[]; mode: string };
    if (!q.text.trim()) {
      setPlan(null);
      return;
    }
    let live = true;
    setLoading(true);
    api.post<PlanT>("/api/assistant/plan", q).then((p) => live && (setPlan(p), setErr(null)))
      .catch((e) => live && setErr((e as Error).message)).finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [key]);
  if (err) return <ErrorBox error={err} />;
  if (!plan) return loading ? <Spinner /> : null;
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="small muted"><b>{plan.mode.title}</b> (depth {plan.mode.depth}): {plan.mode.help}</div>
      {plan.name_analysis && (
        <div>
          <b className="small">Name analysis</b>
          <div className="small muted">{plan.name_analysis.note} Readings: {plan.name_analysis.readings.join(" · ")}.</div>
          <details style={{ marginTop: 6 }}><summary className="small">{plan.candidates.length} candidate usernames (hypotheses, not evidence)
            {plan.candidates_testable ? ` · up to ${plan.candidates_testable} testable after you approve` : " · not tested in Quick mode"}</summary>
            <CandidateTable items={plan.candidates} /></details>
        </div>
      )}
      {plan.jurisdiction_note && <div className="small muted">ℹ {plan.jurisdiction_note}</div>}
      {plan.jurisdictions.map((j) => (
        <div key={j.code} className="card pad">
          <div className="row between wrap"><b>{j.name} <span className="faint mono small">{j.code}</span></b>
            <Badge kind={j.status.startsWith("selected") ? "info" : "warn"}>{j.status}</Badge></div>
          {j.why.length > 0 && <div className="small faint">Because: {j.why.join(", ")}</div>}
          <div className="row wrap small" style={{ gap: 6, marginTop: 6 }}>
            {j.coverage.map((c) => <Badge key={c.group} kind={c.kind}>{c.group}: {c.rating}</Badge>)}
          </div>
          <div className="small" style={{ marginTop: 6 }}>
            {j.connectors.length} country connector(s){j.connectors.length > 0 && `: ${j.connectors.map((c) => c.title).join(", ")}`} ·
            {" "}{j.automated.length} automated official source(s) · {j.manual.length} official source(s) to open yourself
            {j.yours.length > 0 && ` · ${j.yours.length} of yours`}
          </div>
          {j.manual.length > 0 && <details><summary className="small">Manual sources</summary>
            <ul className="small">{j.manual.map((m) => <li key={m.title}><Ext href={m.url}>{m.title}</Ext> <span className="faint">· {m.nature} · {m.evidence}</span></li>)}</ul></details>}
          {j.gaps.length > 0 && <div className="small" style={{ color: "var(--warn)" }}>{j.gaps.join(" ")}</div>}
        </div>
      ))}
      <div>
        <b className="small">Steps</b>
        <ol className="small" style={{ margin: "6px 0", paddingLeft: 20 }}>
          {plan.steps.map((s, i) => (
            <li key={i} style={{ marginBottom: 6 }}>
              <b>{s.title}</b> {s.cost_tier && <CostTier tier={s.cost_tier} />} <span className="muted">— {s.reason}</span>
              {s.sources && s.sources.length > 0 && (
                <>
                  {" "}<button className="btn ghost sm" onClick={() => setOpenStep(openStep === s.title ? null : s.title)}>
                    {openStep === s.title ? "hide" : `${s.sources.length} source(s)`}</button>
                  {openStep === s.title && (
                    <table className="data" style={{ marginTop: 4 }}>
                      <thead><tr><th>Source</th><th>Cost</th><th>Expected value</th><th>Why</th><th>Receives</th></tr></thead>
                      <tbody>{s.sources.map((src) => (
                        <tr key={src.name}><td>{src.title}<div className="faint">{src.category_title}{!src.auto && " · on request"}</div></td>
                          <td><CostTier tier={src.cost_tier} /></td><td><Badge kind={VALUE_KIND[src.value]}>{src.value}</Badge></td>
                          <td className="muted">{src.reason}</td><td className="faint">{src.sends} → {src.destinations.join(", ") || "local"}</td></tr>))}</tbody>
                    </table>
                  )}
                </>
              )}
            </li>
          ))}
        </ol>
      </div>
      <div className="row wrap small" style={{ gap: 6 }}>
        <Badge kind="good">{plan.cost.free} free / local</Badge>
        {plan.cost.api_key > 0 && <Badge kind="warn">{plan.cost.api_key} with your API keys</Badge>}
        {plan.cost.paid > 0 && <Badge kind="violet">{plan.cost.paid} paid (your subscription)</Badge>}
        <span className="muted">Paid sources: {plan.cost.allow_paid ? "allowed" : "switched off"} in Settings
          {plan.cost.cap_usd ? `, limit $${plan.cost.cap_usd} per investigation` : ""}.</span>
      </div>
      {plan.not_used.length > 0 && (
        <details><summary className="small">{plan.not_used.length} applicable source(s) that will not run, and why</summary>
          <table className="data"><tbody>{plan.not_used.map((s) => (
            <tr key={s.name}><td>{s.title}</td><td><SourceStatus status={s.status} /></td><td className="small muted">{s.note}</td></tr>))}</tbody></table>
        </details>
      )}
    </div>
  );
}
