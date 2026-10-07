import { useState } from "react";
import { AuditPanel, AutomationBadge, LinkCheckBadge, OriginBadge, SourceForm, UserSourceActions } from "../components/Catalog";
import type { Automation, SourceDraft, UserSource } from "../types";
import { Badge, ErrorBox, Ext, Spinner, useApi } from "../ui";

interface Src { id: string; title: string; kind: string; kind_title: string; url: string; access_title: string; name_search: boolean;
  person_data: string; api: string; connector: string; anonymized: boolean; notes: string; group: string; group_title: string;
  origin: "system" | "user"; active?: boolean; status?: string; automation?: Automation;
  verification?: { result: string; checked_at?: string } | null }
interface Jur { code: string; name: string; region: string; legal_notes: string; criminal_records_public: boolean;
  judgments_anonymized: boolean | null; sources: Src[]; automated: number; user_sources: number }
interface Payload { regions: { region: string; jurisdictions: Jur[] }[]; totals: Record<string, number>; verified_at: string | null;
  groups: Record<string, string> }

type FormState = { mode: "source" | "link"; initial?: SourceDraft | null; editId?: string } | null;

function JurisdictionPanel({ j, groups, onChanged }: { j: Jur; groups: Record<string, string>; onChanged: () => void }) {
  const [form, setForm] = useState<FormState>(null);
  const [audit, setAudit] = useState(false);
  const [auditKey, setAuditKey] = useState(0);
  const byGroup = Object.keys(groups).map((g) => [g, j.sources.filter((s) => s.group === g)] as const).filter(([, l]) => l.length);
  return (
    <div className="card pad" style={{ marginTop: 10 }}>
      <div className="row between wrap">
        <div>
          <h3 style={{ margin: 0 }}>{j.name} <span className="faint small mono">{j.code}</span></h3>
          <div className="small muted">{j.sources.length} sources · {j.automated} automated{j.user_sources ? ` · ${j.user_sources} yours` : ""}</div>
        </div>
        <div className="row wrap">
          <button className="btn sm" onClick={() => setAudit(!audit)}>{audit ? "Hide audit" : "Audit jurisdiction"}</button>
          <button className="btn primary sm" onClick={() => setForm({ mode: "source", initial: { jurisdictions: [j.code] } })}>＋ Add source</button>
          <button className="btn sm" onClick={() => setForm({ mode: "link", initial: { jurisdictions: [j.code], source_type: "reference" } })}>＋ Add link</button>
          {j.code.length === 2 && <a className="btn sm ghost" href={`#/coverage/${j.code}`}>Country page →</a>}
        </div>
      </div>
      {j.legal_notes && <p className="small">{j.legal_notes}</p>}
      {audit && <div style={{ margin: "10px 0" }}><AuditPanel key={auditKey} code={j.code} onClose={() => setAudit(false)}
        onAdd={(prefill) => setForm({ mode: "source", initial: prefill })} /></div>}
      {byGroup.length === 0 && <p className="small muted">No source is known for this jurisdiction yet. Add one, or run the audit for leads from maintained datasets.</p>}
      {byGroup.length > 0 && (
        <table className="data" style={{ marginTop: 12 }}>
          <thead><tr><th>Source</th><th>Kind</th><th>Access</th><th>Name search</th><th>Automation</th><th>Link</th></tr></thead>
          <tbody>
            {byGroup.map(([g, list]) => [
              <tr key={`g:${g}`} className="group-row"><td colSpan={6}><b>{groups[g]}</b> <span className="faint">· {list.length}</span></td></tr>,
              ...list.map((s) => (
                <tr key={s.id} style={s.active === false ? { opacity: 0.6 } : undefined}>
                  <td><Ext href={s.url}>{s.title}</Ext> <OriginBadge origin={s.origin} />
                    {s.origin === "user" && s.active === false && <Badge>{s.status === "disabled" ? "disabled" : "inactive"}</Badge>}
                    {s.notes && <div className="small faint">{s.notes}</div>}
                    {s.person_data && <div className="small muted">Personal data: {s.person_data}</div>}
                    {s.api && <div className="small"><Ext href={s.api}>API docs</Ext></div>}
                    {s.origin === "user" && <div style={{ marginTop: 4 }}><UserSourceActions s={s as unknown as UserSource} onChanged={onChanged}
                      onEdit={() => setForm({ mode: "source", initial: s as unknown as UserSource, editId: s.id })} /></div>}</td>
                  <td className="small">{s.kind_title}</td>
                  <td className="small">{s.access_title}</td>
                  <td>{s.anonymized ? <Badge>anonymised</Badge> : s.name_search ? <Badge kind="info">yes</Badge> : <span className="faint">no</span>}</td>
                  <td>{s.origin === "user" ? <AutomationBadge a={s.automation} />
                    : s.connector ? <a href={`#/sources/${s.connector}`}><Badge kind="good">{s.connector}</Badge></a> : <span className="faint small">manual</span>}</td>
                  <td><LinkCheckBadge v={s.verification} /></td>
                </tr>
              )),
            ])}
          </tbody>
        </table>
      )}
      {form && <SourceForm mode={form.mode} initial={form.initial} editId={form.editId} onClose={() => setForm(null)}
        onSaved={() => { setForm(null); setAuditKey(auditKey + 1); onChanged(); }} />}
    </div>
  );
}

export default function Jurisdictions() {
  const { data, error, reload } = useApi<Payload>("/api/jurisdictions");
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  return (
    <div className="page wide">
      <h1>Worldwide public-source registry</h1>
      <p className="sub">{data.totals.jurisdictions} jurisdictions · {data.totals.sources} sources · {data.totals.automated} automated by connectors ·
        {" "}{data.totals.name_searchable} searchable by name{data.totals.user_sources ? ` · ${data.totals.user_sources} added by you` : ""}. Availability
        differs by country: what is public in one is protected in another.
        {data.verified_at && ` Links last checked ${data.verified_at.slice(0, 10)}.`}</p>
      <input type="search" placeholder="Filter countries…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 280, marginBottom: 14 }} />
      {data.regions.map((r) => (
        <div key={r.region} style={{ marginBottom: 18 }}>
          <h3 style={{ margin: "0 0 8px" }}>{r.region}</h3>
          <div className="grid3">
            {r.jurisdictions.filter((j) => !q || j.name.toLowerCase().includes(q.toLowerCase()) || j.code.toLowerCase() === q.toLowerCase()).map((j) => (
              <div key={j.code} className="src-card" onClick={() => setOpen(open === j.code ? null : j.code)}
                style={open === j.code ? { borderColor: "var(--accent)" } : undefined}>
                <div className="row between"><b>{j.name}</b><span className="small faint">{j.code}</span></div>
                <div className="row wrap small" style={{ gap: 6 }}>
                  <Badge>{j.sources.length} sources</Badge>
                  {j.automated > 0 && <Badge kind="good">{j.automated} automated</Badge>}
                  {j.user_sources > 0 && <Badge kind="violet">{j.user_sources} yours</Badge>}
                  {j.judgments_anonymized && <Badge>judgments anonymised</Badge>}
                  {!j.criminal_records_public && j.code.length === 2 && j.code !== "EU" && <Badge>criminal records not public</Badge>}
                </div>
              </div>
            ))}
          </div>
          {r.jurisdictions.filter((j) => j.code === open).map((j) => (
            <JurisdictionPanel key={j.code} j={j} groups={data.groups} onChanged={() => void reload()} />
          ))}
        </div>
      ))}
    </div>
  );
}
