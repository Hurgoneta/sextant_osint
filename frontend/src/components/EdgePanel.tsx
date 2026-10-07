// "Why are these connected?" — every piece of evidence behind one edge of the graph, with its source and class.

import { inv } from "../api";
import { EvidenceClass } from "./Assistant";
import { Badge, ErrorBox, Ext, Spinner, fmtDate, pct, useApi } from "../ui";

interface Detail {
  id: string; type: string; title: string; description: string; confidence: number; status: string; first_seen: string;
  from: { id: string; label: string; type: string } | null; to: { id: string; label: string; type: string } | null;
  inferred_only: boolean; note: string;
  evidence: { description: string; confidence: number; observed_at: string; class: string;
    source: { title: string; connector: string; url: string | null; retrieved_at: string; request: string } | null }[];
}

export default function EdgePanel({ invId, relId, onSelect, onClose }: { invId: string; relId: string;
  onSelect: (entityId: string) => void; onClose: () => void }) {
  const { data, error } = useApi<Detail>(inv(invId, `/relationships/${relId}`));
  if (error) return <div className="inspector"><div className="pad"><ErrorBox error={error} /></div></div>;
  if (!data) return <div className="inspector"><div className="pad"><Spinner /></div></div>;
  return (
    <div className="inspector">
      <div className="pad col" style={{ padding: 16, gap: 10 }}>
        <div className="row between"><span className="small muted">Why are these connected?</span>
          <button className="btn ghost sm" onClick={onClose}>✕</button></div>
        <div>
          {data.from && <a style={{ cursor: "pointer" }} onClick={() => onSelect(data.from!.id)}>{data.from.label}</a>}
          <b> — {data.title} → </b>
          {data.to && <a style={{ cursor: "pointer" }} onClick={() => onSelect(data.to!.id)}>{data.to.label}</a>}
        </div>
        <div className="row wrap small" style={{ gap: 6 }}>
          <Badge kind={data.confidence >= 0.75 ? "good" : data.confidence >= 0.4 ? "warn" : ""}>confidence {pct(data.confidence)}</Badge>
          {data.status !== "active" && <Badge kind={data.status === "rejected" ? "bad" : "good"}>{data.status}</Badge>}
          <span className="faint">first seen {fmtDate(data.first_seen)}</span>
        </div>
        {data.description && <div className="small muted">{data.description}</div>}
        {data.note && <div className="notice warn small">{data.note}</div>}
        <b className="small">Evidence ({data.evidence.length})</b>
        {data.evidence.map((ev, i) => (
          <div key={i} className="card pad small col" style={{ gap: 4 }}>
            <div className="row wrap" style={{ gap: 6 }}><EvidenceClass cls={ev.class} /><span className="faint">{pct(ev.confidence)}</span></div>
            <div>{ev.description}</div>
            {ev.source && <div className="faint">Source: {ev.source.url ? <Ext href={ev.source.url}>{ev.source.title || ev.source.connector}</Ext>
              : (ev.source.title || ev.source.connector)} · {fmtDate(ev.source.retrieved_at || ev.observed_at)}
              {ev.source.request && <> · sent: {ev.source.request}</>}</div>}
          </div>
        ))}
        {data.evidence.length === 0 && <span className="small faint">No evidence recorded.</span>}
      </div>
    </div>
  );
}
