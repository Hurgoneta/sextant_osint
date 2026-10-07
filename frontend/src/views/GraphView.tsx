import { useMemo, useRef, useState } from "react";
import type { Core } from "cytoscape";
import { api, inv } from "../api";
import GraphCanvas, { type GraphFilters, type LayoutName } from "../components/GraphCanvas";
import EdgePanel from "../components/EdgePanel";
import Inspector from "../components/Inspector";
import TransformMenu from "../components/TransformMenu";
import { useOntology } from "../ui";
import type { GraphStore } from "./Workspace";

interface Props {
  invId: string;
  store: GraphStore;
  version: number;
  selected: string | null;
  onSelect: (id: string | null) => void;
  reload: () => Promise<void>;
}

const LAYOUTS: [LayoutName, string][] = [["fcose", "Force (fcose)"], ["concentric", "Concentric by confidence"],
  ["breadthfirst", "Tree from input"], ["cose", "Force (classic)"], ["circle", "Circle"], ["grid", "Grid"]];

export default function GraphView({ invId, store, version, selected, onSelect, reload }: Props) {
  const [edge, setEdge] = useState<string | null>(null);
  const o = useOntology();
  const [filters, setFilters] = useState<GraphFilters>({ hiddenTypes: new Set(["source"]), minConfidence: 0, showInactive: true,
    search: "", showRejected: false, yearFrom: null, yearTo: null, hideUndated: false });
  const [layout, setLayout] = useState<LayoutName>("fcose");
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [group, setGroup] = useState(false);
  const [autoLayout, setAutoLayout] = useState(true);
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const [toast, setToast] = useState<string | null>(null);
  const [multi, setMulti] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const [newType, setNewType] = useState("username");
  const [newValue, setNewValue] = useState("");
  const cyRef = useRef<Core | null>(null);

  const ents = useMemo(() => [...store.ents.values()], [version]); // eslint-disable-line react-hooks/exhaustive-deps
  const rels = useMemo(() => [...store.rels.values()], [version]); // eslint-disable-line react-hooks/exhaustive-deps
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of ents) m.set(e.type, (m.get(e.type) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [ents]);

  function flash(msg: string) {
    setToast(msg);
    window.setTimeout(() => setToast(null), 4000);
  }

  function searchEnter() {
    const q = filters.search.trim().toLowerCase();
    if (!q) return;
    const hit = ents.find((e) => e.label.toLowerCase().includes(q) || e.value.toLowerCase().includes(q));
    if (hit) {
      onSelect(hit.id);
      setFocusNonce((n) => n + 1);
    }
  }

  async function mergeSelected() {
    const [a, b] = multi.map((id) => store.ents.get(id)).filter(Boolean) as { id: string; type: string; label: string }[];
    if (!a || !b || a.type !== b.type) return;
    if (!window.confirm(`Merge “${b.label}” into “${a.label}”? This is reversible from the inspector.`)) return;
    try {
      await api.post(inv(invId, "/merge"), { keep: a.id, merge: b.id });
      await reload();
      flash("Merged — the scores were recomputed.");
    } catch (e) {
      flash((e as Error).message);
    }
  }

  async function addEntity() {
    try {
      const r = await api.post<{ id: string }>(inv(invId, "/entities"), { type: newType, value: newValue, link_from: selected });
      setAdding(false);
      setNewValue("");
      await reload();
      onSelect(r.id);
    } catch (e) {
      flash((e as Error).message);
    }
  }

  function exportPng() {
    const cy = cyRef.current;
    if (!cy) return;
    const uri = cy.png({ full: true, scale: 2, bg: "#0a0e13" });
    const a = document.createElement("a");
    a.href = uri;
    a.download = `sextant-graph-${invId}.png`;
    a.click();
  }

  const multiSameType = multi.length === 2 && store.ents.get(multi[0])?.type === store.ents.get(multi[1])?.type;
  return (
    <div className={`graphwrap ${selected || edge ? "" : "noinspector"}`}>
      <div className="graph-area">
        <GraphCanvas ents={ents} rels={rels} version={version} colorOf={o.color} shapeOf={o.shape} titleOf={o.title}
          target={store.target} filters={filters} groupByType={group} layout={layout} layoutNonce={layoutNonce}
          autoLayout={autoLayout} selected={selected} focusNonce={focusNonce} onSelect={(id) => { setEdge(null); onSelect(id); }}
          onSelectEdge={(rid) => { onSelect(null); setEdge(rid); }}
          onContext={(id, x, y) => { onSelect(id); setMenu({ id, x, y }); }} onSelectionChange={setMulti}
          onReady={(cy) => { cyRef.current = cy; }} />
        <div className="graph-toolbar">
          <div className="floating">
            <input type="search" placeholder="Search entities…" value={filters.search}
              onChange={(e) => setFilters({ ...filters, search: e.target.value })} onKeyDown={(e) => e.key === "Enter" && searchEnter()} />
          </div>
          <div className="floating">
            <select value={layout} onChange={(e) => { setLayout(e.target.value as LayoutName); setLayoutNonce((n) => n + 1); }}>
              {LAYOUTS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
            <button className="btn sm" onClick={() => setLayoutNonce((n) => n + 1)} title="Re-run the layout">⟳ Layout</button>
            <button className="btn sm" onClick={() => cyRef.current?.fit(undefined, 40)} title="Fit to screen">⤢ Fit</button>
            <label className="check small"><input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} />Group by type</label>
            <label className="check small" title="Re-layout as new entities arrive (off = manual layout; drag nodes freely)">
              <input type="checkbox" checked={autoLayout} onChange={(e) => setAutoLayout(e.target.checked)} />Auto layout</label>
          </div>
          <div className="floating">
            <span className="small muted">Min. confidence</span>
            <input type="range" min={0} max={0.9} step={0.05} value={filters.minConfidence} style={{ width: 110 }}
              onChange={(e) => setFilters({ ...filters, minConfidence: Number(e.target.value) })} />
            <b className="small" style={{ width: 32 }}>{Math.round(filters.minConfidence * 100)}%</b>
            <label className="check small"><input type="checkbox" checked={filters.showInactive}
              onChange={(e) => setFilters({ ...filters, showInactive: e.target.checked })} />Ignored</label>
            <label className="check small"><input type="checkbox" checked={filters.showRejected}
              onChange={(e) => setFilters({ ...filters, showRejected: e.target.checked })} />Rejected links</label>
          </div>
          <div className="floating" title="Show entities whose dated facts (account created, domain registered, record date, publication…) fall in this range">
            <span className="small muted">Years</span>
            <input type="number" placeholder="from" min={1800} max={2200} value={filters.yearFrom ?? ""} style={{ width: 70 }}
              onChange={(e) => setFilters({ ...filters, yearFrom: e.target.value ? Number(e.target.value) : null })} />
            <span className="small muted">–</span>
            <input type="number" placeholder="to" min={1800} max={2200} value={filters.yearTo ?? ""} style={{ width: 70 }}
              onChange={(e) => setFilters({ ...filters, yearTo: e.target.value ? Number(e.target.value) : null })} />
            <label className="check small"><input type="checkbox" checked={filters.hideUndated}
              disabled={filters.yearFrom == null && filters.yearTo == null}
              onChange={(e) => setFilters({ ...filters, hideUndated: e.target.checked })} />Hide undated</label>
          </div>
          <div className="floating">
            {multi.length === 2 && <button className="btn sm warn" disabled={!multiSameType} onClick={() => void mergeSelected()}
              title={multiSameType ? "Merge the two selected entities" : "Select two entities of the same type"}>Merge selected</button>}
            <button className="btn sm" onClick={() => setAdding(!adding)}>＋ Entity</button>
            <button className="btn sm" onClick={exportPng}>PNG</button>
          </div>
          {adding && (
            <div className="floating">
              <select value={newType} onChange={(e) => setNewType(e.target.value)}>
                {o.list.filter((t) => t.id !== "source").map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
              </select>
              <input type="search" placeholder="value" value={newValue} onChange={(e) => setNewValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void addEntity()} />
              <button className="btn sm primary" onClick={() => void addEntity()}>Add{selected ? " & link to selected" : ""}</button>
            </div>
          )}
        </div>
        <div className="legend">
          {counts.map(([t, n]) => (
            <button key={t} className={filters.hiddenTypes.has(t) ? "off" : ""} onClick={() => {
              const s = new Set(filters.hiddenTypes);
              if (s.has(t)) s.delete(t);
              else s.add(t);
              setFilters({ ...filters, hiddenTypes: s });
            }}>
              <span className="dot" style={{ background: o.color(t) }} />{o.title(t)} {n}
            </button>
          ))}
          <span className="small faint" style={{ alignSelf: "center", marginLeft: 6 }}>Right-click a node for transforms · shift-click to multi-select</span>
        </div>
        {toast && <div className="floating" style={{ position: "absolute", bottom: 46, right: 12, zIndex: 6 }}>{toast}</div>}
        {ents.length === 0 && <div className="empty" style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center" }}>Waiting for the first results…</div>}
      </div>
      {!selected && edge && (
        <EdgePanel invId={invId} relId={edge} onSelect={(id) => { setEdge(null); onSelect(id); }} onClose={() => setEdge(null)} />
      )}
      {selected && (
        <Inspector invId={invId} entityId={selected} version={version} onSelect={(id) => { onSelect(id); setFocusNonce((n) => n + 1); }}
          onChanged={() => void reload()} onTransforms={(id) => setMenu({ id, x: window.innerWidth - 820, y: 120 })}
          onClose={() => onSelect(null)} />
      )}
      {menu && (
        <TransformMenu invId={invId} entity={store.ents.get(menu.id)} x={menu.x} y={menu.y} onClose={() => setMenu(null)} onQueued={flash} />
      )}
    </div>
  );
}
