import { useState } from "react";
import { inv } from "../api";
import type { Coverage } from "../types";
import { ErrorBox, RunStatus, Spinner, useApi } from "../ui";

const LABELS: [string, string][] = [
  ["queried", "Sources queried"], ["failed_or_blocked", "Unavailable / blocked"], ["requires_credentials", "Requiring credentials"],
  ["requires_subscription", "Requiring subscription"], ["disabled", "Disabled"], ["not_installed", "Not installed"],
  ["manual_only", "Manual only"], ["catalog_only", "Catalog only / retired"], ["excluded_by_policy", "Excluded by policy"],
  ["skipped", "Skipped"], ["not_applicable", "Not applicable"],
];

export default function CoverageView({ invId, version }: { invId: string; version: number }) {
  const { data, error } = useApi<Coverage>(inv(invId, "/coverage"), [Math.floor(version / 5)]);
  const [state, setState] = useState("");
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const rows = data.connectors.filter((c) => !state || c.state === state);
  const g = data.global;
  return (
    <div className="page wide">
      {g && (
        <div className="card pad" style={{ marginBottom: 14 }}>
          <h3 style={{ marginTop: 0, letterSpacing: ".08em" }}>INVESTIGATION COVERAGE</h3>
          <div className="grid3">
            <div className="kv">
              <span className="k">Countries evaluated</span><b>{g.countries.evaluated}</b>
              <span className="k">Countries with sources</span><b>{g.countries.with_sources}</b>
              <span className="k">With country connectors</span><b>{g.countries.with_country_connectors}</b>
              <span className="k">Countries with no connector</span><b>{g.countries.no_connector}</b>
            </div>
            <div className="kv">
              <span className="k">Platforms evaluated</span><b>{g.platforms.evaluated.toLocaleString()}</b>
              <span className="k">Platforms searched</span><b>{g.platforms.searched.toLocaleString()}</b>
              <span className="k">… accounts found on</span><b>{g.platforms.found}</b>
              <span className="k">Platforms unavailable</span><b>{g.platforms.unavailable}</b>
              <span className="k">Skipped (robots.txt / policy)</span><b>{g.platforms.skipped}</b>
              <span className="k">Not searched</span><b>{g.platforms.not_searched.toLocaleString()}</b>
            </div>
            <div className="kv">
              <span className="k">Public databases queried</span><b>{g.public_databases.queried}</b>
              <span className="k">Public databases failed</span><b>{g.public_databases.failed}</b>
              <span className="k">Public databases unavailable</span><b>{g.public_databases.unavailable}</b>
              <span className="k">Official links (your countries)</span><b>{g.public_databases.official_links_for_applicable_countries}</b>
              <span className="k">Commercial configured</span><b>{g.commercial.configured} <span className="faint small">({g.commercial.queried} queried)</span></b>
              <span className="k">Commercial not configured</span><b>{g.commercial.not_configured} <span className="faint small">+ {g.commercial.catalog_only} contact-provider</span></b>
            </div>
          </div>
          <p className="small muted" style={{ margin: "10px 0 6px" }}>{g.countries.note} {g.platforms.note}</p>
          {g.countries.applicable.length > 0 && (
            <table className="data">
              <thead><tr><th>Country</th><th>Coverage</th><th>Country connectors (this investigation)</th><th>Official links</th><th>Potential</th></tr></thead>
              <tbody>{g.countries.applicable.map((c) => (
                <tr key={c.code}><td><a href={`#/coverage/${c.code}`}>{c.name}</a></td><td className="small">{c.level_title}</td>
                  <td className="small">{c.connectors.length ? c.connectors.map((x) => `${x.title}: ${x.state.replace(/_/g, " ")}`).join(" · ") : "none"}</td>
                  <td>{c.official_links}{c.your_sources ? <span className="faint"> + {c.your_sources} yours</span> : null}</td><td>{c.potential_sources}</td></tr>))}</tbody>
            </table>
          )}
          <div className="notice" style={{ marginTop: 10 }}>{g.disclaimer}</div>
        </div>
      )}
      <div className="notice" style={{ marginBottom: 14 }}>{data.disclaimer}</div>
      <div className="kpis" style={{ marginBottom: 18 }}>
        {LABELS.map(([k, label]) => (
          <div key={k} className="kpi" style={{ cursor: "pointer", borderColor: state === stateFor(k) ? "var(--accent)" : undefined }}
            onClick={() => setState(state === stateFor(k) ? "" : stateFor(k))} title="Filter the table">
            <b>{data.summary[k] ?? 0}</b><span>{label}</span>
          </div>
        ))}
      </div>
      <div className="card">
        <table className="data">
          <thead><tr><th>Source</th><th>Category</th><th>Coverage state</th><th>Outcomes</th><th>Note</th></tr></thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.connector}>
                <td><a href={`#/sources/${c.connector}`}>{c.title}</a></td>
                <td className="small muted">{c.category_title}</td>
                <td><RunStatus status={c.state} /></td>
                <td className="small">{Object.entries(c.runs).map(([k, v]) => `${k.replace("_", " ")}: ${v}`).join(" · ") || "—"}
                  {c.queued ? ` · queued ${c.queued}` : ""}{c.running ? ` · running ${c.running}` : ""}</td>
                <td className="small muted wrapword">{c.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function stateFor(k: string): string {
  return ({ failed_or_blocked: "failed", requires_credentials: "needs_credentials", requires_subscription: "needs_subscription",
    manual_only: "manual", excluded_by_policy: "excluded" } as Record<string, string>)[k] ?? k;
}
