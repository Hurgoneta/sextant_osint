import { useCallback, useEffect, useRef, useState } from "react";
import { api, inv } from "../api";
import type { EntitySummary, EventItem, GraphPayload, InvestigationInfo, RelSummary, StatusPayload, TransformRun } from "../types";
import { Badge, ErrorBox, Spinner, fmtDuration, navigate } from "../ui";
import GraphView from "./GraphView";
import LiveView from "./LiveView";
import AccountsView from "./AccountsView";
import RecordsView from "./RecordsView";
import EntitiesView from "./EntitiesView";
import TimelineView from "./TimelineView";
import CoverageView from "./CoverageView";
import TasksView from "./TasksView";
import ReportView from "./ReportView";
import AssistantView from "./AssistantView";

export interface GraphStore {
  ents: Map<string, EntitySummary>;
  rels: Map<string, RelSummary>;
  target: string | null;
  version: number;
  runs: TransformRun[];
}

const TABS: [string, string][] = [
  ["assistant", "Assistant"], ["graph", "Graph"], ["live", "Live"], ["accounts", "Accounts"], ["records", "Public records"], ["entities", "Entities"],
  ["timeline", "Timeline"], ["coverage", "Coverage"], ["tasks", "Transforms"], ["report", "Report"],
];
const STATUS_KIND: Record<string, string> = { running: "info", completed: "good", limit_reached: "warn", failed: "bad",
  cancelled: "", paused: "warn", created: "" };

function useGraphStore(id: string) {
  const store = useRef<GraphStore>({ ents: new Map(), rels: new Map(), target: null, version: 0, runs: [] });
  const seq = useRef(0);
  const [version, setVersion] = useState(0);
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bump = useRef<number | null>(null);

  const schedule = useCallback(() => {
    if (bump.current !== null) return;
    bump.current = window.setTimeout(() => {
      bump.current = null;
      store.current.version += 1;
      setVersion(store.current.version);
    }, 350);
  }, []);

  const loadGraph = useCallback(async () => {
    const g = await api.get<GraphPayload>(inv(id, "/graph"));
    const s = store.current;
    s.ents = new Map(g.entities.map((e) => [e.id, e]));
    s.rels = new Map(g.relationships.map((r) => [r.id, r]));
    s.target = g.target;
    seq.current = g.seq;
    schedule();
  }, [id, schedule]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await loadGraph();
        const runs = await api.get<{ runs: TransformRun[] }>(inv(id, "/runs"));
        store.current.runs = runs.runs.slice(0, 200);
      } catch (e) {
        setError((e as Error).message);
        return;
      }
      while (alive) {
        try {
          const r = await api.get<{ resync: boolean; seq: number; events: EventItem[]; status: StatusPayload }>(
            inv(id, `/events?after=${seq.current}&wait=20`));
          if (!alive) break;
          setStatus(r.status);
          if (r.resync) {
            await loadGraph();
            continue;
          }
          const s = store.current;
          let changed = false;
          for (const ev of r.events) {
            if (ev.kind === "entity" || ev.kind === "score") {
              const prev = s.ents.get(ev.data.id);
              s.ents.set(ev.data.id, prev ? { ...prev, ...ev.data } : ev.data);
              changed = true;
            } else if (ev.kind === "relationship") {
              s.rels.set(ev.data.id, ev.data);
              changed = true;
            } else if (ev.kind === "run") {
              const i = s.runs.findIndex((x) => x.id === ev.data.id);
              if (i >= 0) s.runs[i] = ev.data;
              else s.runs.unshift(ev.data);
              if (s.runs.length > 300) s.runs.length = 300;
              changed = true;
            } else if (ev.kind === "graph_changed") {
              await loadGraph();
            }
          }
          seq.current = r.seq;
          if (changed) schedule();
          if (!r.events.length && r.status.status !== "running") await new Promise((res) => setTimeout(res, 2500));
        } catch (e) {
          if (!alive) break;
          setError((e as Error).message);
          await new Promise((res) => setTimeout(res, 4000));
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [id, loadGraph]);
  return { store: store.current, version, status, setStatus, error, reload: loadGraph };
}

export default function Workspace({ id, tab, entity }: { id: string; tab: string; entity?: string }) {
  const [info, setInfo] = useState<InvestigationInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { store, version, status, setStatus, error: liveError, reload } = useGraphStore(id);
  const st = status ?? info;

  useEffect(() => {
    api.get<InvestigationInfo>(inv(id)).then(setInfo).catch((e) => setError((e as Error).message));
  }, [id]);

  async function control(action: string, body?: unknown) {
    try {
      setStatus(await api.post<StatusPayload>(inv(id, `/${action}`), body));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const select = (eid: string | null) => navigate(`/inv/${id}/${tab === "graph" ? "graph" : tab}${eid ? `/${eid}` : ""}`);
  const openInGraph = (eid: string) => navigate(`/inv/${id}/graph/${eid}`);

  if (error && !info) return <div className="page"><ErrorBox error={error} /></div>;
  if (!info) return <div className="page"><Spinner /></div>;
  const running = st?.status === "running";
  const s = st?.stats;
  return (
    <div className="ws">
      <div className="ws-head">
        <div>
          <h2>{info.title}</h2>
          <div className="small muted">{st?.phase || "—"}</div>
        </div>
        <span className="statpill">
          {running && <span className="spinner" style={{ width: 11, height: 11 }} />}
          <Badge kind={STATUS_KIND[st?.status ?? ""] ?? ""}>{(st?.status ?? "").replace("_", " ")}</Badge>
          depth {st?.limits.depth} · max {st?.max_depth} hops
        </span>
        <div className="counters">
          <span><b>{s?.entities ?? 0}</b> entities</span>
          <span><b>{s?.relationships ?? 0}</b> relationships</span>
          <span><b>{s?.high_confidence ?? 0}</b> high-confidence</span>
          <span><b>{st?.runs ?? 0}</b> transforms</span>
          <span><b>{(st?.requests ?? 0).toLocaleString()}</b> requests</span>
          <span><b>{st?.queued ?? 0}</b> queued</span>
          <span><b>{fmtDuration(st?.elapsed ?? 0)}</b></span>
        </div>
        <div className="spacer" />
        {running && <button className="btn warn sm" onClick={() => void control("pause")}>Pause</button>}
        {st?.status === "paused" && <button className="btn good sm" onClick={() => void control("resume")}>Resume</button>}
        {(running || st?.status === "paused") && <button className="btn bad sm" onClick={() => void control("stop")}>Stop</button>}
        {st && ["completed", "limit_reached", "cancelled", "failed", "created"].includes(st.status) && (
          <button className="btn sm" onClick={() => void control(st.status === "created" ? "start" : "continue")}
            title={st.status === "created" ? "Start automatic investigation" : "Run another round: follow what is still queued or newly eligible"}>
            {st.status === "created" ? "▶ Auto-investigate" : "↻ Continue"}
          </button>
        )}
      </div>
      <div className="tabs">
        {TABS.map(([k, label]) => (
          <button key={k} className={tab === k ? "active" : ""} onClick={() => navigate(`/inv/${id}/${k}`)}>
            {label}
            {k === "graph" && <span className="count">{store.ents.size}</span>}
            {k === "tasks" && <span className="count">{st?.queued ? `${st.queued} queued` : ""}</span>}
          </button>
        ))}
      </div>
      <div className={`ws-body ${tab === "graph" ? "fill" : ""}`}>
        {(error || liveError) && tab !== "graph" && <div style={{ padding: 12 }}><ErrorBox error={error || liveError} /></div>}
        {tab === "assistant" && <AssistantView invId={id} version={version} onOpen={openInGraph} />}
        {tab === "graph" && <GraphView invId={id} store={store} version={version} selected={entity ?? null} onSelect={select} reload={reload} />}
        {tab === "live" && <LiveView invId={id} store={store} version={version} status={st as StatusPayload} onOpenGraph={() => navigate(`/inv/${id}/graph`)} />}
        {tab === "accounts" && <AccountsView invId={id} version={version} onOpen={openInGraph} />}
        {tab === "records" && <RecordsView invId={id} version={version} onOpen={openInGraph} />}
        {tab === "entities" && <EntitiesView store={store} version={version} onOpen={openInGraph} />}
        {tab === "timeline" && <TimelineView invId={id} version={version} onOpen={openInGraph} />}
        {tab === "coverage" && <CoverageView invId={id} version={version} />}
        {tab === "tasks" && <TasksView invId={id} version={version} onOpen={openInGraph} />}
        {tab === "report" && <ReportView invId={id} info={info} />}
      </div>
    </div>
  );
}
