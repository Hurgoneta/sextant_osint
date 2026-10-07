import { useMemo, useState } from "react";
import { api, inv } from "../api";
import type { Check, EntitySummary } from "../types";
import { Badge, Confidence, Empty, ErrorBox, Ext, MatchBadge, RunStatus, Spinner, useApi } from "../ui";

interface Account extends EntitySummary {
  platform: string;
  platform_title: string;
  username: string;
  display_name: string;
  profile_url: string;
  category: string;
  thumb: string;
  found_by: string[];
  evidence: { description: string; weight: number }[];
  contradictions: { description: string; weight: number }[];
}

interface Payload {
  by_identifier: Record<string, Check[]>;
  accounts: Account[];
  check_summary: Record<string, number>;
  manual: { username: string; links: { platform: string; title: string; category: string; url: string; reason: string }[] }[];
}

export default function AccountsView({ invId, version, onOpen }: { invId: string; version: number; onOpen: (id: string) => void }) {
  const { data, error } = useApi<Payload>(inv(invId, "/accounts"), [Math.floor(version / 4)]);
  const [min, setMin] = useState(0);
  const [statusFilter, setStatusFilter] = useState("found");
  const [q, setQ] = useState("");
  const accounts = useMemo(() => (data?.accounts ?? []).filter((a) => a.confidence >= min &&
    (!q || `${a.label} ${a.display_name ?? ""}`.toLowerCase().includes(q.toLowerCase()))), [data, min, q]);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  async function investigate(id: string) {
    await api.post(inv(invId, `/entities/${id}/transforms`), { transforms: "auto", expand_levels: 1 });
  }
  return (
    <div className="page wide">
      <div className="kpis" style={{ marginBottom: 16 }}>
        <div className="kpi"><b>{data.accounts.length}</b><span>accounts in the graph</span></div>
        <div className="kpi"><b>{data.accounts.filter((a) => a.confidence >= 0.75 || a.status === "confirmed").length}</b><span>likely / confirmed the target</span></div>
        {Object.entries(data.check_summary).map(([k, v]) => (
          <div className="kpi" key={k}><b>{v}</b><span>site checks: {k.replace("_", " ")}</span></div>
        ))}
      </div>
      <p className="small muted">"Account exists" and "account belongs to the target" are different questions. Existence comes from the
        site check; identity comes from the evidence listed on each card.</p>
      <div className="row wrap" style={{ margin: "10px 0 14px" }}>
        <input type="search" placeholder="Filter accounts…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
        <span className="small muted">Min. confidence</span>
        <input type="range" min={0} max={0.9} step={0.05} value={min} onChange={(e) => setMin(Number(e.target.value))} style={{ width: 160 }} />
        <b className="small">{Math.round(min * 100)}%</b>
      </div>
      {accounts.length === 0 && <Empty>No accounts match.</Empty>}
      <div className="grid3">
        {accounts.slice(0, 300).map((a) => (
          <div key={a.id} className="acc-card">
            <div className="hd">
              {a.thumb ? <img src={a.thumb} alt="" className="thumb" /> : <div className="thumb" style={{ display: "grid", placeItems: "center", background: "var(--panel-2)" }}>{(a.platform_title || "?")[0]}</div>}
              <div style={{ minWidth: 0, flex: 1 }}>
                <b className="wrapword">{a.platform_title || a.platform}</b>
                <div className="small muted wrapword">{a.username}{a.display_name ? ` · ${a.display_name}` : ""}</div>
              </div>
            </div>
            <div className="row between"><MatchBadge e={a} /><Confidence value={a.confidence} /></div>
            <div className="ev">
              {a.evidence.slice(0, 5).map((r, i) => <div key={i}><span className="ok">✓</span> {r.description}</div>)}
              {a.contradictions.slice(0, 3).map((r, i) => <div key={`c${i}`}><span className="no">✗</span> {r.description}</div>)}
              {a.evidence.length === 0 && a.contradictions.length === 0 && <div><span className="q">?</span> No identifying evidence yet</div>}
            </div>
            <div className="small faint">Found by: {a.found_by.join(", ") || "—"}</div>
            <div className="row wrap">
              {a.profile_url && <Ext href={a.profile_url}><span className="btn sm">OPEN PROFILE</span></Ext>}
              <button className="btn sm" onClick={() => onOpen(a.id)}>SHOW IN GRAPH</button>
              <button className="btn sm primary" onClick={() => void investigate(a.id)}>INVESTIGATE ENTITY</button>
            </div>
          </div>
        ))}
      </div>

      <h3 style={{ marginTop: 28 }}>Site checks per identifier</h3>
      <div className="row" style={{ marginBottom: 8 }}>
        {["found", "all", "not_found", "blocked", "unknown", "skipped"].map((s) => (
          <button key={s} className={`btn sm ${statusFilter === s ? "primary" : ""}`} onClick={() => setStatusFilter(s)}>{s.replace("_", " ")}</button>
        ))}
      </div>
      {Object.entries(data.by_identifier).map(([ident, checks]) => {
        const rows = checks.filter((c) => statusFilter === "all" || c.status === statusFilter);
        return (
          <div key={ident} className="card" style={{ marginBottom: 12 }}>
            <div className="row between" style={{ padding: "10px 12px" }}>
              <b>{ident}</b>
              <span className="small muted">{checks.length} checks · {checks.filter((c) => c.status === "found").length} found</span>
            </div>
            <div style={{ maxHeight: 360, overflow: "auto" }}>
              <table className="data">
                <thead><tr><th>Platform</th><th>Category</th><th>Exists?</th><th>Target?</th><th>Detail</th></tr></thead>
                <tbody>
                  {rows.slice(0, 500).map((c, i) => (
                    <tr key={i}>
                      <td>{c.url ? <Ext href={c.url}>{c.platform_title || c.platform}</Ext> : c.platform_title || c.platform}</td>
                      <td className="small muted">{c.category}</td>
                      <td><RunStatus status={c.status} /></td>
                      <td>{c.account ? <a href="#" onClick={(e) => { e.preventDefault(); onOpen(c.account!.id); }}><Confidence value={c.account.confidence} /></a> : <span className="faint">—</span>}</td>
                      <td className="small muted wrapword">{c.detail} <span className="faint">{c.connector}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}

      {data.manual.length > 0 && (
        <>
          <h3 style={{ marginTop: 28 }}>Manual checks</h3>
          <p className="small muted">These platforms forbid automated lookups or hide profiles behind a login. Open the links yourself.</p>
          {data.manual.map((m) => (
            <div key={m.username} className="card pad" style={{ marginBottom: 10 }}>
              <b>{m.username}</b>
              <div className="row wrap" style={{ marginTop: 8 }}>
                {m.links.map((l) => <Ext key={l.platform} href={l.url}><Badge kind="info" title={l.reason}>{l.title} ↗</Badge></Ext>)}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
