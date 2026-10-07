import { useState } from "react";
import { inv } from "../api";
import type { EntitySummary, SourceRef } from "../types";
import { Empty, ErrorBox, SourceChip, Spinner, TypeDot, useApi } from "../ui";

interface Item { date: string; event: string; entity: EntitySummary; source: SourceRef | null }

export default function TimelineView({ invId, version, onOpen }: { invId: string; version: number; onOpen: (id: string) => void }) {
  const [min, setMin] = useState(0.4);
  const { data, error } = useApi<Item[]>(inv(invId, `/timeline?min_confidence=${min}`), [Math.floor(version / 6), min]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  const rows = data.filter((t) => (!from || t.date >= from) && (!to || t.date <= to + "~"));
  let lastYear = "";
  return (
    <div className="page">
      <div className="row wrap" style={{ marginBottom: 14 }}>
        <span className="small muted">From</span><input type="text" placeholder="YYYY" value={from} onChange={(e) => setFrom(e.target.value)} style={{ width: 110 }} />
        <span className="small muted">to</span><input type="text" placeholder="YYYY" value={to} onChange={(e) => setTo(e.target.value)} style={{ width: 110 }} />
        <span className="small muted">Include candidates above</span>
        <input type="range" min={0} max={0.9} step={0.05} value={min} onChange={(e) => setMin(Number(e.target.value))} style={{ width: 140 }} />
        <b className="small">{Math.round(min * 100)}%</b>
      </div>
      {rows.length === 0 && <Empty>No dated events.</Empty>}
      <div className="card">
        {rows.map((t, i) => {
          const y = t.date.slice(0, 4);
          const head = y !== lastYear;
          lastYear = y;
          return (
            <div key={i}>
              {head && <div style={{ padding: "10px 14px 4px", fontWeight: 700, color: "var(--muted)" }}>{y}</div>}
              <div className="row" style={{ padding: "6px 14px", borderBottom: "1px solid #1a2330", cursor: "pointer" }} onClick={() => onOpen(t.entity.id)}>
                <span className="mono small" style={{ width: 92 }}>{t.date}</span>
                <span style={{ width: 190 }} className="small muted">{t.event}</span>
                <TypeDot type={t.entity.type} />
                <span style={{ flex: 1 }} className="wrapword">{t.entity.label}</span>
                <SourceChip s={t.source} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
