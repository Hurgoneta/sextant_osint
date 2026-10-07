import { useState } from "react";
import { OriginBadge } from "../components/Catalog";
import { ErrorBox, SourceStatus, Spinner, useApi } from "../ui";

interface Row { name: string; title: string; category_title: string; free: boolean; api: boolean; paid: boolean; public: boolean;
  status: string; access: string; inputs: string[]; jurisdictions: string[]; origin: "system" | "user"; automation: string }

const AUTOMATION: Record<string, string> = { connector: "connector", manual: "manual", none: "not automated", missing_connector: "connector missing" };
const FILTERS: [string, string][] = [["", "All"], ["free", "FREE"], ["api", "API"], ["paid", "PAID"], ["public", "PUBLIC"],
  ["manual", "MANUAL"], ["user", "YOURS"]];

export default function Matrix() {
  const { data, error } = useApi<{ rows: Row[]; disclaimer: string; origins: { system: number; user: number } }>("/api/coverage-matrix");
  const [q, setQ] = useState("");
  const [only, setOnly] = useState("");
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const keep = (r: Row) => !only || (only === "user" ? r.origin === "user" : only === "manual" ? r.automation !== "connector"
    : (r as unknown as Record<string, boolean>)[only]);
  const rows = data.rows.filter((r) => keep(r) &&
    (!q || `${r.title} ${r.category_title} ${r.jurisdictions.join(" ")}`.toLowerCase().includes(q.toLowerCase())));
  const tick = (b: boolean) => (b ? <span className="yes">✓</span> : <span className="faint">·</span>);
  return (
    <div className="page wide">
      <h1>Source coverage matrix</h1>
      <p className="sub">What the engine can and cannot investigate, computed from the same records as Data Sources ({data.origins.system} built-in
        {data.origins.user ? `, ${data.origins.user} yours` : ""}). FREE = no account needed · API = key, approval or a documented API · PAID = costs
        money (your own subscription) · PUBLIC = official public-record / government source · MANUAL = no connector runs it.</p>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <input type="search" placeholder="Filter by name, category or jurisdiction…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 300 }} />
        {FILTERS.map(([k, label]) => (
          <button key={k} className={`btn sm ${only === k ? "primary" : ""}`} onClick={() => setOnly(k)}>{label}</button>
        ))}
        <span className="small muted">{rows.length} sources</span>
      </div>
      <div className="card">
        <table className="data matrix">
          <thead><tr><th>Source</th><th>Category</th><th style={{ textAlign: "center" }}>FREE</th><th style={{ textAlign: "center" }}>API</th>
            <th style={{ textAlign: "center" }}>PAID</th><th style={{ textAlign: "center" }}>PUBLIC</th><th>Status</th><th>Automation</th>
            <th>Inputs</th><th>Jurisdiction</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name} className="clickable" onClick={() => (window.location.hash = `#/sources/${r.name}`)}>
                <td>{r.title} <OriginBadge origin={r.origin} /></td><td className="small muted">{r.category_title}</td>
                <td className="c">{tick(r.free)}</td><td className="c">{tick(r.api)}</td><td className="c">{tick(r.paid)}</td><td className="c">{tick(r.public)}</td>
                <td><SourceStatus status={r.status} /></td>
                <td className="small muted">{AUTOMATION[r.automation] ?? r.automation}</td>
                <td className="small muted">{r.inputs.join(", ")}</td>
                <td className="small muted">{r.jurisdictions.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small faint" style={{ marginTop: 12 }}>{data.disclaimer}</p>
    </div>
  );
}
