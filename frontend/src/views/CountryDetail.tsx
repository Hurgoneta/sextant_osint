import { useMemo, useState } from "react";
import { api } from "../api";
import { AuditPanel, AutomationBadge, LinkCheckBadge, SourceForm, UserSourceActions } from "../components/Catalog";
import type { CountryDetailT, CountrySource, SourceDraft } from "../types";
import { Badge, ErrorBox, Ext, SourceStatus, Spinner, fmtDate, useApi } from "../ui";
import { LEVEL_KIND } from "./GlobalCoverage";

type FormState = { mode: "source" | "link"; initial?: SourceDraft | null; editId?: string } | null;

const VER: Record<string, [string, string]> = { ok: ["good", "link OK"], reachable_refuses_bots: ["info", "reachable (blocks bots)"],
  tls_problem: ["warn", "TLS issue"], unreachable: ["warn", "unreachable from here"], server_error: ["warn", "server error"], dead: ["bad", "dead link"] };
const HEALTH_KIND: Record<string, string> = { operational: "good", degraded: "warn", auth_required: "warn", subscription_required: "violet",
  temporarily_unavailable: "warn", deprecated: "bad", broken: "bad", not_publicly_searchable: "", country_restricted: "warn",
  excluded: "bad", unknown: "" };
const SEARCH_KIND: Record<string, string> = { searchable: "good", api_key_required: "warn", subscription_required: "violet",
  adapter_not_installed: "", adapter_off: "info", search_everything_only: "info", adult_off: "", no_rule: "", manual: "info", not_publicly_searchable: "", defunct: "bad", excluded: "bad", disabled: "" };

function Verification({ v }: { v?: { result: string; checked_at?: string } | null }) {
  const [k, l] = VER[v?.result ?? ""] ?? ["", "not checked"];
  return <Badge kind={k} title={v?.checked_at ? `checked ${fmtDate(v.checked_at)}` : undefined}>{l}</Badge>;
}

function SourceTable({ items, discovered }: { items: CountrySource[]; discovered?: boolean }) {
  return (
    <table className="data">
      <thead><tr><th>Source</th><th>Kind</th><th>{discovered ? "Dataset" : "Access"}</th><th>Automation</th><th>Link</th></tr></thead>
      <tbody>
        {items.map((s) => (
          <tr key={s.id}>
            <td><Ext href={s.url}>{s.title}</Ext>
              {s.notes && <div className="small faint">{s.notes}</div>}
              {s.person_data && <div className="small muted">Personal data: {s.person_data}</div>}
              {s.api && <div className="small"><Ext href={s.api}>API docs</Ext></div>}</td>
            <td className="small">{s.kind_title}
              {s.name_search && <div><Badge kind="info">name search</Badge></div>}
              {s.anonymized && <div><Badge>anonymised</Badge></div>}</td>
            <td className="small">{discovered ? <>{s.dataset_title}<div className="faint mono">{s.dataset_ref}</div></> : s.access_title}</td>
            <td className="small">{s.connector ? <a href={`#/sources/${s.connector}`}><Badge kind="good">{s.connector}</Badge></a> : <span className="faint">link only</span>}
              {s.connector_note && <div className="faint">{s.connector_note}</div>}</td>
            <td><Verification v={s.verification} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function CountryDetail({ code }: { code: string }) {
  const { data, error, reload } = useApi<CountryDetailT>(`/api/global-coverage/${code}`);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(null);
  const [audit, setAudit] = useState(false);
  const [auditKey, setAuditKey] = useState(0);
  const [groupFilter, setGroupFilter] = useState("");
  const grouped = useMemo(() => {
    const out: Record<string, CountrySource[]> = {};
    for (const s of data?.potential_sources ?? []) {
      if (groupFilter && s.group !== groupFilter) continue;
      (out[s.group_title] ??= []).push(s);
    }
    return out;
  }, [data, groupFilter]);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const live = data.live_discovery;
  const own = data.curated.filter((s) => (s.provenance ?? "curated") === "curated");
  const regional = data.curated.filter((s) => s.provenance === "regional");

  async function discover() {
    setBusy(true);
    setErr(null);
    try {
      await api.post(`/api/global-coverage/${code}/discover`);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }


  return (
    <div className="page wide">
      <div className="row wrap" style={{ marginBottom: 6 }}>
        <a href="#/coverage" className="small">← Global coverage</a>
      </div>
      <h1>{data.name} <span className="faint mono" style={{ fontSize: 15 }}>{data.code}</span></h1>
      <p className="sub">
        {data.region}{data.subregion ? ` · ${data.subregion}` : ""} · {data.country?.status_title}
        {data.blocs.length > 0 && ` · member of ${data.blocs.join(", ")}`}
        {data.country?.languages?.length ? ` · languages: ${data.country.languages.join(", ")}` : ""}
      </p>
      <div className="card pad" style={{ marginBottom: 14 }}>
        <div className="row wrap between">
          <div className="row wrap">
            <Badge kind={LEVEL_KIND[data.level]}>{data.level_title}</Badge>
            <span className="small">{data.connectors} country connector(s) · {data.curated_sources} official source(s) ·
              {" "}{data.regional_sources} regional portal(s) ·
              {" "}{data.discovered_sources} potential source(s) · {data.regional_platforms} regional platform(s)
              {data.custom_sources > 0 && ` · ${data.custom_sources} of yours`}</span>
          </div>
          <div className="row">
            <button className="btn sm" disabled={busy} onClick={() => void discover()}
              title="Asks Wikidata live for this country's institutions and checks every source URL. Sends no personal data.">
              {busy ? <Spinner /> : "🔎"} Search for connectors
            </button>
            <button className="btn sm" onClick={() => setAudit(!audit)}>{audit ? "Hide audit" : "Audit jurisdiction"}</button>
            <button className="btn primary sm" onClick={() => setForm({ mode: "source", initial: { jurisdictions: [code] } })}>＋ Add source</button>
            <button className="btn sm" onClick={() => setForm({ mode: "link", initial: { jurisdictions: [code], source_type: "reference" } })}>＋ Add link</button>
          </div>
        </div>
        {data.level === "none" && (
          <div className="notice warn" style={{ marginTop: 10 }}>
            <b>Status:</b> No connector currently available · <b>Reason:</b> {data.reason}
          </div>
        )}
        {data.reason && data.level !== "none" && <p className="small muted" style={{ margin: "8px 0 0" }}>{data.reason}</p>}
        {data.legal_notes && <p className="small" style={{ margin: "8px 0 0" }}>{data.legal_notes}</p>}
        <div className="row wrap small" style={{ marginTop: 8, gap: 8 }}>
          {data.judgments_anonymized && <Badge>judgments anonymised by law</Badge>}
          {!data.criminal_records_public && <Badge>criminal records not public</Badge>}
          {data.last_checked && <span className="faint">Official links last checked {data.last_checked.slice(0, 10)}</span>}
        </div>
        <ErrorBox error={err} />
      </div>

      {audit && <div style={{ marginBottom: 14 }}><AuditPanel key={auditKey} code={code} onClose={() => setAudit(false)}
        onAdd={(prefill) => setForm({ mode: "source", initial: prefill })} /></div>}
      {form && <SourceForm mode={form.mode} initial={form.initial} editId={form.editId} onClose={() => setForm(null)}
        onSaved={() => { setForm(null); setAuditKey(auditKey + 1); void reload(); }} />}

      {live && (
        <div className="card pad" style={{ marginBottom: 14 }}>
          <b>Last connector search</b> <span className="small muted">{fmtDate(live.ran_at)} · Wikidata returned {live.wikidata_items} institutions ·
            {" "}{live.new_sources.length} not yet in the registry · links: {Object.entries(live.counts).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(", ")}</span>
          {live.notes.map((n) => <div key={n} className="small" style={{ color: "#f3cf86" }}>{n}</div>)}
          {live.new_sources.length > 0 && (
            <table className="data" style={{ marginTop: 8 }}>
              <thead><tr><th>New candidate (Wikidata, live)</th><th>Kind</th><th>Link</th><th /></tr></thead>
              <tbody>{live.new_sources.map((w) => (
                <tr key={w.id}><td><Ext href={w.url}>{w.title}</Ext> <span className="faint mono small">{w.qid}</span></td>
                  <td className="small">{data.kinds[w.kind] ?? w.kind}</td>
                  <td><Verification v={live.checks[w.id]} /></td>
                  <td><button className="btn sm" title="Opens the source form, filled in. Nothing is saved until you confirm."
                    onClick={() => setForm({ mode: "source", initial: { title: w.title, url: w.url, kind: w.kind, jurisdictions: [code],
                      source_type: "reference", notes: `Found by the live Wikidata search (${w.qid}); not verified.` } })}>Add…</button></td></tr>))}</tbody>
            </table>
          )}
        </div>
      )}

      <div className="grid2">
        <div className="card pad">
          <h3 style={{ marginTop: 0 }}>Country connectors</h3>
          {data.country_connectors.length === 0 ? <p className="small muted">No connector targets this country yet.</p> : (
            <table className="data">
              <thead><tr><th>Connector</th><th>Status</th><th>Health</th></tr></thead>
              <tbody>{data.country_connectors.map((c) => (
                <tr key={c.connector}><td><a href={`#/sources/${c.connector}`}>{c.title}</a><div className="small faint">{c.category_title} · {c.access_title}</div></td>
                  <td><SourceStatus status={c.status} /></td>
                  <td><Badge kind={HEALTH_KIND[c.health]}>{c.health_title}</Badge>
                    <div className="small faint">{c.last_success ? `last success ${fmtDate(c.last_success)}` : "no successful query yet"}</div></td></tr>))}</tbody>
            </table>
          )}
        </div>
        <div className="card pad">
          <h3 style={{ marginTop: 0 }}>Global connectors with data for this country</h3>
          <table className="data"><tbody>{data.global_capabilities_detail.map((g) => (
            <tr key={g.connector + g.what}><td><a href={`#/sources/${g.connector}`}>{g.connector}</a></td><td className="small">{g.what}</td>
              <td>{g.status && <SourceStatus status={g.status} />}</td></tr>))}</tbody></table>
        </div>
      </div>

      <h3>Official sources ({own.length})</h3>
      <div className="card">{own.length ? <SourceTable items={own} /> :
        <p className="small muted" style={{ padding: 14 }}>No curated official source yet — see the potential sources below.</p>}</div>

      {regional.length > 0 && (
        <>
          <h3>Regional portals that include this country ({regional.length})</h3>
          <p className="small muted">Shared by several countries; depth for this country varies (see notes).</p>
          <div className="card"><SourceTable items={regional} /></div>
        </>
      )}

      {data.bloc_sources.length > 0 && (
        <>
          <h3>Supranational sources that apply ({data.bloc_sources.length})</h3>
          <div className="card"><SourceTable items={data.bloc_sources} /></div>
        </>
      )}

      {data.custom.length > 0 && (
        <>
          <h3>Your sources ({data.custom.length})</h3>
          <p className="small muted">The same records as in Data Sources: edit, disable or delete them here or there.</p>
          <div className="card"><table className="data">
            <thead><tr><th>Source</th><th>Category</th><th>Status</th><th>Automation</th><th>Link</th></tr></thead>
            <tbody>{data.custom.map((s) => (
              <tr key={s.id} style={s.active ? undefined : { opacity: 0.6 }}>
                <td><Ext href={s.url}>{s.title}</Ext> <span className="small faint">{s.source_type_title}</span>
                  {s.notes && <div className="small faint">{s.notes}</div>}
                  <div style={{ marginTop: 4 }}><UserSourceActions s={s} onChanged={() => void reload()}
                    onEdit={() => setForm({ mode: "source", initial: s, editId: s.id })} /></div></td>
                <td className="small">{s.category_title}<div className="faint">{s.group_title}</div></td>
                <td><SourceStatus status={s.status} /><div className="small faint">{s.review_title}</div></td>
                <td><AutomationBadge a={s.automation} /></td>
                <td><LinkCheckBadge v={s.verification} /></td>
              </tr>))}</tbody></table></div>
        </>
      )}

      <div className="row between wrap" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Potential sources from maintained datasets ({data.potential_sources.length})</h3>
        <select value={groupFilter} onChange={(e) => setGroupFilter(e.target.value)} style={{ maxWidth: 260 }}>
          <option value="">All categories</option>
          {Object.entries(data.groups_vocabulary).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      <p className="small muted">Discovered automatically (GLEIF, OpenSanctions, Wikidata, IANA, RIR, GDELT). Nobody has reviewed what they
        publish or whether they can be searched by name; use them as leads.</p>
      {Object.entries(grouped).map(([g, items]) => (
        <div key={g} style={{ marginBottom: 12 }}>
          <div className="small muted" style={{ margin: "6px 0" }}><b>{g}</b> · {items.length}</div>
          <div className="card"><SourceTable items={items} discovered /></div>
        </div>
      ))}

      <h3>Regional platforms ({data.platforms.length})</h3>
      {data.platforms.length === 0 ? <p className="small muted">No platform in the registry is specific to this country; global platforms apply.</p> : (
        <div className="card" style={{ maxHeight: 460, overflow: "auto" }}>
          <table className="data">
            <thead><tr><th>Platform</th><th>Category</th><th>Source</th><th>Status here</th></tr></thead>
            <tbody>{data.platforms.map((p) => (
              <tr key={p.id}><td><Ext href={p.url}>{p.name}</Ext></td><td className="small">{p.category_title}</td>
                <td className="small muted">{p.source}</td>
                <td><Badge kind={SEARCH_KIND[p.search_status]}>{p.search_status_title}</Badge></td></tr>))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}
