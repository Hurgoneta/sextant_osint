import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import type { EntitySummary, Ontology, SourceRef } from "./types";

// ---------------------------------------------------------------- routing (hash based: #/inv/abc/graph)

export function useRoute(): string[] {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
}

export function navigate(path: string): void {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

// ---------------------------------------------------------------- data hooks

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const load = useCallback(async () => {
    if (!path) return;
    const my = ++seq.current;
    setLoading(true);
    try {
      const d = await api.get<T>(path);
      if (my === seq.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (my === seq.current) setError((e as Error).message);
    } finally {
      if (my === seq.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    void load();
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms === null) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}

// ---------------------------------------------------------------- ontology

const OntologyCtx = createContext<Ontology | null>(null);

export function OntologyProvider({ children }: { children: ReactNode }) {
  const { data } = useApi<Ontology>("/api/ontology");
  return <OntologyCtx.Provider value={data}>{children}</OntologyCtx.Provider>;
}

export function useOntology() {
  const o = useContext(OntologyCtx);
  const types = new Map((o?.entity_types ?? []).map((t) => [t.id, t]));
  const rels = new Map((o?.relation_types ?? []).map((r) => [r.id, r]));
  return {
    ready: !!o,
    types,
    rels,
    color: (t: string) => types.get(t)?.color ?? "#94a3b8",
    title: (t: string) => types.get(t)?.title ?? t,
    shape: (t: string) => types.get(t)?.shape ?? "ellipse",
    list: o?.entity_types ?? [],
  };
}

// ---------------------------------------------------------------- formatting

export function pct(x: number): string {
  return `${Math.round((x || 0) * 100)}%`;
}

export function fmtDate(s?: string | null): string {
  if (!s) return "—";
  return s.replace("T", " ").replace(/\+00:00$|Z$/, "").slice(0, 16);
}

export function fmtDuration(sec: number): string {
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function isHttp(u: unknown): u is string {
  return typeof u === "string" && /^https?:\/\/[^\s]+$/i.test(u);
}

// ---------------------------------------------------------------- components

export function Ext({ href, children }: { href: string; children?: ReactNode }) {
  if (!isHttp(href)) return <span className="wrapword">{children ?? href}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="wrapword">
      {children ?? href}
    </a>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}

const CONF_COLOR = (c: number) => (c >= 0.75 ? "var(--good)" : c >= 0.4 ? "var(--warn)" : "#6b7787");

export function Confidence({ value, label }: { value: number; label?: string }) {
  return (
    <span className="conf" title={label ?? "Heuristic confidence that this pertains to the target"}>
      <span className="bar">
        <i style={{ width: `${Math.round(value * 100)}%`, background: CONF_COLOR(value) }} />
      </span>
      <b>{pct(value)}</b>
    </span>
  );
}

export function TypeDot({ type }: { type: string }) {
  const o = useOntology();
  return <span className="dot" style={{ background: o.color(type) }} title={o.title(type)} />;
}

export function Badge({ kind = "", children, title }: { kind?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${kind}`} title={title}>
      {children}
    </span>
  );
}

const SOURCE_STATUS: Record<string, [string, string]> = {
  available: ["good", "Available"],
  needs_credentials: ["warn", "API key required"],
  needs_subscription: ["violet", "Subscription required"],
  disabled: ["", "Disabled"],
  not_installed: ["", "Not installed"],
  catalog_only: ["info", "Contact provider"],
  manual: ["", "Manual only"],
  retired: ["bad", "Retired"],
  excluded: ["bad", "Excluded by policy"],
};

export function SourceStatus({ status, title }: { status: string; title?: string }) {
  const [k, t] = SOURCE_STATUS[status] ?? ["", status];
  return <Badge kind={k} title={title}>{title && status !== "available" ? t : t}</Badge>;
}

const RUN_STATUS: Record<string, string> = {
  found: "good", empty: "", not_found: "", blocked: "bad", rate_limited: "warn", needs_credentials: "warn",
  error: "bad", timeout: "warn", cancelled: "", budget_exhausted: "warn", skipped: "", running: "info", queued: "info",
  queried: "good", failed: "bad", needs_subscription: "violet", pending: "info", not_applicable: "", not_run: "",
  catalog_only: "info", manual: "", excluded: "bad", disabled: "", not_installed: "", retired: "bad",
};

export function RunStatus({ status }: { status: string }) {
  return <Badge kind={RUN_STATUS[status] ?? ""}>{status.replace(/_/g, " ")}</Badge>;
}

export function MatchBadge({ e }: { e: Pick<EntitySummary, "match_label" | "status" | "tier"> }) {
  if (!e.match_label) return null;
  const kind =
    e.status === "confirmed" ? "good" : e.status === "seed" ? "info" : e.status === "ignored" || e.status === "separated"
      ? "" : e.tier === "high" ? "good" : e.tier === "medium" ? "warn" : "";
  return <Badge kind={kind}>{e.match_label}</Badge>;
}

export function SourceChip({ s }: { s: SourceRef | null }) {
  if (!s) return null;
  const tip = `${s.title}\n${s.url ?? "(local)"}\nconnector: ${s.connector}\nretrieved: ${fmtDate(s.retrieved_at)}${s.request ? `\nsent: ${s.request}` : ""}`;
  const body = (
    <span className="srcchip" title={tip}>
      ⛁ {s.connector}
    </span>
  );
  return isHttp(s.url) ? (
    <a href={s.url} target="_blank" rel="noopener noreferrer nofollow">
      {body}
    </a>
  ) : (
    body
  );
}

export function Value({ v }: { v: unknown }): ReactNode {
  if (v === null || v === undefined || v === "") return <span className="faint">—</span>;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") {
    if (v.startsWith("data:image/")) return <img src={v} alt="" className="thumb" />;
    if (isHttp(v)) return <Ext href={v} />;
    return <span className="wrapword">{v}</span>;
  }
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="faint">—</span>;
    return (
      <span className="col" style={{ gap: 2 }}>
        {v.slice(0, 30).map((x, i) => (
          <span key={i}>
            <Value v={x} />
          </span>
        ))}
        {v.length > 30 && <span className="faint">… {v.length - 30} more</span>}
      </span>
    );
  }
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>).filter(([, x]) => x !== null && x !== "" && x !== undefined);
    return (
      <span className="col" style={{ gap: 2 }}>
        {entries.slice(0, 20).map(([k, x]) => (
          <span key={k}>
            <span className="muted small">{k}: </span>
            <Value v={x} />
          </span>
        ))}
      </span>
    );
  }
  return String(v);
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return v;
}
