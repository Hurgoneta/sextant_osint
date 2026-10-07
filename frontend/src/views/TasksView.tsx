import { inv } from "../api";
import type { EntitySummary, TransformRun } from "../types";
import { ErrorBox, RunStatus, Spinner, fmtDate, useApi } from "../ui";

interface Payload {
  runs: (TransformRun & { input: EntitySummary | null })[];
  tasks: { id: string; transform: string; priority: number; depth: number; reason: string; status: string; trigger: string;
    entity: EntitySummary | null }[];
}

export default function TasksView({ invId, version, onOpen }: { invId: string; version: number; onOpen: (id: string) => void }) {
  const { data, error } = useApi<Payload>(inv(invId, "/runs"), [Math.floor(version / 3)]);
  if (error) return <div className="page"><ErrorBox error={error} /></div>;
  if (!data) return <div className="page"><Spinner /></div>;
  return (
    <div className="page wide">
      <h3 style={{ marginTop: 0 }}>Queue ({data.tasks.length})</h3>
      <p className="small muted">Ordered by priority: investigative value of the entity, usefulness of the transform, closeness to the input.
        Requests from you jump the queue.</p>
      <div className="card" style={{ maxHeight: 320, overflow: "auto", marginBottom: 22 }}>
        <table className="data">
          <thead><tr><th>Status</th><th>Transform</th><th>Input</th><th>Priority</th><th>Why</th></tr></thead>
          <tbody>
            {data.tasks.map((t) => (
              <tr key={t.id}>
                <td><RunStatus status={t.status} /></td>
                <td className="mono">{t.transform}</td>
                <td>{t.entity && <a href="#" onClick={(e) => { e.preventDefault(); onOpen(t.entity!.id); }}>{t.entity.label}</a>}</td>
                <td className="small">{t.priority.toFixed(2)}</td>
                <td className="small muted">{t.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Transform runs ({data.runs.length})</h3>
      <div className="card">
        <table className="data">
          <thead><tr><th>Status</th><th>Transform</th><th>Input</th><th>Sent → to</th><th>Result</th><th>Started</th></tr></thead>
          <tbody>
            {data.runs.map((r) => (
              <tr key={r.id}>
                <td><RunStatus status={r.status} /><div className="small faint">{r.trigger}</div></td>
                <td className="mono small">{r.transform}</td>
                <td>{r.input ? <a href="#" onClick={(e) => { e.preventDefault(); onOpen(r.input!.id); }}>{r.input_label}</a> : r.input_label}</td>
                <td className="small">{r.sent}<div className="faint">→ {r.destinations.join(", ") || "local"}</div></td>
                <td className="small">{r.new_entities ? `+${r.new_entities} entities · ` : ""}{r.requests} req<div className="faint wrapword">{r.message}</div></td>
                <td className="small muted">{fmtDate(r.started_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
