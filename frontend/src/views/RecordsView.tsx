import { useState } from "react";
import { api, inv } from "../api";
import { Badge, Confidence, Empty, ErrorBox, Ext, Spinner, Value, useApi } from "../ui";

interface RecordRow {
  id: string;
  label: string;
  status: string;
  confidence: number;
  tier: string;
  record_type: string;
  jurisdiction: string;
  authority: string;
  official_url: string;
  reference: string;
  date: string;
  title: string;
  category: string;
  matched_name: string;
  name_match: string;
  role: string;
  role_meaning: string;
  birth_years: number[];
  nationality: string;
  criminal: boolean;
  summary: string;
  record_state: string;
  identity_state: string;
  how_to_verify: string;
  note: string;
  evidence: { description: string; weight: number }[];
  contradictions: { description: string; weight: number }[];
  sources: { connector: string; url: string | null; retrieved_at: string }[];
}

interface JurSource { id: string; title: string; kind_title: string; url: string; access_title: string; name_search: boolean; connector: string; notes: string; anonymized: boolean }
interface JurPayload { regions: { region: string; jurisdictions: { code: string; name: string; legal_notes: string; sources: JurSource[] }[] }[] }

const ID_KIND: Record<string, string> = { "Confirmed identity match": "good", "Possible name match": "warn", "Unable to verify": "",
  "Public record found — linked by identifier": "info", "Different person (separated)": "", Ignored: "" };

export default function RecordsView({ invId, version, onOpen }: { invId: string; version: number; onOpen: (id: string) => void }) {
  const { data, error } = useApi<{ records: RecordRow[]; jurisdictions: string[] }>(inv(invId, "/records"), [Math.floor(version / 4)]);
  const countries = data?.jurisdictions ?? [];
  const jur = useApi<JurPayload>(countries.length ? `/api/jurisdictions?countries=${countries.join(",")}` : null, [countries.join(",")]);
  const [onlyCriminal, setOnlyCriminal] = useState(false);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const rows = data.records.filter((r) => !onlyCriminal || r.criminal);
  async function decide(id: string, action: string) {
    await api.post(inv(invId, `/entities/${id}/decision`), { action });
  }
  return (
    <div className="page wide">
      <div className="notice warn" style={{ marginBottom: 14 }}>
        <b>Records are found by name in official public sources — a matching name is almost always someone else.</b> "Possible
        name match" means only that. A record becomes a <b>confirmed identity match</b> only when you confirm it after checking
        identifiers such as the date of birth at the official source. Never draw conclusions about a person from a name match.
      </div>
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <Badge>{data.records.length} records</Badge>
        <Badge kind="warn">{data.records.filter((r) => r.identity_state === "Possible name match").length} possible name matches</Badge>
        <Badge kind="good">{data.records.filter((r) => r.status === "confirmed").length} confirmed</Badge>
        <label className="check small"><input type="checkbox" checked={onlyCriminal} onChange={(e) => setOnlyCriminal(e.target.checked)} />Only criminal-category cases</label>
      </div>
      {rows.length === 0 && <Empty>No public records were returned by the sources that ran.</Empty>}
      <div className="col" style={{ gap: 12 }}>
        {rows.map((r) => (
          <div key={r.id} className="card pad">
            <div className="row between wrap">
              <div className="row wrap">
                <Badge kind="info">Public record found</Badge>
                <Badge kind={ID_KIND[r.identity_state] ?? ""}>{r.identity_state}</Badge>
                {r.criminal && <Badge kind="bad">criminal-category case</Badge>}
              </div>
              <Confidence value={r.confidence} label="Confidence that the record concerns the target" />
            </div>
            <h3 style={{ margin: "10px 0 4px", fontSize: 15 }}>{r.title || r.label}</h3>
            <div className="kv" style={{ marginTop: 8 }}>
              <span className="k">Jurisdiction</span><span>{r.jurisdiction}</span>
              <span className="k">Source / authority</span><span>{r.authority} <span className="faint">({r.sources.map((s) => s.connector).join(", ")})</span></span>
              <span className="k">Official URL</span><span>{r.official_url ? <Ext href={r.official_url} /> : "—"}</span>
              <span className="k">Record type</span><span>{r.record_type?.replace(/_/g, " ")}{r.category ? ` · ${r.category}` : ""}</span>
              <span className="k">Date</span><span>{r.date || "—"}</span>
              {r.reference && <><span className="k">Reference</span><span className="mono">{r.reference}</span></>}
              <span className="k">Name on the record</span><span><b>{r.matched_name || "—"}</b> {r.name_match && <span className="faint">({r.name_match})</span>}</span>
              {r.role && <><span className="k">Role of named party</span><span>{r.role}{r.role_meaning ? ` (${r.role_meaning})` : ""}</span></>}
              {r.birth_years?.length > 0 && <><span className="k">Birth years on record</span><span>{r.birth_years.join(", ")}</span></>}
              {r.nationality && <><span className="k">Nationality on record</span><span>{r.nationality}</span></>}
              {r.summary && <><span className="k">Summary</span><span><Value v={r.summary} /></span></>}
              <span className="k">Exact matching evidence</span>
              <span className="ev">
                {r.evidence.map((e, i) => <div key={i}><span className="ok">+{e.weight.toFixed(1)}</span> {e.description}</div>)}
                {r.contradictions.map((e, i) => <div key={`c${i}`}><span className="no">{e.weight.toFixed(1)}</span> {e.description}</div>)}
                {r.evidence.length + r.contradictions.length === 0 && <span className="faint">Nothing beyond the source's own result.</span>}
              </span>
              {(r.how_to_verify || r.note) && <><span className="k">How to verify</span><span className="small">{r.how_to_verify || r.note}</span></>}
            </div>
            <div className="row wrap" style={{ marginTop: 12 }}>
              <button className="btn sm" onClick={() => onOpen(r.id)}>Show in graph</button>
              <button className="btn sm good" onClick={() => void decide(r.id, "confirm")} title="Only after verifying identifiers at the official source">Confirm identity match</button>
              <button className="btn sm warn" onClick={() => void decide(r.id, "separate")}>Different person</button>
              <button className="btn sm" onClick={() => void decide(r.id, "ignore")}>Ignore</button>
            </div>
          </div>
        ))}
      </div>

      {jur.data && (
        <>
          <h3 style={{ marginTop: 28 }}>Official sources for {countries.join(", ")}</h3>
          <p className="small muted">Including sources that cannot be searched automatically. Open them yourself where needed.</p>
          {jur.data.regions.flatMap((reg) => reg.jurisdictions).filter((j) => countries.includes(j.code)).map((j) => (
            <div key={j.code} className="card pad" style={{ marginBottom: 10 }}>
              <b>{j.name}</b>
              {j.legal_notes && <p className="small muted">{j.legal_notes}</p>}
              <table className="data">
                <tbody>
                  {j.sources.map((s) => (
                    <tr key={s.id}>
                      <td><Ext href={s.url}>{s.title}</Ext><div className="small faint">{s.notes}</div></td>
                      <td className="small">{s.kind_title}</td>
                      <td className="small">{s.access_title}</td>
                      <td>{s.connector ? <Badge kind="good">automated</Badge> : s.anonymized ? <Badge>anonymised</Badge> : s.name_search ? <Badge kind="info">name search (manual)</Badge> : <Badge>link</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
