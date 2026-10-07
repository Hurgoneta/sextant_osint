import { useEffect, useState } from "react";
import { api, inv } from "../api";
import type { Coverage, StatusPayload } from "../types";
import { RunStatus, Spinner, fmtDuration, useInterval } from "../ui";
import type { GraphStore } from "./Workspace";

interface Props {
  invId: string;
  store: GraphStore;
  version: number;
  status: StatusPayload | null;
  onOpenGraph: () => void;
}

export default function LiveView({ invId, store, version, status, onOpenGraph }: Props) {
  const [cov, setCov] = useState<Coverage | null>(null);
  const running = status?.status === "running";
  const load = () => void api.get<Coverage>(inv(invId, "/coverage")).then(setCov).catch(() => undefined);
  useEffect(load, [invId]); // eslint-disable-line react-hooks/exhaustive-deps
  useInterval(load, running ? 3000 : null);
  void version;

  const s = status?.stats;
  const sum = cov?.summary ?? {};
  const groups = (cov?.groups ?? []).filter((g) => g.total > 0 || g.unavailable_sources > 0);
  return (
    <div className="live">
      <div className="live-box">
        <div className="hd">
          {running ? <Spinner /> : <span>◉</span>}
          FULL INVESTIGATION
          <span className="spacer" />
          <span className="small muted" style={{ letterSpacing: 0, fontWeight: 500 }}>
            {status?.phase} · {fmtDuration(status?.elapsed ?? 0)} · {(status?.requests ?? 0).toLocaleString()} requests
          </span>
        </div>
        <div style={{ padding: "14px 0 10px" }}>
          {groups.length === 0 && <div className="progress-row"><span className="muted">Planning…</span><span /><span /></div>}
          {groups.map((g) => (
            <div className="progress-row" key={g.group}>
              <span>{g.group}<div className="small faint">{g.queried_sources} sources queried{g.unavailable_sources ? ` · ${g.unavailable_sources} unavailable` : ""}</div></span>
              <span className="bar"><i className={g.pending === 0 && g.total > 0 ? "done" : ""} style={{ width: g.total ? `${Math.round(g.progress * 100)}%` : "0%" }} /></span>
              <b className="small" style={{ textAlign: "right" }}>{g.total ? `${Math.round(g.progress * 100)}%` : "—"}</b>
            </div>
          ))}
        </div>
        <div className="bigstats">
          <div><b>{(s?.entities ?? 0).toLocaleString()}</b><span>Entities discovered</span></div>
          <div><b>{(s?.relationships ?? 0).toLocaleString()}</b><span>Relationships</span></div>
          <div><b>{(s?.high_confidence ?? 0).toLocaleString()}</b><span>High-confidence links</span></div>
          <div><b>{status?.queued ?? 0}</b><span>Transforms queued · {status?.running ?? 0} running</span></div>
        </div>
        <div style={{ padding: "14px 20px", borderTop: "1px solid var(--line)" }} className="small">
          <div className="row wrap" style={{ gap: 18 }}>
            <span>Sources queried: <b>{sum.queried ?? 0}</b></span>
            <span>Unavailable / blocked: <b>{sum.failed_or_blocked ?? 0}</b></span>
            <span>Requiring credentials: <b>{sum.requires_credentials ?? 0}</b></span>
            <span>Requiring subscription: <b>{sum.requires_subscription ?? 0}</b></span>
            <span>Disabled / not installed: <b>{(sum.disabled ?? 0) + (sum.not_installed ?? 0)}</b></span>
            <span>Not applicable: <b>{sum.not_applicable ?? 0}</b></span>
          </div>
          <p className="faint" style={{ marginBottom: 0 }}>{cov?.disclaimer}</p>
        </div>
        <div style={{ padding: "0 20px 16px" }}>
          <button className="btn primary" onClick={onOpenGraph}>VIEW LIVE GRAPH</button>
        </div>
      </div>

      <h3 style={{ margin: "22px 0 8px" }}>Transform activity</h3>
      <div className="card feed">
        {store.runs.length === 0 && <div className="muted">No transforms yet.</div>}
        {store.runs.slice(0, 120).map((r) => (
          <div key={r.id}>
            <RunStatus status={r.status} />
            <span style={{ flex: 1 }}>
              <b>{r.transform}</b> <span className="muted">on</span> {r.input_label}
              {r.message && <div className="small faint wrapword">{r.message}</div>}
            </span>
            <span className="small faint">{r.new_entities ? `+${r.new_entities}` : ""} {r.requests ? `${r.requests} req` : ""}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
