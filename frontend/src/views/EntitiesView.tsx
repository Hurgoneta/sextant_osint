import { useMemo, useState } from "react";
import type { EntitySummary } from "../types";
import { Confidence, MatchBadge, TypeDot, useOntology } from "../ui";
import type { GraphStore } from "./Workspace";

type SortKey = "confidence" | "label" | "type" | "depth" | "value_score";

export default function EntitiesView({ store, version, onOpen }: { store: GraphStore; version: number; onOpen: (id: string) => void }) {
  const o = useOntology();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [min, setMin] = useState(0);
  const [sort, setSort] = useState<SortKey>("confidence");
  const rows = useMemo(() => {
    const all = [...store.ents.values()];
    const f = all.filter((e) => (!type || e.type === type) && (!status || e.status === status) && e.confidence >= min &&
      (!q || `${e.label} ${e.value}`.toLowerCase().includes(q.toLowerCase())));
    const cmp: Record<SortKey, (a: EntitySummary, b: EntitySummary) => number> = {
      confidence: (a, b) => b.confidence - a.confidence, value_score: (a, b) => b.value_score - a.value_score,
      label: (a, b) => a.label.localeCompare(b.label), type: (a, b) => a.type.localeCompare(b.type), depth: (a, b) => a.depth - b.depth,
    };
    return f.sort(cmp[sort]);
  }, [store, version, q, type, status, min, sort]); // eslint-disable-line react-hooks/exhaustive-deps
  const types = [...new Set([...store.ents.values()].map((e) => e.type))].sort();
  const th = (k: SortKey, label: string) => (
    <th style={{ cursor: "pointer" }} onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</th>
  );
  return (
    <div className="page wide">
      <div className="row wrap" style={{ marginBottom: 12 }}>
        <input type="search" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
        <select value={type} onChange={(e) => setType(e.target.value)} style={{ maxWidth: 200 }}>
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{o.title(t)}</option>)}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ maxWidth: 180 }}>
          <option value="">Any status</option>
          {["seed", "candidate", "confirmed", "separated", "ignored"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <span className="small muted">Min. confidence</span>
        <input type="range" min={0} max={0.9} step={0.05} value={min} onChange={(e) => setMin(Number(e.target.value))} style={{ width: 140 }} />
        <span className="small">{rows.length} shown</span>
      </div>
      <div className="card">
        <table className="data">
          <thead><tr>{th("type", "Type")}{th("label", "Entity")}<th>Assessment</th>{th("confidence", "Confidence")}{th("value_score", "Inv. value")}{th("depth", "Depth")}</tr></thead>
          <tbody>
            {rows.slice(0, 1500).map((e) => (
              <tr key={e.id} className="clickable" onClick={() => onOpen(e.id)}>
                <td className="small"><span className="row" style={{ gap: 6 }}><TypeDot type={e.type} />{o.title(e.type)}</span></td>
                <td className="wrapword">{e.label}{e.value !== e.label && <div className="small faint mono">{e.value.slice(0, 120)}</div>}</td>
                <td><MatchBadge e={e} /></td>
                <td><Confidence value={e.confidence} /></td>
                <td className="small">{Math.round(e.value_score * 100)}</td>
                <td className="small">{e.depth}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
