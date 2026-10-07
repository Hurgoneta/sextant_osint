import { useState } from "react";
import { api, inv } from "../api";
import type { InvestigationInfo } from "../types";
import { ErrorBox, Spinner } from "../ui";

export default function ReportView({ invId, info }: { invId: string; info: InvestigationInfo }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function get(fmt: "html" | "json" | "csv", open = false) {
    setBusy(fmt);
    setError(null);
    try {
      const blob = await api.download(inv(invId, `/export/${fmt}`));
      const url = URL.createObjectURL(blob);
      if (open) {
        window.open(url, "_blank", "noopener");
      } else {
        const a = document.createElement("a");
        a.href = url;
        a.download = `sextant-${invId}.${fmt}`;
        a.click();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="page">
      <h1>Intelligence report</h1>
      <p className="sub">
        Target, identifiers, executive summary, identity candidates, accounts, websites, domains, organizations, documents,
        publications, public records, digital footprint, timeline, correlations, contradictions, coverage, unavailable and paid
        sources, recommendations and the audit log. Every claim links to its sources in the appendix.
      </p>
      <ErrorBox error={error} />
      <div className="grid3">
        <div className="card pad col">
          <b>HTML report</b>
          <span className="small muted">Self-contained, script-free. Print it to PDF from the browser.</span>
          <div className="row">
            <button className="btn primary" disabled={!!busy} onClick={() => void get("html", true)}>{busy === "html" ? <Spinner /> : "Open"}</button>
            <button className="btn" disabled={!!busy} onClick={() => void get("html")}>Download</button>
          </div>
        </div>
        <div className="card pad col">
          <b>JSON (complete)</b>
          <span className="small muted">Entities, properties with observation ids, relationships with evidence, sources, transform runs, coverage, decisions.</span>
          <button className="btn" disabled={!!busy} onClick={() => void get("json")}>{busy === "json" ? <Spinner /> : "Download JSON"}</button>
        </div>
        <div className="card pad col">
          <b>CSV (entities)</b>
          <span className="small muted">One row per entity with confidence, sources and evidence. Formula-safe.</span>
          <button className="btn" disabled={!!busy} onClick={() => void get("csv")}>{busy === "csv" ? <Spinner /> : "Download CSV"}</button>
        </div>
      </div>
      {info.purpose && <div className="notice" style={{ marginTop: 18 }}><b>Recorded purpose:</b> {info.purpose}</div>}
      <p className="small faint" style={{ marginTop: 18 }}>
        Exports leave the encrypted store — keep them safe. A year of birth you entered is local-only and is never exported.
      </p>
    </div>
  );
}
