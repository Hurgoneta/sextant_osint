import { useEffect, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition, type StylesheetJson } from "cytoscape";
import fcose from "cytoscape-fcose";
import type { EntitySummary, RelSummary } from "../types";

cytoscape.use(fcose);

export type LayoutName = "fcose" | "concentric" | "breadthfirst" | "circle" | "grid" | "cose";

export interface GraphFilters {
  hiddenTypes: Set<string>;
  minConfidence: number;
  showInactive: boolean;
  search: string;
  showRejected: boolean;
  yearFrom: number | null;           // show entities whose dated facts overlap [yearFrom, yearTo]
  yearTo: number | null;
  hideUndated: boolean;              // when a year range is set: also hide entities without any date
}

interface Props {
  ents: EntitySummary[];
  rels: RelSummary[];
  version: number;
  colorOf: (t: string) => string;
  shapeOf: (t: string) => string;
  titleOf: (t: string) => string;
  target: string | null;
  filters: GraphFilters;
  groupByType: boolean;
  layout: LayoutName;
  layoutNonce: number;
  autoLayout: boolean;
  selected: string | null;
  focusNonce: number;
  onSelect: (id: string | null) => void;
  onSelectEdge?: (relId: string) => void;   // "why are these connected?"
  onContext: (id: string, x: number, y: number) => void;
  onSelectionChange: (ids: string[]) => void;
  onReady?: (cy: Core) => void;
}

const STYLE: StylesheetJson = [
  { selector: "node", style: {
    "background-color": "data(color)", shape: "data(shape)" as never, width: "data(size)", height: "data(size)",
    label: "data(short)", color: "#c9d4e3", "font-size": 9, "text-valign": "bottom", "text-margin-y": 4,
    "text-wrap": "ellipsis", "text-max-width": "120px", "min-zoomed-font-size": 7, "border-width": 1.5,
    "border-color": "#0a0e13", opacity: "data(opacity)" as never, "text-outline-color": "#0a0e13", "text-outline-width": 2,
  } },
  { selector: "node[thumb]", style: { "background-image": "data(thumb)", "background-fit": "cover", "background-clip": "node" } },
  { selector: "node.seed", style: { "border-width": 3, "border-color": "#e3eaf2" } },
  { selector: "node.confirmed", style: { "border-width": 3, "border-color": "#2fbf71" } },
  { selector: "node.inactive", style: { "border-style": "dashed", "border-color": "#6b7787", "border-width": 2 } },
  { selector: "node.target", style: { "font-size": 12, "font-weight": "bold", color: "#ffffff" } },
  { selector: "node.match", style: { "border-width": 4, "border-color": "#f4b740", opacity: 1 } },
  { selector: "node.dim", style: { opacity: 0.12 } },
  { selector: "node:selected", style: { "border-width": 4, "border-color": "#4c8dff", "overlay-color": "#4c8dff", "overlay-opacity": 0.12 } },
  { selector: "node.hidden, edge.hidden", style: { display: "none" } },
  { selector: ":parent", style: {
    "background-color": "#121a24", "background-opacity": 0.35, "border-color": "#2d3a4c", "border-width": 1, label: "data(label)",
    "text-valign": "top", "font-size": 11, color: "#8a97a8", shape: "round-rectangle", padding: "18px",
  } },
  { selector: "edge", style: {
    width: "data(width)", "line-color": "#33435a", "target-arrow-color": "#33435a", "target-arrow-shape": "triangle",
    "arrow-scale": 0.7, "curve-style": "bezier", opacity: "data(opacity)" as never, label: "", "font-size": 8, color: "#8a97a8",
    "text-rotation": "autorotate", "text-background-color": "#0a0e13", "text-background-opacity": 0.8, "text-background-padding": "2px",
  } },
  { selector: "edge[type = 'SAME_AS']", style: { "line-style": "dashed", "line-color": "#a78bfa", "target-arrow-shape": "none" } },
  { selector: "edge[type = 'OWNS'], edge[type = 'USES']", style: { "line-color": "#3d5a85", "target-arrow-color": "#3d5a85" } },
  { selector: "edge.rejected", style: { "line-style": "dotted", "line-color": "#7a2a2e", "target-arrow-color": "#7a2a2e" } },
  { selector: "edge.showlabel, edge:selected", style: { label: "data(type)" } },
  { selector: "edge.dim", style: { opacity: 0.06 } },
];

function nodeData(e: EntitySummary, p: Props) {
  const seedish = e.status === "seed" || e.status === "confirmed";
  const weight = seedish ? 1 : Math.max(e.confidence, e.value_score);
  const inactive = e.status === "ignored" || e.status === "separated";
  return {
    id: e.id, label: e.label, short: e.label.length > 34 ? `${e.label.slice(0, 32)}…` : e.label, type: e.type,
    color: p.colorOf(e.type), shape: e.id === p.target ? "star" : p.shapeOf(e.type),
    size: e.id === p.target ? 50 : seedish ? 36 : 14 + 24 * weight,
    opacity: inactive ? 0.3 : seedish ? 1 : 0.45 + 0.55 * Math.min(1, weight + 0.15),
    confidence: e.confidence, status: e.status, value: e.value, ...(e.thumb ? { thumb: e.thumb } : {}),
    y0: e.years ? e.years[0] : null, y1: e.years ? e.years[1] : null,
    ...(p.groupByType ? { parent: `grp:${e.type}` } : {}),
  };
}

function nodeClasses(e: EntitySummary, target: string | null): string {
  const c = [e.status === "seed" ? "seed" : "", e.status === "confirmed" ? "confirmed" : "",
    e.status === "ignored" || e.status === "separated" ? "inactive" : "", e.id === target ? "target" : ""];
  return c.filter(Boolean).join(" ");
}

function edgeData(r: RelSummary) {
  return { id: r.id, source: r.source, target: r.target, type: r.type, width: 0.8 + 2.2 * r.confidence,
    opacity: r.status === "rejected" ? 0.35 : 0.35 + 0.55 * r.confidence };
}

export default function GraphCanvas(p: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const propsRef = useRef(p);
  propsRef.current = p;
  const lastLayout = useRef(0);
  const pendingLayout = useRef<number | null>(null);
  const grouped = useRef<boolean | null>(null);

  // ---------------------------------------------------------------- init
  useEffect(() => {
    if (!ref.current) return;
    const cy = cytoscape({ container: ref.current, style: STYLE, minZoom: 0.05, maxZoom: 4,
      boxSelectionEnabled: true, selectionType: "additive" });
    cyRef.current = cy;
    propsRef.current.onReady?.(cy);
    cy.on("tap", "node", (ev) => {
      if (ev.target.isParent()) return;
      propsRef.current.onSelect(ev.target.id());
    });
    cy.on("tap", "edge", (ev) => propsRef.current.onSelectEdge?.(ev.target.id()));
    cy.on("tap", (ev) => {
      if (ev.target === cy) propsRef.current.onSelect(null);
    });
    cy.on("cxttap", "node", (ev) => {
      if (ev.target.isParent()) return;
      const oe = ev.originalEvent as MouseEvent;
      propsRef.current.onContext(ev.target.id(), oe.clientX, oe.clientY);
    });
    cy.on("mouseover", "edge", (ev) => ev.target.addClass("showlabel"));
    cy.on("mouseout", "edge", (ev) => ev.target.removeClass("showlabel"));
    cy.on("mouseover", "node", (ev) => ev.target.connectedEdges().addClass("showlabel"));
    cy.on("mouseout", "node", (ev) => ev.target.connectedEdges().removeClass("showlabel"));
    cy.on("select unselect", "node", () => propsRef.current.onSelectionChange(cy.$("node:selected").map((n) => n.id())));
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // ---------------------------------------------------------------- sync elements
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const regroup = grouped.current !== p.groupByType;
    grouped.current = p.groupByType;
    const added: string[] = [];
    cy.batch(() => {
      if (regroup) cy.elements().remove();
      if (p.groupByType) {
        const types = new Set(p.ents.map((e) => e.type));
        types.forEach((t) => {
          if (cy.getElementById(`grp:${t}`).empty()) cy.add({ group: "nodes", data: { id: `grp:${t}`, label: p.titleOf(t) } });
        });
      }
      const seen = new Set<string>();
      for (const e of p.ents) {
        seen.add(e.id);
        const ele = cy.getElementById(e.id);
        const data = nodeData(e, p);
        if (ele.empty()) {
          const def: ElementDefinition = { group: "nodes", data, classes: nodeClasses(e, p.target) };
          const added_ = cy.add(def);
          // place new nodes next to an already-positioned neighbour so the graph grows organically
          const rel = p.rels.find((r) => (r.source === e.id && !cy.getElementById(r.target).empty()) ||
            (r.target === e.id && !cy.getElementById(r.source).empty()));
          if (rel) {
            const other = cy.getElementById(rel.source === e.id ? rel.target : rel.source);
            const pos = other.position();
            added_.position({ x: pos.x + (Math.random() - 0.5) * 120, y: pos.y + (Math.random() - 0.5) * 120 });
          }
          added.push(e.id);
        } else {
          ele.data(data);
          ele.classes(nodeClasses(e, p.target) + (ele.hasClass("match") ? " match" : ""));
        }
      }
      cy.nodes().forEach((n) => {
        if (!n.isParent() && !seen.has(n.id())) n.remove();
      });
      const relIds = new Set<string>();
      for (const r of p.rels) {
        if (!seen.has(r.source) || !seen.has(r.target)) continue;
        relIds.add(r.id);
        const ele = cy.getElementById(r.id);
        if (ele.empty()) {
          cy.add({ group: "edges", data: edgeData(r), classes: r.status === "rejected" ? "rejected" : "" });
          added.push(r.id);
        } else {
          ele.data(edgeData(r));
          ele.toggleClass("rejected", r.status === "rejected");
        }
      }
      cy.edges().forEach((ed) => {
        if (!relIds.has(ed.id())) ed.remove();
      });
      if (p.groupByType) cy.nodes(":parent").forEach((g) => { if (g.children().empty()) g.remove(); });
    });
    applyFilters(cy, p);
    if (regroup || (added.length && p.autoLayout)) scheduleLayout(regroup || cy.nodes().length < 3);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.version, p.ents, p.rels, p.groupByType]);

  // ---------------------------------------------------------------- filters, search, selection
  useEffect(() => {
    const cy = cyRef.current;
    if (cy) applyFilters(cy, p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.filters]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.$("node:selected").forEach((n) => { if (n.id() !== p.selected) n.unselect(); });
    if (p.selected) {
      const n = cy.getElementById(p.selected);
      if (!n.empty()) n.select();
    }
  }, [p.selected, p.version]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !p.selected || !p.focusNonce) return;
    const n = cy.getElementById(p.selected);
    if (!n.empty()) cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 1.1) }, { duration: 350 });
  }, [p.focusNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (p.layoutNonce) scheduleLayout(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.layoutNonce, p.layout]);

  function scheduleLayout(full: boolean) {
    const wait = Math.max(0, 2500 - (Date.now() - lastLayout.current));
    if (pendingLayout.current !== null) {
      if (!full) return;
      window.clearTimeout(pendingLayout.current);
    }
    pendingLayout.current = window.setTimeout(() => {
      pendingLayout.current = null;
      runLayout(full);
    }, full ? 0 : wait);
  }

  function runLayout(full: boolean) {
    const cy = cyRef.current;
    if (!cy) return;
    lastLayout.current = Date.now();
    const pp = propsRef.current;
    const eles = cy.elements(":visible");
    const n = eles.nodes().length;
    const common = { animate: n < 600, animationDuration: 450, fit: full, padding: 40 };
    const opts: Record<string, unknown> = (() => {
      switch (pp.layout) {
        case "concentric":
          return { name: "concentric", concentric: (node: cytoscape.NodeSingular) => (node.id() === pp.target ? 10 : (node.data("confidence") ?? 0) * 8),
            levelWidth: () => 1.5, minNodeSpacing: 18, ...common };
        case "breadthfirst":
          return { name: "breadthfirst", directed: false, roots: cy.nodes(".seed"), spacingFactor: 1.1, ...common };
        case "circle":
          return { name: "circle", ...common };
        case "grid":
          return { name: "grid", ...common };
        case "cose":
          return { name: "cose", nodeRepulsion: 9000, idealEdgeLength: 90, ...common };
        default:
          return { name: "fcose", quality: n > 1500 ? "draft" : "default", randomize: full, nodeRepulsion: 6500,
            idealEdgeLength: 85, nodeSeparation: 60, packComponents: true, tile: true, ...common };
      }
    })();
    eles.layout(opts as unknown as cytoscape.LayoutOptions).run();
  }

  return <div ref={ref} className="cy" />;
}

function applyFilters(cy: Core, p: Props) {
  const f = p.filters;
  const q = f.search.trim().toLowerCase();
  const ranged = f.yearFrom != null || f.yearTo != null;
  const outOfRange = (n: cytoscape.NodeSingular) => {
    if (!ranged || n.data("status") === "seed") return false;
    const y0 = n.data("y0") as number | null, y1 = n.data("y1") as number | null;
    if (y0 == null || y1 == null) return f.hideUndated;
    return (f.yearFrom != null && y1 < f.yearFrom) || (f.yearTo != null && y0 > f.yearTo);
  };
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      if (n.isParent()) return;
      const hide = f.hiddenTypes.has(n.data("type")) || outOfRange(n) ||
        (!f.showInactive && (n.data("status") === "ignored" || n.data("status") === "separated")) ||
        (n.data("status") !== "seed" && n.data("status") !== "confirmed" && n.data("confidence") < f.minConfidence &&
          ["account", "website", "document", "publication", "public_record", "person"].includes(n.data("type")));
      n.toggleClass("hidden", hide);
      const match = !!q && (String(n.data("label")).toLowerCase().includes(q) || String(n.data("value")).toLowerCase().includes(q));
      n.toggleClass("match", match);
      n.toggleClass("dim", !!q && !match);
    });
    cy.edges().forEach((e) => {
      e.toggleClass("hidden", (!f.showRejected && e.hasClass("rejected")) || e.source().hasClass("hidden") || e.target().hasClass("hidden"));
      e.toggleClass("dim", !!q && !(e.source().hasClass("match") || e.target().hasClass("match")));
    });
  });
}
