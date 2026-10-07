// Data Sources → Discover sources: candidates from the configured web-search provider, classified and checked against the
// catalog. Nothing is added until you press "Add…", which opens the normal source form, prefilled and marked Not verified.

import { useState } from "react";
import { api } from "../api";
import type { SourceDraft } from "../types";
import { Badge, ErrorBox, Ext, Spinner } from "../ui";

interface Candidate { id: string; host: string; url: string; title: string; snippet: string; rank: number; query: string; provider: string;
  score: number; status: string; draft: SourceDraft;
  classification: { category: string; nature: string; official: boolean; commercial: boolean; api: boolean; jurisdiction: string | null;
    signals: string[] };
  duplicates: { title: string; reasons: string[] }[] }
interface Result { ok?: boolean; message?: string; queries?: string[]; provider_title?: string; candidates: Candidate[];
  already_in_catalog?: Candidate[]; errors?: string[]; note?: string; audit?: { coverage: { group: string; rating: string }[] } | null;
  cost?: { requests: number; estimated_usd: number | null } }
interface Inspect { reachable: boolean; http_status: number | null; error: string | null; title: string; draft: SourceDraft;
  classification: Candidate["classification"]; duplicates: { title: string; reasons: string[]; strength: string }[]; already_exists: boolean }

export function DiscoverSources({ onAdd, onClose }: { onAdd: (d: SourceDraft) => void; onClose: () => void }) {
  const [request, setRequest] = useState("");
  const [jur, setJur] = useState("");
  const [cat, setCat] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Result | null>(null);
  const [ins, setIns] = useState<Inspect | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function discover() {
    setBusy(true);
    setErr(null);
    try {
      setRes(await api.post<Result>("/api/catalog/discover", { request, jurisdiction: jur, category: cat }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function inspect() {
    setBusy(true);
    setErr(null);
    try {
      setIns(await api.post<Inspect>("/api/catalog/inspect", { url, jurisdiction: jur, category: cat }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="card pad col" style={{ marginBottom: 16, borderColor: "var(--accent)" }}>
      <div className="row between"><b>Discover sources</b><button className="btn ghost sm" onClick={onClose}>✕</button></div>
      <span className="small muted">Ask for what is missing — e.g. “commercial people/contact data providers covering Sweden” or pick a
        jurisdiction to search its coverage gaps. Candidates come from your configured web-search provider, are classified by keywords and
        checked against the catalog. Nothing is added until you choose.</span>
      <div className="row wrap">
        <input type="text" placeholder="What are you looking for?" value={request} onChange={(e) => setRequest(e.target.value)} style={{ flex: 1, minWidth: 260 }} />
        <input type="text" placeholder="Jurisdiction (e.g. SE)" value={jur} onChange={(e) => setJur(e.target.value.toUpperCase())} style={{ maxWidth: 150 }} />
        <input type="text" placeholder="Category id (optional)" value={cat} onChange={(e) => setCat(e.target.value)} style={{ maxWidth: 200 }} />
        <button className="btn primary" disabled={busy || (!request.trim() && !jur.trim() && !cat.trim())} onClick={() => void discover()}>
          {busy ? <Spinner /> : null}Discover</button>
      </div>
      <div className="row wrap">
        <input type="text" placeholder="…or inspect one address: https://…" value={url} onChange={(e) => setUrl(e.target.value)} style={{ flex: 1, minWidth: 260 }} />
        <button className="btn" disabled={busy || !/^https?:\/\//i.test(url)} onClick={() => void inspect()}>Inspect</button>
      </div>
      <ErrorBox error={err} />
      {ins && (
        <div className="card pad col">
          <div className="row between wrap"><b>{ins.title}</b>
            <span className="row" style={{ gap: 6 }}><Badge kind={ins.reachable ? "good" : "warn"}>{ins.reachable ? `HTTP ${ins.http_status}` : `unreachable${ins.error ? `: ${ins.error}` : ""}`}</Badge>
              <Badge>{ins.classification.nature}</Badge>{ins.classification.api && <Badge kind="info">API hint</Badge>}</span></div>
          <div className="small muted">{ins.classification.signals.join(" · ")}</div>
          {ins.duplicates.length > 0 && <div className="small" style={{ color: "var(--warn)" }}>Already in the catalog? {ins.duplicates.map((d) => `${d.title} (${d.reasons.join(", ")})`).join("; ")}</div>}
          <div><button className="btn primary sm" onClick={() => onAdd(ins.draft)}>Add…</button></div>
        </div>
      )}
      {res && res.ok === false && <div className="notice warn small">{res.message}</div>}
      {res && res.ok !== false && (
        <>
          <span className="small faint">{res.provider_title} · queries: {(res.queries ?? []).join(" · ")}
            {res.cost && ` · ${res.cost.requests} request(s)`}{(res.errors ?? []).length > 0 && ` · ${(res.errors ?? []).length} failed`}</span>
          {res.audit && <div className="row wrap small" style={{ gap: 4 }}>{res.audit.coverage.map((c) => <Badge key={c.group}>{c.group}: {c.rating}</Badge>)}</div>}
          {res.candidates.length === 0 && <span className="small muted">No new candidates.</span>}
          {res.candidates.map((c) => (
            <div key={c.id} className="src-card" style={{ cursor: "default" }}>
              <div className="row between wrap"><b>{c.title}</b>
                <span className="row" style={{ gap: 6 }}><Badge>{c.classification.nature}</Badge><Badge>{c.classification.category.replace(/_/g, " ")}</Badge>
                  {c.classification.jurisdiction && <Badge kind="info">{c.classification.jurisdiction}</Badge>}<Badge kind="warn">{c.status}</Badge></span></div>
              <div className="small"><Ext href={c.url}>{c.host}</Ext> · rank #{c.rank} for “{c.query}”</div>
              <div className="small muted">{c.snippet}</div>
              <div className="small faint">{c.classification.signals.join(" · ")}</div>
              <div><button className="btn primary sm" onClick={() => onAdd(c.draft)}>Add…</button></div>
            </div>
          ))}
          {(res.already_in_catalog ?? []).length > 0 && <details><summary className="small">{(res.already_in_catalog ?? []).length} result(s) already in your catalog</summary>
            <ul className="small">{(res.already_in_catalog ?? []).map((c) => <li key={c.id}>{c.title} — {c.duplicates.map((d) => d.title).join(", ")}</li>)}</ul></details>}
          <span className="small faint">{res.note}</span>
        </>
      )}
    </div>
  );
}
