import { useState } from "react";
import { api } from "../api";
import { AIProvidersCard, PricingCard, ResetPanel, SearchProvidersCard } from "../components/SettingsPanels";
import { ExportCatalogButton, ImportCatalogDialog, ResetCatalogDialog } from "../components/Catalog";
import { Badge, ErrorBox, Spinner, useApi } from "../ui";

interface Setting { key: string; label: string; help: string; kind: string; default: unknown; choices: string[]; value: unknown; source: string }
interface Payload { settings: Setting[]; data_dir: string; encryption: string }

export default function Settings() {
  const { data, error, setData } = useApi<Payload>("/api/settings");
  const meta = useApi<{ privacy: string; database: string }>("/api/meta");
  const [err, setErr] = useState<string | null>(null);
  const [wipe, setWipe] = useState("");
  const [reset, setReset] = useState(false);
  const [importing, setImporting] = useState(false);
  async function set(key: string, value: unknown) {
    try {
      setData(await api.put<Payload>("/api/settings", { values: { [key]: value } }));
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function doWipe() {
    try {
      await api.post("/api/data/wipe", { confirm: wipe });
      window.location.hash = "#/";
      window.location.reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <h1>Settings</h1>
      <p className="sub">API keys are configured per source under <a href="#/sources">Data sources</a>.</p>
      <ErrorBox error={err} />
      <div className="card pad col" style={{ gap: 16 }}>
        {data.settings.map((s) => (
          <div key={s.key}>
            <div className="row between"><b>{s.label}</b><Badge>{s.source}</Badge></div>
            <div className="small muted" style={{ margin: "2px 0 6px" }}>{s.help}</div>
            {s.kind === "bool" ? (
              <label className="check"><input type="checkbox" checked={!!s.value} onChange={(e) => void set(s.key, e.target.checked)} />enabled</label>
            ) : s.kind === "choice" ? (
              <select value={String(s.value)} onChange={(e) => void set(s.key, e.target.value)} style={{ maxWidth: 200 }}>
                {s.choices.map((c) => <option key={c}>{c}</option>)}
              </select>
            ) : (
              <input type={s.kind === "int" ? "number" : "text"} defaultValue={String(s.value ?? "")} style={{ maxWidth: 360 }}
                onBlur={(e) => e.target.value !== String(s.value ?? "") && void set(s.key, e.target.value)} />
            )}
          </div>
        ))}
      </div>
      <h3 style={{ marginTop: 26 }}>AI providers</h3>
      <AIProvidersCard />
      <h3 style={{ marginTop: 26 }}>Web search providers</h3>
      <SearchProvidersCard />
      <h3 style={{ marginTop: 26 }}>Pricing</h3>
      <PricingCard />
      <h3 style={{ marginTop: 26 }}>Privacy & storage</h3>
      <div className="card pad col">
        <p style={{ margin: 0 }}>{meta.data?.privacy}</p>
        <div className="kv">
          <span className="k">Data folder</span><span className="mono">{data.data_dir}</span>
          <span className="k">Database</span><span className="mono">{meta.data?.database}</span>
          <span className="k">Encryption</span><span>{data.encryption === "passphrase" ? "Key derived from SEXTANT_PASSPHRASE (scrypt)" : "Random key file next to the database — set SEXTANT_PASSPHRASE for stronger protection"}</span>
        </div>
      </div>
      <h3 style={{ marginTop: 26 }}>Catalog</h3>
      <div className="card pad col">
        <span className="small">
          <b>Export catalog</b> saves everything you added or changed in the catalog to a JSON file: your sources and links, categories, platforms,
          their relationships, your on/off changes to built-in sources and ignored audit suggestions. API keys, investigations and settings are
          never in it. <b>Import catalog</b> reads such a file back (you see a preview first; what you already have is skipped).
        </span>
        <span className="small">
          <b>Reset catalog to default</b> removes all of that, plus saved connector searches. Built-in sources come back exactly as shipped with
          this version. Investigations, API keys and settings are kept. You see what will be removed, and can export a backup, before anything
          happens.
        </span>
        <div className="row wrap">
          <ExportCatalogButton />
          <button className="btn" onClick={() => setImporting(true)}>⤒ Import catalog…</button>
          <button className="btn warn" onClick={() => setReset(true)}>Reset catalog to default…</button>
        </div>
      </div>
      {reset && <ResetCatalogDialog onClose={() => setReset(false)} onDone={() => setReset(false)} />}
      {importing && <ImportCatalogDialog onClose={() => setImporting(false)} onDone={() => setImporting(false)} />}
      <h3 style={{ marginTop: 26, color: "var(--bad)" }}>Hard reset</h3>
      <ResetPanel />
      <h3 style={{ marginTop: 26, color: "var(--bad)" }}>Delete everything</h3>
      <div className="card pad col">
        <span className="small">Deletes every investigation, cached response, stored API key, your catalog additions and the encryption key. This is
          not the catalog reset above: it erases all local data. This cannot be undone.</span>
        <div className="row">
          <input type="text" placeholder="Type DELETE EVERYTHING" value={wipe} onChange={(e) => setWipe(e.target.value)} style={{ maxWidth: 260 }} />
          <button className="btn bad" disabled={wipe !== "DELETE EVERYTHING"} onClick={() => void doWipe()}>Delete all local data</button>
        </div>
      </div>
    </div>
  );
}
