import { useEffect, useState } from "react";
import { api, inv } from "../api";
import type { EntitySummary, TransformOption } from "../types";
import { ErrorBox, SourceStatus, Spinner, TypeDot, useOntology } from "../ui";

interface Props {
  invId: string;
  entity: EntitySummary | undefined;
  x: number;
  y: number;
  onClose: () => void;
  onQueued: (msg: string) => void;
}

export default function TransformMenu({ invId, entity, x, y, onClose, onQueued }: Props) {
  const [items, setItems] = useState<TransformOption[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const o = useOntology();

  useEffect(() => {
    if (!entity) return;
    api.get<TransformOption[]>(inv(invId, `/entities/${entity.id}/transforms`)).then(setItems).catch((e) => setError((e as Error).message));
  }, [invId, entity]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!entity) return null;
  async function run(transforms: string[] | "all" | "auto", expand_levels = 0) {
    try {
      const r = await api.post<{ queued: string[]; skipped: { transform: string; reason: string }[] }>(
        inv(invId, `/entities/${entity!.id}/transforms`), { transforms, expand_levels });
      onQueued(`${r.queued.length} transform(s) queued` + (r.skipped.length ? `, ${r.skipped.length} skipped` : ""));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const usable = (items ?? []).filter((t) => t.applicable);
  const visible = showAll ? items ?? [] : usable;
  const groups = new Map<string, TransformOption[]>();
  for (const t of visible) {
    const k = t.category_title;
    groups.set(k, [...(groups.get(k) ?? []), t]);
  }
  const left = Math.min(x, window.innerWidth - 440);
  const top = Math.min(y, window.innerHeight - 200);
  return (
    <>
      <div style={{ position: "fixed", inset: 0, zIndex: 49 }} onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div className="ctxmenu" style={{ left, top }} onContextMenu={(e) => e.preventDefault()}>
        <div className="head">
          <div className="row"><TypeDot type={entity.type} /><b className="wrapword">{entity.label}</b></div>
          <div className="small muted">{o.title(entity.type)} · {usable.filter((t) => t.status === "available").length} transforms available</div>
        </div>
        <ErrorBox error={error} />
        <button className="item" onClick={() => void run("auto")}><b>⚡ Auto-investigate</b><span className="small muted">— the engine picks</span></button>
        <button className="item" onClick={() => void run("auto", 2)}><b>⤳ Auto-investigate from here</b><span className="small muted">— +2 levels</span></button>
        <button className="item" onClick={() => void run(usable.filter((t) => t.status === "available").map((t) => t.name))}>
          <b>▶ Run all applicable</b>
        </button>
        {!items && <div style={{ padding: 10 }}><Spinner /></div>}
        {[...groups.entries()].map(([g, list]) => (
          <div key={g}>
            <div className="group">{g}</div>
            {list.map((t) => (
              <button key={t.name} className="item" disabled={t.status !== "available" || !t.applicable}
                title={`${t.description || t.title}\nSends: ${t.sends}\nTo: ${t.destinations.join(", ") || "local"}\nCost: ${t.cost}${t.status_note ? `\n${t.status_note}` : ""}`}
                onClick={() => void run([t.name])}>
                <span style={{ flex: 1 }}>
                  {t.title}
                  {t.already_run && <span className="small faint"> · already run</span>}
                  <div className="small faint">{t.connector_title}{t.cost && t.cost !== "Free" ? ` · ${t.cost}` : ""}</div>
                </span>
                {t.status !== "available" ? (
                  <a href={`#/sources/${t.connector}`} onClick={(e) => e.stopPropagation()}><SourceStatus status={t.status} /></a>
                ) : !t.applicable ? <span className="small faint">n/a</span> : null}
              </button>
            ))}
          </div>
        ))}
        {items && (
          <button className="item small muted" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Hide transforms that don't apply" : `Show all ${items.length} transforms for this type`}
          </button>
        )}
      </div>
    </>
  );
}
