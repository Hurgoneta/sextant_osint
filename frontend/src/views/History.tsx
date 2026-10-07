import { api } from "../api";
import { Badge, Empty, ErrorBox, Spinner, fmtDate, navigate, useApi } from "../ui";

interface Row {
  id: string;
  created_at: string;
  updated_at: string;
  status: string;
  target_type: string;
  depth: string;
  title: string;
  stats: { entities?: number; relationships?: number; high_confidence?: number };
  seeds: string[] | null;
}

const STATUS_KIND: Record<string, string> = { running: "info", completed: "good", limit_reached: "warn", failed: "bad",
  cancelled: "", paused: "warn", created: "" };

export default function History() {
  const { data, error, loading, reload } = useApi<Row[]>("/api/investigations");
  async function remove(id: string) {
    if (!window.confirm("Delete this investigation and everything it found? This cannot be undone.")) return;
    await api.del(`/api/investigations/${id}`);
    void reload();
  }
  return (
    <div className="page">
      <h1>Investigations</h1>
      <p className="sub">Stored encrypted on this computer only.</p>
      <ErrorBox error={error} />
      {loading && !data && <Spinner />}
      {data && data.length === 0 && <Empty>No investigations yet. <a href="#/">Start one</a>.</Empty>}
      {data && data.length > 0 && (
        <div className="card">
          <table className="data">
            <thead><tr><th>Investigation</th><th>Status</th><th>Depth</th><th>Entities</th><th>High-confidence</th><th>Created</th><th /></tr></thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => navigate(`/inv/${r.id}/graph`)}>
                  <td><b>{r.title}</b><div className="small faint">{(r.seeds ?? []).slice(0, 4).join(" · ")}</div></td>
                  <td><Badge kind={STATUS_KIND[r.status] ?? ""}>{r.status.replace("_", " ")}</Badge></td>
                  <td>{r.depth}</td>
                  <td>{r.stats?.entities ?? "—"}</td>
                  <td>{r.stats?.high_confidence ?? "—"}</td>
                  <td className="small muted">{fmtDate(r.created_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}><button className="btn sm bad" onClick={() => void remove(r.id)}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
