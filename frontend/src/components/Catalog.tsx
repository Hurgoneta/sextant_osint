// The catalog's user layer in the UI: one source form (add source / add link / edit), duplicate warnings, the
// jurisdiction audit, category and platform management, and the one catalog reset. Every screen uses these, so a
// source is created, edited and removed the same way wherever it is shown.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import type { Automation, CatalogVocab, DuplicateMatch, LinkCheck, SourceDraft, UserPlatform, UserSource } from "../types";
import { Badge, ErrorBox, Ext, Spinner, fmtDate, useApi, useDebounced } from "../ui";

// ---------------------------------------------------------------- vocabulary

let vocabCache: CatalogVocab | null = null;

export function useVocab(): { vocab: CatalogVocab | null; reload: () => Promise<void> } {
  const [vocab, setVocab] = useState<CatalogVocab | null>(vocabCache);
  async function reload() {
    const v = await api.get<CatalogVocab>("/api/catalog/vocab");
    vocabCache = v;
    setVocab(v);
  }
  useEffect(() => {
    void reload();
  }, []);
  return { vocab, reload };
}

// ---------------------------------------------------------------- small pieces

export function Modal({ title, onClose, children, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <>
      <div className="modal-bg" onClick={onClose} />
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        <div className="row between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="btn ghost sm" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </>
  );
}

export function OriginBadge({ origin }: { origin?: string }) {
  return origin === "user" ? <Badge kind="violet" title="Added by you. The catalog reset removes it.">Yours</Badge> : null;
}

const AUTOMATION_KIND: Record<string, string> = { connector: "good", manual: "", none: "", missing_connector: "bad" };

export function AutomationBadge({ a, quiet }: { a?: Automation; quiet?: boolean }) {
  // quiet: only when it adds something to a "Manual only" status badge (a linked or missing connector)
  if (!a || (quiet && a.mode !== "connector" && a.mode !== "missing_connector")) return null;
  const label = a.mode === "connector" ? `Automated: ${a.connector_title ?? a.connector}` : a.mode === "manual" ? "Manual"
    : a.mode === "missing_connector" ? "Connector missing" : "Not automated";
  return <Badge kind={AUTOMATION_KIND[a.mode] ?? ""} title={a.title}>{label}</Badge>;
}

const VER: Record<string, [string, string]> = { ok: ["good", "link OK"], reachable_refuses_bots: ["info", "reachable (blocks bots)"],
  tls_problem: ["warn", "TLS issue"], unreachable: ["warn", "unreachable from here"], server_error: ["warn", "server error"], dead: ["bad", "dead link"] };

export function LinkCheckBadge({ v }: { v?: { result?: string; checked_at?: string } | null }) {
  const [k, l] = VER[v?.result ?? ""] ?? ["", "not verified"];
  return <Badge kind={k} title={v?.checked_at ? `checked ${fmtDate(v.checked_at)}` : "Never checked"}>{l}</Badge>;
}

interface Option { value: string; label: string }

export function ChipsInput({ values, onChange, options, placeholder, listId }: {
  values: string[]; onChange: (v: string[]) => void; options: Option[]; placeholder?: string; listId: string;
}) {
  const [text, setText] = useState("");
  const byValue = useMemo(() => new Map(options.map((o) => [o.value, o.label])), [options]);
  const byLabel = useMemo(() => new Map(options.map((o) => [o.label.toLowerCase(), o.value])), [options]);
  function resolve(raw: string): string | undefined {
    const t = raw.trim();
    if (!t) return undefined;
    if (byValue.has(t)) return t;
    if (byValue.has(t.toUpperCase())) return t.toUpperCase();
    const exact = byLabel.get(t.toLowerCase());
    if (exact) return exact;
    const starts = options.filter((o) => o.label.toLowerCase().startsWith(t.toLowerCase()));
    return starts.length === 1 ? starts[0].value : undefined;
  }
  function commit(raw: string): boolean {
    const v = resolve(raw);
    if (!v) return false;
    if (!values.includes(v)) onChange([...values, v]);
    setText("");
    return true;
  }
  return (
    <div className="chips-input">
      {values.map((v) => (
        <span key={v} className="chip">{byValue.get(v) ?? v}
          <button type="button" className="chip-x" aria-label={`Remove ${byValue.get(v) ?? v}`}
            onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
        </span>
      ))}
      <input type="text" list={listId} value={text} placeholder={values.length ? "" : placeholder}
        onChange={(e) => (byLabel.has(e.target.value.toLowerCase()) ? commit(e.target.value) : setText(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit(text);
          } else if (e.key === "Backspace" && !text && values.length) onChange(values.slice(0, -1));
        }}
        onBlur={() => commit(text)} />
      <datalist id={listId}>{options.filter((o) => !values.includes(o.value)).map((o) => <option key={o.value} value={o.label} />)}</datalist>
    </div>
  );
}

function jurisdictionOptions(v: CatalogVocab | null): Option[] {
  return (v?.jurisdictions ?? []).map((j) => ({ value: j.code, label: `${j.name} (${j.code})` }));
}

// ---------------------------------------------------------------- duplicates

function DuplicateRows({ items, onEdit }: { items: DuplicateMatch[]; onEdit?: (m: DuplicateMatch) => void }) {
  const TYPE: Record<string, string> = { connector: "Connector", official_source: "Official source", user_source: "Your source",
    platform: "Platform", user_platform: "Your platform" };
  return (
    <table className="data">
      <tbody>
        {items.map((m) => (
          <tr key={`${m.type}:${m.id}`}>
            <td><b>{m.title}</b> <OriginBadge origin={m.origin} />
              <div className="small faint wrapword">{m.url}</div></td>
            <td className="small">{TYPE[m.type] ?? m.type}{m.jurisdictions.length > 0 && <div className="faint">{m.jurisdictions.join(", ")}</div>}</td>
            <td className="small"><Badge kind={m.strength === "likely" ? "warn" : ""}>{m.strength}</Badge>
              <div className="muted">{m.reasons.join(" · ")}</div></td>
            <td className="row" style={{ justifyContent: "flex-end" }}>
              <a className="btn sm" href={m.href}>Open existing</a>
              {m.editable && onEdit && <button className="btn sm" onClick={() => onEdit(m)}>Edit existing</button>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------- the source form (add source / add link / edit)

interface Draft {
  title: string; url: string; category: string; source_type: string; access: string; kind: string; jurisdictions: string[];
  platforms: string[]; connector: string; inputs: string[]; official: boolean; name_search: boolean; description: string;
  person_data: string; notes: string; review: string; provider_class: string; pricing: string; auth: string;
  terms_url: string; data_types: string[]; access_models: string[]; api_available: string; web_app: boolean;
  documentation_url: string; coverage: string; limitations: string; last_verified: string;
}
const EMPTY: Draft = { title: "", url: "", category: "", source_type: "reference", access: "unknown", kind: "", jurisdictions: [],
  platforms: [], connector: "", inputs: [], official: false, name_search: false, description: "", person_data: "", notes: "",
  review: "not_verified", provider_class: "public", pricing: "", auth: "", terms_url: "", data_types: [], access_models: [],
  api_available: "unknown", web_app: false, documentation_url: "", coverage: "", limitations: "", last_verified: "" };
const ACCESS_MODELS: [string, string][] = [["free", "Free"], ["free_tier", "Free tier"], ["trial", "Trial"],
  ["per_query", "Per query / credits"], ["subscription", "Subscription"], ["enterprise", "Enterprise"], ["manual", "Manual (web app)"]];

function draftOf(s?: SourceDraft | UserSource | null): Partial<Draft> {
  const out: Record<string, unknown> = {};
  if (!s) return out;
  for (const k of Object.keys(EMPTY)) {
    const v = (s as Record<string, unknown>)[k];
    if (v !== null && v !== undefined) out[k] = v;
  }
  return out as Partial<Draft>;
}

export function SourceForm({ initial, editId, mode: startMode = "source", onClose, onSaved }: {
  initial?: SourceDraft | UserSource | null; editId?: string; mode?: "source" | "link"; onClose: () => void;
  onSaved: (s: UserSource) => void;
}) {
  const { vocab, reload } = useVocab();
  const [mode, setMode] = useState(startMode);
  const [id, setId] = useState(editId);
  const [f, setF] = useState<Draft>({ ...EMPTY, ...(startMode === "source" && !initial?.source_type ? { source_type: "manual" } : {}),
    ...draftOf(initial) });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [dups, setDups] = useState<DuplicateMatch[] | null>(null);
  const [hints, setHints] = useState<DuplicateMatch[]>([]);
  const [newCat, setNewCat] = useState<{ title: string; group: string } | null>(null);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setF((cur) => ({ ...cur, [k]: v }));
  const probe = useDebounced(JSON.stringify({ title: f.title, url: f.url, jurisdictions: f.jurisdictions }), 450);

  useEffect(() => {
    const q = JSON.parse(probe) as { title: string; url: string; jurisdictions: string[] };
    if (q.title.trim().length < 3 && !/^https?:\/\/\S+\.\S+/i.test(q.url)) {
      setHints([]);
      return;
    }
    let live = true;
    api.post<{ duplicates: DuplicateMatch[] }>("/api/catalog/duplicates", { ...q, exclude_id: id ?? null })
      .then((d) => live && setHints(d.duplicates))
      .catch(() => live && setHints([]));
    return () => {
      live = false;
    };
  }, [probe, id]);

  async function createCategory() {
    if (!newCat) return;
    try {
      const c = await api.post<{ id: string }>("/api/catalog/categories", { ...newCat, description: "" });
      await reload();
      set("category", c.id);
      setNewCat(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function save(confirmed: boolean) {
    setBusy(true);
    setErr(null);
    try {
      if (!confirmed && !id) {
        const d = await api.post<{ duplicates: DuplicateMatch[] }>("/api/catalog/duplicates",
          { title: f.title, url: f.url, jurisdictions: f.jurisdictions, platforms: f.platforms, connector: f.connector });
        if (d.duplicates.some((m) => m.strength === "likely")) {
          setDups(d.duplicates);
          return;
        }
      }
      const body = { ...f, source_type: mode === "link" ? "reference" : f.source_type, allow_duplicate: confirmed };
      const s = id ? await api.put<UserSource>(`/api/catalog/sources/${id}`, body) : await api.post<UserSource>("/api/catalog/sources", body);
      onSaved(s);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setDups((e.detail as { duplicates?: DuplicateMatch[] })?.duplicates ?? []);
      else setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function editExisting(m: DuplicateMatch) {
    try {
      const item = await api.get<{ record: UserSource }>(`/api/catalog/sources/${m.id}`);
      setId(m.id);
      setF({ ...EMPTY, ...draftOf(item.record) });
      setMode("source");
      setDups(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (!vocab) return <Modal title="Loading…" onClose={onClose}><Spinner /></Modal>;
  const juris = jurisdictionOptions(vocab);
  const sysCats = vocab.categories.filter((c) => c.origin === "system");
  const userCats = vocab.categories.filter((c) => c.origin === "user");
  const title = id ? `Edit “${f.title || "source"}”` : mode === "link" ? "Add link" : "Add source";
  const type = vocab.source_types[f.source_type];

  if (dups) {
    return (
      <Modal title="This source may already exist" onClose={onClose} wide>
        <p className="small muted" style={{ marginTop: 0 }}>The catalog already has entries that look like “{f.title}” ({f.url}). Open or edit one
          of them, or save yours anyway if it really is a different source.</p>
        <div className="card"><DuplicateRows items={dups} onEdit={(m) => void editExisting(m)} /></div>
        <ErrorBox error={err} />
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" onClick={() => setDups(null)}>Back to the form</button>
          <button className="btn warn" disabled={busy} onClick={() => void save(true)}>Continue anyway</button>
        </div>
      </Modal>
    );
  }

  const categoryField = (
    <label className="field">Category *
      <select value={newCat ? "__new__" : f.category} onChange={(e) => (e.target.value === "__new__" ? setNewCat({ title: "", group: "other" })
        : (setNewCat(null), set("category", e.target.value)))}>
        <option value="" disabled>Choose a category…</option>
        {userCats.length > 0 && <optgroup label="Your categories">{userCats.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}</optgroup>}
        <optgroup label="Built-in categories">{sysCats.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}</optgroup>
        <option value="__new__">＋ New category…</option>
      </select>
    </label>
  );
  const jurisdictionField = (
    <label className="field"><span>Jurisdiction(s) <span className="faint">(empty = global)</span></span>
      <ChipsInput listId="cat-juris" values={f.jurisdictions} onChange={(v) => set("jurisdictions", v)} options={juris}
        placeholder="Type a country or code, e.g. Sweden or SE" />
    </label>
  );
  return (
    <Modal title={title} onClose={onClose} wide>
      {mode === "link" && (
        <div className="notice" style={{ marginBottom: 12 }}>
          A reference link is a website stored for you to open. It is <b>not</b> an automated connector: Sextant never queries it.
        </div>
      )}
      <div className="grid2">
        <label className="field">Name *<input type="text" value={f.title} autoFocus onChange={(e) => set("title", e.target.value)} /></label>
        <label className="field">URL *<input type="text" value={f.url} placeholder="https://…" onChange={(e) => set("url", e.target.value)} />
          <span className="small faint">Saving an address never makes Sextant contact it.</span></label>
        {categoryField}
        {jurisdictionField}
      </div>
      {newCat && (
        <div className="card pad row wrap" style={{ marginTop: 10, alignItems: "flex-end" }}>
          <label className="field" style={{ flex: 2 }}>New category name<input type="text" value={newCat.title}
            onChange={(e) => setNewCat({ ...newCat, title: e.target.value })} /></label>
          <label className="field" style={{ flex: 1 }}>Shown in jurisdiction views under
            <select value={newCat.group} onChange={(e) => setNewCat({ ...newCat, group: e.target.value })}>
              {Object.entries(vocab.groups).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select></label>
          <button className="btn primary sm" disabled={newCat.title.trim().length < 2} onClick={() => void createCategory()}>Create category</button>
          <button className="btn ghost sm" onClick={() => setNewCat(null)}>Cancel</button>
        </div>
      )}
      {mode === "source" && (
        <>
          <div className="grid2" style={{ marginTop: 12 }}>
            <label className="field">Type
              <select value={f.source_type} onChange={(e) => set("source_type", e.target.value)}>
                {Object.entries(vocab.source_types).map(([k, v]) => <option key={k} value={k}>{v.title}</option>)}
              </select>
              {type && <span className="small faint">{type.help}</span>}</label>
            <label className="field">Access
              <select value={f.access} onChange={(e) => set("access", e.target.value)}>
                {Object.entries(vocab.access).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select></label>
            <label className="field"><span>Automated by an existing connector <span className="faint">(optional)</span></span>
              <select value={f.connector} onChange={(e) => set("connector", e.target.value)}>
                <option value="">None (not automated)</option>
                {vocab.connectors.map((c) => <option key={c.name} value={c.name}>{c.title} · {c.category_title}</option>)}
              </select>
              <span className="small faint">Only when a connector of this installation already queries this source. Its status then applies.</span></label>
            <label className="field"><span>Platform(s) <span className="faint">(optional)</span></span>
              <ChipsInput listId="cat-plat" values={f.platforms} onChange={(v) => set("platforms", v)}
                options={vocab.platforms.map((p) => ({ value: p.id, label: p.origin === "user" ? `${p.name} (yours)` : p.name }))}
                placeholder="e.g. GitHub" /></label>
            <label className="field"><span>Can be searched by <span className="faint">(what you would enter there)</span></span>
              <ChipsInput listId="cat-inputs" values={f.inputs} onChange={(v) => set("inputs", v)}
                options={vocab.entity_types.map((t) => ({ value: t.id, label: t.title }))} placeholder="e.g. Person, Organization" /></label>
            <label className="field"><span>Official source type <span className="faint">(optional)</span></span>
              <select value={f.kind} onChange={(e) => set("kind", e.target.value)}>
                <option value="">—</option>
                {Object.entries(vocab.kinds).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select></label>
            <label className="field">Personal data it shows<input type="text" value={f.person_data} placeholder="e.g. party names in judgments"
              onChange={(e) => set("person_data", e.target.value)} /></label>
            <label className="field">Status
              <select value={f.review} onChange={(e) => set("review", e.target.value)}>
                {Object.entries(vocab.review).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select></label>
            <label className="field">Provider type
              <select value={f.provider_class} onChange={(e) => set("provider_class", e.target.value)}>
                {Object.entries(vocab.provider_classes).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              {f.provider_class === "commercial" && <span className="small faint">Listed only: it is never queried until a connector
                configured with your own licence exists.</span>}</label>
            {f.provider_class !== "public" && <>
              <label className="field">Pricing / access terms<input type="text" value={f.pricing} placeholder="e.g. per lookup, annual contract"
                onChange={(e) => set("pricing", e.target.value)} /></label>
              <label className="field">Authentication<input type="text" value={f.auth} placeholder="e.g. API key + contract"
                onChange={(e) => set("auth", e.target.value)} /></label>
              <label className="field">Terms / licence URL<input type="text" value={f.terms_url} placeholder="https://…"
                onChange={(e) => set("terms_url", e.target.value)} /></label>
              <label className="field">Data categories (as the provider describes them)<input type="text" value={f.data_types.join(", ")}
                placeholder="e.g. business contact data, company data"
                onChange={(e) => set("data_types", e.target.value.split(",").map((x) => x.trim()).filter(Boolean))} /></label>
              <div className="field"><span>Access model</span>
                <div className="row wrap" style={{ gap: 8 }}>{ACCESS_MODELS.map(([k, v]) => (
                  <label key={k} className="check small"><input type="checkbox" checked={f.access_models.includes(k)}
                    onChange={(e) => set("access_models", e.target.checked ? [...f.access_models, k] : f.access_models.filter((x) => x !== k))} />
                    {v}</label>))}</div></div>
              <label className="field">API
                <select value={f.api_available} onChange={(e) => set("api_available", e.target.value)}>
                  <option value="unknown">Unknown</option><option value="yes">Documented API</option><option value="no">No API</option>
                </select></label>
              <label className="check small"><input type="checkbox" checked={f.web_app} onChange={(e) => set("web_app", e.target.checked)} />
                Web application</label>
              <label className="field">Documentation URL<input type="text" value={f.documentation_url} placeholder="https://…"
                onChange={(e) => set("documentation_url", e.target.value)} /></label>
              <label className="field">Coverage<input type="text" value={f.coverage} placeholder="e.g. Nordic countries, B2B only"
                onChange={(e) => set("coverage", e.target.value)} /></label>
              <label className="field">Known limitations<input type="text" value={f.limitations}
                onChange={(e) => set("limitations", e.target.value)} /></label>
              <label className="field">Last verified<input type="date" value={f.last_verified}
                onChange={(e) => set("last_verified", e.target.value)} /></label>
            </>}
          </div>
          <div className="row wrap" style={{ marginTop: 10, gap: 18 }}>
            <label className="check small"><input type="checkbox" checked={f.official} onChange={(e) => set("official", e.target.checked)} />
              Official / public-record source</label>
            <label className="check small"><input type="checkbox" checked={f.name_search} onChange={(e) => set("name_search", e.target.checked)} />
              Anyone can search it by a person's name</label>
          </div>
          <label className="field" style={{ marginTop: 10 }}>Description
            <textarea rows={2} value={f.description} onChange={(e) => set("description", e.target.value)} /></label>
        </>
      )}
      <label className="field" style={{ marginTop: 10 }}>Notes<textarea rows={2} value={f.notes} onChange={(e) => set("notes", e.target.value)} /></label>
      {hints.length > 0 && (
        <div className="notice warn small" style={{ marginTop: 10 }}>
          <b>Similar entries already exist:</b> {hints.slice(0, 4).map((h) => `${h.title} (${h.reasons.join(", ").toLowerCase()})`).join(" · ")}
          {hints.length > 4 && ` · ${hints.length - 4} more`}
        </div>
      )}
      <ErrorBox error={err} />
      <div className="row between" style={{ marginTop: 14 }}>
        {mode === "link" ? <button className="btn ghost sm" onClick={() => setMode("source")}>More fields…</button> : <span />}
        <div className="row">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || f.title.trim().length < 2 || !f.url.trim() || !f.category}
            onClick={() => void save(false)}>{busy ? <Spinner /> : null}{id ? "Save changes" : mode === "link" ? "Save link" : "Save source"}</button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- actions on one of your sources

export function UserSourceActions({ s, onEdit, onChanged }: { s: Pick<UserSource, "id" | "title" | "enabled">; onEdit: () => void;
  onChanged: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<string | null>(null);
  async function act(what: string, fn: () => Promise<unknown>) {
    setBusy(what);
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  if (confirm) {
    return (
      <span className="row wrap small" style={{ gap: 6 }}>
        Delete “{s.title}” everywhere?
        <button className="btn bad sm" disabled={!!busy} onClick={() => void act("delete", () => api.del(`/api/catalog/sources/${s.id}`))}>Delete</button>
        <button className="btn ghost sm" onClick={() => setConfirm(false)}>Keep</button>
      </span>
    );
  }
  return (
    <span className="row wrap" style={{ gap: 4 }}>
      <button className="btn sm" onClick={onEdit}>Edit</button>
      <button className="btn sm" disabled={!!busy} title={s.enabled ? "Keep it, but stop counting and showing it as active" : "Switch it back on"}
        onClick={() => void act("toggle", () => api.patch(`/api/catalog/sources/${s.id}`, { enabled: !s.enabled }))}>
        {s.enabled ? "Disable" : "Enable"}</button>
      <button className="btn sm" disabled={!!busy} title="One request to the source's address (sends no personal data)"
        onClick={() => void act("check", () => api.post(`/api/catalog/sources/${s.id}/check-link`))}>
        {busy === "check" ? <Spinner /> : null}Check link</button>
      <button className="btn ghost sm" onClick={() => setConfirm(true)}>Delete</button>
      {err && <span className="small" style={{ color: "var(--bad)" }}>{err}</span>}
    </span>
  );
}

// ---------------------------------------------------------------- jurisdiction audit (read-only)

interface AuditCoverage { group: string; title: string; core: boolean; rating: string; rating_title: string; rating_kind: string;
  system: number; user: number; verified_links: number; unverified_links: number; potential: number; notes: string[];
  connectors: { name: string; title: string; status: string; status_title: string; scope: string }[] }
interface AuditRec { id: string; title: string; url: string; jurisdiction_name: string; kind_title: string; group_title: string;
  type_title: string; reason: string; confidence: string; confidence_reasons: string[]; status: string; nature: string;
  link_check: LinkCheck | null; provenance: { dataset: string; ref: string; retrieved_at: string | null }; prefill: SourceDraft }
interface AuditCurrent { id: string; title: string; url: string; origin: string; layer: string; kind_title: string; nature: string;
  automation: string; evidence: string; evidence_kind: string; review?: string; active?: boolean }
interface AuditT {
  code: string; name: string; audited_at: string;
  read_from: { catalog_version: number; your_sources: number; built_in_sources: number; dataset_entries: number; potential_sources: number;
    country_connectors: number; global_connectors: number; live_search_at: string | null; links_checked_at: string | null };
  legal: { notes: string; criminal_records_public: boolean; judgments_anonymized: boolean | null };
  coverage: AuditCoverage[]; gaps: { group: string; title: string; message: string; notes: string[] }[];
  sources: AuditCurrent[]; duplicates: { reason: string; strength: string; suggestion: string; items: { id: string; title: string; url: string; origin: string; layer: string }[] }[];
  recommendations: AuditRec[]; recommendations_total: number; ignored: AuditRec[];
  improvements: { kind: string; title: string; href: string }[]; legend: Record<string, string>; disclaimer: string;
}

const CONF_KIND: Record<string, string> = { high: "good", medium: "warn", low: "bad" };
const AUTO_LABEL: Record<string, [string, string]> = { automated: ["good", "Automated"], automatable: ["warn", "Automatable (needs setup)"],
  manual: ["", "Manual"] };

function RecCard({ r, onAdd, onIgnore, ignored }: { r: AuditRec; onAdd: () => void; onIgnore: () => void; ignored?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="src-card" style={{ cursor: "default" }}>
      <div className="row between"><b className="wrapword">{r.title}</b><Badge kind={CONF_KIND[r.confidence]} title="Confidence from the evidence below">{r.confidence} confidence</Badge></div>
      <div className="small muted">{r.jurisdiction_name} · {r.group_title} · {r.kind_title} · {r.type_title} · {r.nature}</div>
      <div className="small">{r.reason}</div>
      <div className="row wrap small" style={{ gap: 6 }}><Badge>{r.status}</Badge><LinkCheckBadge v={r.link_check} /></div>
      {open && (
        <div className="small">
          <div className="wrapword"><Ext href={r.url}>{r.url}</Ext></div>
          <ul style={{ margin: "4px 0", paddingLeft: 18 }}>{r.confidence_reasons.map((x) => <li key={x}>{x}</li>)}</ul>
          <span className="faint">Provenance: {r.provenance.dataset} {r.provenance.ref}{r.provenance.retrieved_at ? `, ${r.provenance.retrieved_at.slice(0, 10)}` : ""}</span>
        </div>
      )}
      <div className="row" style={{ gap: 6 }}>
        <button className="btn sm" onClick={() => setOpen(!open)}>{open ? "Hide" : "Review"}</button>
        <button className="btn primary sm" onClick={onAdd}>Add…</button>
        <button className="btn ghost sm" onClick={onIgnore}>{ignored ? "Restore" : "Ignore"}</button>
      </div>
    </div>
  );
}

export function AuditPanel({ code, onAdd, onClose }: { code: string; onAdd: (prefill: SourceDraft) => void; onClose: () => void }) {
  const { data, error, reload, loading } = useApi<AuditT>(`/api/catalog/audit/${code}`);
  const [showIgnored, setShowIgnored] = useState(false);
  async function ignore(id: string, ignored: boolean) {
    await api.post(`/api/catalog/audit/${code}/ignore`, { item: id, ignored });
    await reload();
  }
  if (error) return <div className="card pad"><ErrorBox error={error} /></div>;
  if (!data) return <div className="card pad"><Spinner /> Auditing…</div>;
  const rf = data.read_from;
  return (
    <div className="card pad audit">
      <div className="row between wrap">
        <div>
          <h3 style={{ margin: 0 }}>Audit of {data.name}</h3>
          <div className="small muted">Read-only: nothing is changed until you add a suggestion yourself. Read {fmtDate(data.audited_at)} from catalog
            version {rf.catalog_version}: {rf.built_in_sources} built-in sources, {rf.your_sources} of yours, {rf.country_connectors} country
            connector(s), {rf.global_connectors} global connector(s) with data here, {rf.dataset_entries} dataset entries.</div>
        </div>
        <div className="row">
          <button className="btn sm" disabled={loading} onClick={() => void reload()}>{loading ? <Spinner /> : null}Run again</button>
          <button className="btn ghost sm" onClick={onClose}>Close audit</button>
        </div>
      </div>

      <h4>Current coverage</h4>
      <table className="data">
        <thead><tr><th>Area</th><th>Assessment</th><th className="c">Built-in</th><th className="c">Yours</th><th>Automation</th>
          <th className="c">Unreviewed leads</th><th>Evidence</th></tr></thead>
        <tbody>
          {data.coverage.map((c) => (
            <tr key={c.group}>
              <td><b>{c.title}</b>{c.core && <span className="faint small"> · core</span>}
                {c.notes.map((n) => <div key={n} className="small faint">{n}</div>)}</td>
              <td><Badge kind={c.rating_kind}>{c.rating_title}</Badge></td>
              <td className="c">{c.system || <span className="faint">0</span>}</td>
              <td className="c">{c.user || <span className="faint">0</span>}</td>
              <td className="small">{c.connectors.length ? c.connectors.map((k) => (
                <div key={k.name}><a href={`#/sources/${k.name}`}>{k.title}</a> <span className="faint">({k.scope}, {k.status_title.toLowerCase()})</span></div>))
                : <span className="faint">none</span>}</td>
              <td className="c">{c.potential || <span className="faint">0</span>}</td>
              <td className="small">{c.verified_links > 0 && <Badge kind="good">{c.verified_links} verified link(s)</Badge>}{" "}
                {c.unverified_links > 0 && <Badge>{c.unverified_links} not verified</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Coverage gaps</h4>
      {data.gaps.length === 0 ? <p className="small muted">No gap in the core areas (company, courts, public records, regulatory, procurement, news, domains).</p> : (
        <ul className="small">{data.gaps.map((g) => <li key={g.group}><b>{g.title}:</b> {g.message} {g.notes.join(" ")}</li>)}</ul>
      )}

      <h4>Potential duplicates</h4>
      {data.duplicates.length === 0 ? <p className="small muted">None found.</p> : (
        <ul className="small">{data.duplicates.map((d, i) => (
          <li key={i}><Badge kind={d.strength === "likely" ? "warn" : ""}>{d.reason}</Badge>{" "}
            {d.items.map((x) => `${x.title} (${x.origin === "user" ? "yours" : x.layer})`).join(" ↔ ")}. <span className="muted">{d.suggestion}</span></li>))}</ul>
      )}

      <h4>Recommended additions <span className="faint small">({Math.min(data.recommendations.length, data.recommendations_total)} of {data.recommendations_total})</span></h4>
      <p className="small muted" style={{ marginTop: 0 }}>Only entries from maintained datasets that are not in the catalog yet. None is verified: review the
        site, then use <b>Add…</b> to open the source form with the fields filled in.</p>
      {data.recommendations.length === 0 ? <p className="small muted">Nothing to suggest from the datasets.</p> : (
        <div className="grid2">{data.recommendations.map((r) => (
          <RecCard key={r.id} r={r} onAdd={() => onAdd(r.prefill)} onIgnore={() => void ignore(r.id, true)} />))}</div>
      )}
      {data.ignored.length > 0 && (
        <p className="small"><button className="btn ghost sm" onClick={() => setShowIgnored(!showIgnored)}>
          {showIgnored ? "Hide" : "Show"} {data.ignored.length} ignored suggestion(s)</button></p>
      )}
      {showIgnored && <div className="grid2">{data.ignored.map((r) => (
        <RecCard key={r.id} r={r} ignored onAdd={() => onAdd(r.prefill)} onIgnore={() => void ignore(r.id, false)} />))}</div>}

      <h4>Possible improvements</h4>
      {data.improvements.length === 0 ? <p className="small muted">None.</p> : (
        <ul className="small">{data.improvements.map((x, i) => (
          <li key={i}>{x.href.startsWith("#") ? <a href={x.href}>{x.title}</a> : <><span>{x.title}</span> <Ext href={x.href}>open ↗</Ext></>}</li>))}</ul>
      )}

      <details>
        <summary className="small">Every source considered ({data.sources.length})</summary>
        <table className="data" style={{ marginTop: 6 }}>
          <thead><tr><th>Source</th><th>Layer</th><th>Nature</th><th>Automation</th><th>Evidence</th></tr></thead>
          <tbody>{data.sources.map((x) => {
            const [ak, al] = AUTO_LABEL[x.automation] ?? ["", x.automation];
            return (
              <tr key={x.id}>
                <td><Ext href={x.url}>{x.title}</Ext> <OriginBadge origin={x.origin} />{x.active === false && <Badge>inactive</Badge>}
                  <div className="small faint">{x.kind_title}</div></td>
                <td className="small">{x.layer}</td><td className="small">{x.nature}</td>
                <td><Badge kind={ak}>{al}</Badge></td>
                <td><Badge kind={x.evidence_kind}>{x.evidence}</Badge>{x.review && <div className="small faint">{x.review}</div>}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </details>
      <p className="small faint">{Object.entries(data.legend).map(([k, v]) => `${k}: ${v}`).join(" · ")}. {data.disclaimer}</p>
    </div>
  );
}

// ---------------------------------------------------------------- categories

export function CategoryManager({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const { vocab, reload } = useVocab();
  const [form, setForm] = useState({ title: "", group: "other", description: "" });
  const [editing, setEditing] = useState<string | null>(null);
  const [edit, setEdit] = useState({ title: "", group: "other", description: "" });
  const [err, setErr] = useState<string | null>(null);
  async function run(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
      await reload();
      onChanged();
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    }
  }
  if (!vocab) return <Modal title="Categories" onClose={onClose}><Spinner /></Modal>;
  const mine = vocab.categories.filter((c) => c.origin === "user");
  const builtIn = vocab.categories.filter((c) => c.origin === "system");
  return (
    <Modal title="Categories" onClose={onClose} wide>
      <p className="small muted" style={{ marginTop: 0 }}>Your categories group your sources in Data Sources, the coverage matrix and the filters; in jurisdiction
        views they appear under the group you choose. Built-in categories come with Sextant and cannot be changed.</p>
      <div className="card pad row wrap" style={{ alignItems: "flex-end" }}>
        <label className="field" style={{ flex: 2 }}>New category<input type="text" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
        <label className="field" style={{ flex: 1 }}>Jurisdiction group
          <select value={form.group} onChange={(e) => setForm({ ...form, group: e.target.value })}>
            {Object.entries(vocab.groups).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <button className="btn primary" disabled={form.title.trim().length < 2}
          onClick={() => void run(() => api.post("/api/catalog/categories", form)).then((ok) => ok && setForm({ title: "", group: "other", description: "" }))}>
          ＋ Add category</button>
      </div>
      <ErrorBox error={err} />
      <h3>Yours ({mine.length})</h3>
      {mine.length === 0 ? <p className="small muted">None yet.</p> : (
        <table className="data"><tbody>{mine.map((c) => (
          <tr key={c.id}>
            {editing === c.id ? (
              <>
                <td><input type="text" value={edit.title} onChange={(e) => setEdit({ ...edit, title: e.target.value })} /></td>
                <td><select value={edit.group} onChange={(e) => setEdit({ ...edit, group: e.target.value })}>
                  {Object.entries(vocab.groups).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></td>
                <td className="row"><button className="btn primary sm" onClick={() => void run(() => api.put(`/api/catalog/categories/${c.id}`, edit))
                  .then((ok) => ok && setEditing(null))}>Save</button><button className="btn ghost sm" onClick={() => setEditing(null)}>Cancel</button></td>
              </>
            ) : (
              <>
                <td><b>{c.title}</b> <span className="faint small">{c.user_sources} source(s)</span></td>
                <td className="small">{c.group_title}</td>
                <td className="row"><button className="btn sm" onClick={() => { setEditing(c.id); setEdit({ title: c.title, group: c.group, description: c.description ?? "" }); }}>Edit</button>
                  <button className="btn ghost sm" onClick={() => void run(() => api.del(`/api/catalog/categories/${c.id}`))}>Delete</button></td>
              </>
            )}
          </tr>))}</tbody></table>
      )}
      <details style={{ marginTop: 12 }}>
        <summary className="small">Built-in categories ({builtIn.length})</summary>
        <table className="data"><tbody>{builtIn.map((c) => (
          <tr key={c.id}><td>{c.title}</td><td className="small muted">{c.group_title}</td>
            <td className="small faint">{c.system_sources} connector(s){c.user_sources ? ` · ${c.user_sources} of yours` : ""}</td></tr>))}</tbody></table>
      </details>
    </Modal>
  );
}

// ---------------------------------------------------------------- platforms

const PLATFORM_EMPTY = { name: "", url: "", category: "social", profile_url: "", countries: [] as string[], region: "", notes: "",
  status: "active", public_profile: "yes" };

export function PlatformForm({ initial, onClose, onSaved }: { initial?: UserPlatform | null; onClose: () => void; onSaved: () => void }) {
  const { vocab } = useVocab();
  const [f, setF] = useState({ ...PLATFORM_EMPTY, ...(initial ? { ...initial } : {}) });
  const [dups, setDups] = useState<DuplicateMatch[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function save(confirmed: boolean) {
    setBusy(true);
    setErr(null);
    const body = { name: f.name, url: f.url, category: f.category, profile_url: f.profile_url, countries: f.countries, region: f.region,
      notes: f.notes, status: f.status, public_profile: f.public_profile, allow_duplicate: confirmed };
    try {
      if (initial) await api.put(`/api/catalog/platforms/${initial.id}`, body);
      else await api.post("/api/catalog/platforms", body);
      onSaved();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setDups((e.detail as { duplicates?: DuplicateMatch[] })?.duplicates ?? []);
      else setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (!vocab) return <Modal title="Loading…" onClose={onClose}><Spinner /></Modal>;
  if (dups) {
    return (
      <Modal title="This platform may already exist" onClose={onClose} wide>
        <div className="card"><DuplicateRows items={dups} /></div>
        <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" onClick={() => setDups(null)}>Back to the form</button>
          <button className="btn warn" disabled={busy} onClick={() => void save(true)}>Continue anyway</button>
        </div>
      </Modal>
    );
  }
  const juris = jurisdictionOptions(vocab).filter((o) => o.value.length === 2);
  return (
    <Modal title={initial ? `Edit “${initial.name}”` : "Add platform"} onClose={onClose} wide>
      <div className="notice" style={{ marginBottom: 12 }}>Platforms you add are never checked automatically. With a profile-URL template, Sextant
        shows a link to open for every username an investigation finds.</div>
      <div className="grid2">
        <label className="field">Name *<input type="text" value={f.name} autoFocus onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label className="field">Website *<input type="text" value={f.url} placeholder="https://…" onChange={(e) => setF({ ...f, url: e.target.value })} /></label>
        <label className="field">Category
          <select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
            {Object.entries(vocab.platform_categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="field"><span>Profile URL template <span className="faint">(optional)</span></span>
          <input type="text" value={f.profile_url} placeholder="https://example.com/u/{username}" onChange={(e) => setF({ ...f, profile_url: e.target.value })} />
          <span className="small faint">Profile links to it (pasted as input, found on pages, in search results) are recognised as accounts on
            this platform — also templates whose username is a ?parameter — and it becomes a web-discovery target.</span></label>
        <label className="field"><span>Countries <span className="faint">(empty = global)</span></span>
          <ChipsInput listId="plat-countries" values={f.countries} onChange={(v) => setF({ ...f, countries: v })} options={juris}
            placeholder="e.g. Sweden" /></label>
        <label className="field">Region (free text)<input type="text" value={f.region} onChange={(e) => setF({ ...f, region: e.target.value })} /></label>
        <label className="field">Public profiles
          <select value={f.public_profile} onChange={(e) => setF({ ...f, public_profile: e.target.value })}>
            <option value="yes">public</option><option value="partial">partly public</option><option value="login_wall">login required</option>
            <option value="none">no public profiles</option></select></label>
        <label className="field">Service status
          <select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}>
            {vocab.service_statuses.map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
      </div>
      <label className="field" style={{ marginTop: 10 }}>Notes<textarea rows={2} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} /></label>
      <ErrorBox error={err} />
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={busy || f.name.trim().length < 2 || !f.url.trim()} onClick={() => void save(false)}>
          {initial ? "Save changes" : "Save platform"}</button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- the one catalog reset

interface ResetSummary {
  remove: Record<string, number>; total: number; items: Record<string, string[]>;
  keeps: { connectors: number; official_sources: number; platforms: number; categories: number; note: string };
}

const RESET_ROWS: [string, string][] = [["sources", "Custom sources"], ["links", "Custom reference links"], ["categories", "Custom categories"],
  ["platforms", "Custom platforms"], ["jurisdiction_links", "Source → jurisdiction relationships"],
  ["platform_links", "Source → platform relationships"], ["connector_links", "Source → connector relationships"],
  ["source_preferences", "On/off and auto-run changes to built-in sources"], ["audit_ignores", "Ignored audit suggestions"],
  ["discovery_results", "Saved “Search for connectors” results"], ["legacy_custom", "Unmigrated country-page sources"]];

export function ResetCatalogDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { data, error } = useApi<ResetSummary>("/api/catalog/reset");
  const [step, setStep] = useState<"summary" | "confirm" | "done">("summary");
  const [exported, setExported] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [removed, setRemoved] = useState<Record<string, number> | null>(null);
  async function reset() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ removed: Record<string, number> }>("/api/catalog/reset", { confirm: typed });
      setRemoved(r.removed);
      setStep("done");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const close = step === "done" ? () => { onDone(); onClose(); } : onClose;
  if (error) return <Modal title="Reset catalog to default" onClose={onClose}><ErrorBox error={error} /></Modal>;
  if (!data) return <Modal title="Reset catalog to default" onClose={onClose}><Spinner /></Modal>;
  const rows = RESET_ROWS.filter(([k]) => (data.remove[k] ?? 0) > 0 || ["sources", "links", "categories", "platforms"].includes(k));
  return (
    <Modal title={step === "confirm" ? "RESET TO DEFAULT" : "Reset catalog to default?"} onClose={close}>
      {step === "summary" && (
        <>
          <p style={{ marginTop: 0 }}>Detected custom catalog data:</p>
          <table className="data"><tbody>{rows.map(([k, label]) => (
            <tr key={k}><td>{label}</td><td className="c"><b>{data.remove[k] ?? 0}</b></td></tr>))}</tbody></table>
          {Object.entries(data.items).some(([, v]) => v.length > 0) && (
            <details style={{ marginTop: 8 }}><summary className="small">Show the names</summary>
              {Object.entries(data.items).filter(([, v]) => v.length).map(([k, v]) => (
                <p key={k} className="small"><b>{RESET_ROWS.find(([x]) => x === k)?.[1] ?? k}:</b> {v.join(", ")}</p>))}
            </details>
          )}
          <p className="small muted">{data.keeps.note} Built-in: {data.keeps.connectors} connectors, {data.keeps.official_sources} official sources,
            {" "}{data.keeps.platforms.toLocaleString()} platforms, {data.keeps.categories} categories.</p>
          {data.total > 0 && (
            <div className="notice" style={{ marginTop: 12 }}>
              <b>Back up first.</b> Export your catalog to a file before resetting; Settings → <i>Import catalog</i> restores it
              later. API keys are never written to the file.
              <div style={{ marginTop: 8 }}><ExportCatalogButton onExported={() => setExported(true)} /></div>
            </div>
          )}
          {data.total === 0 && <div className="notice">Nothing to reset: the catalog is already the default.</div>}
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn warn" disabled={data.total === 0} onClick={() => setStep("confirm")}>Continue</button>
          </div>
        </>
      )}
      {step === "confirm" && (
        <>
          <p style={{ marginTop: 0 }}>This will permanently remove all user-created catalog configuration. This includes:</p>
          <ul className="small">
            <li>custom data sources ({data.remove.sources}) and reference links ({data.remove.links})</li>
            <li>custom categories ({data.remove.categories})</li>
            <li>custom platform records ({data.remove.platforms})</li>
            <li>custom jurisdiction, platform and connector relationships ({(data.remove.jurisdiction_links ?? 0) + (data.remove.platform_links ?? 0) + (data.remove.connector_links ?? 0)})</li>
            <li>your on/off and auto-run changes to built-in sources ({data.remove.source_preferences}), ignored audit suggestions and saved connector searches</li>
          </ul>
          <p className="small">System/default sources will be restored. Investigations, API keys and settings are kept. <b>This action cannot be undone</b>
            {exported ? " (you exported a backup you can import again)." : "."}</p>
          {!exported && <div className="notice warn small" style={{ marginBottom: 10 }}>No backup exported in this dialog.
            <span style={{ marginLeft: 8 }}><ExportCatalogButton onExported={() => setExported(true)} small /></span></div>}
          <label className="field">Type RESET to confirm<input type="text" value={typed} autoFocus onChange={(e) => setTyped(e.target.value)} /></label>
          <ErrorBox error={err} />
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn bad" disabled={typed !== "RESET" || busy} onClick={() => void reset()}>{busy ? <Spinner /> : null}Reset everything</button>
          </div>
        </>
      )}
      {step === "done" && removed && (
        <>
          <p style={{ marginTop: 0 }}>The catalog is back to the defaults of this version. Removed:</p>
          <ul className="small">{RESET_ROWS.filter(([k]) => removed[k]).map(([k, label]) => <li key={k}>{label}: {removed[k]}</li>)}</ul>
          <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" onClick={close}>Close</button></div>
        </>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- export / import (backup and restore)

export async function exportCatalog(): Promise<string> {
  const blob = await api.download("/api/catalog/export");
  const name = `sextant-catalog-${new Date().toISOString().slice(0, 10)}.json`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  return name;
}

export function ExportCatalogButton({ onExported, small }: { onExported?: () => void; small?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function go() {
    setBusy(true);
    setErr(null);
    try {
      setSaved(await exportCatalog());
      onExported?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="row wrap" style={{ gap: 8, display: "inline-flex" }}>
      <button className={`btn${small ? " sm" : ""}`} disabled={busy} onClick={() => void go()}>{busy ? <Spinner /> : "⤓"} Export catalog</button>
      {saved && <span className="small" style={{ color: "var(--good)" }}>✓ Saved {saved}</span>}
      {err && <span className="small" style={{ color: "var(--bad)" }}>{err}</span>}
    </span>
  );
}

interface Counts { add: number; update?: number; skip: number }
interface ImportReport {
  categories: Counts; platforms: Counts; sources: Counts; audit_ignores: Counts;
  source_preferences: { apply: number; unchanged: number; skip: number; in_file: number };
  skipped: { type: string; title: string; reason: string }[]; warnings: string[]; dropped_fields: number; changes: number;
  file: { app_version?: string; exported_at?: string; counts?: Record<string, number> }; dry_run: boolean; applied: boolean;
}

export function ImportCatalogDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [doc, setDoc] = useState<unknown>(null);
  const [fileName, setFileName] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [prefs, setPrefs] = useState(true);
  const [preview, setPreview] = useState<ImportReport | null>(null);
  const [result, setResult] = useState<ImportReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function run(d: unknown, dry: boolean, ow: boolean, pr: boolean) {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<ImportReport>("/api/catalog/import", { document: d, dry_run: dry, overwrite: ow, restore_preferences: pr });
      if (dry) setPreview(r);
      else setResult(r);
    } catch (e) {
      setErr((e as Error).message);
      if (dry) setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  async function pick(file: File | undefined) {
    setPreview(null);
    setErr(null);
    if (!file) return;
    setFileName(file.name);
    if (file.size > 30_000_000) {
      setErr("This file is too large to be a catalog export.");
      return;
    }
    let d: unknown;
    try {
      d = JSON.parse(await file.text());
    } catch {
      setErr("This file is not valid JSON.");
      return;
    }
    setDoc(d);
    await run(d, true, overwrite, prefs);
  }
  function option(next: { overwrite?: boolean; prefs?: boolean }) {
    const ow = next.overwrite ?? overwrite;
    const pr = next.prefs ?? prefs;
    setOverwrite(ow);
    setPrefs(pr);
    if (doc) void run(doc, true, ow, pr);
  }
  const close = result ? () => { onDone(); onClose(); } : onClose;
  const rows = (r: ImportReport): [string, Counts | null][] => [["Categories", r.categories], ["Platforms", r.platforms],
    ["Sources and links", r.sources], ["Ignored audit suggestions", r.audit_ignores]];

  if (result) {
    return (
      <Modal title="Catalog imported" onClose={close}>
        <p style={{ marginTop: 0 }}>{result.changes} change(s) written in one step. They show up in Data Sources, the jurisdictions, the coverage
          matrix and the platforms right away.</p>
        <ul className="small">{rows(result).map(([label, c]) => c && (c.add || c.update) ? (
          <li key={label}>{label}: {c.add} added{c.update ? `, ${c.update} replaced` : ""}</li>) : null)}
          {result.source_preferences.apply > 0 && <li>On/off switches restored: {result.source_preferences.apply}</li>}</ul>
        <div className="row" style={{ justifyContent: "flex-end" }}><button className="btn primary" onClick={close}>Close</button></div>
      </Modal>
    );
  }
  return (
    <Modal title="Import catalog" onClose={onClose} wide>
      <p className="small muted" style={{ marginTop: 0 }}>Choose a file made with <b>Export catalog</b>. You see what it would change before anything is
        written. Records already in your catalog are skipped; every record is checked like a new entry in the form.</p>
      <div className="row wrap" style={{ gap: 18 }}>
        <input type="file" accept=".json,application/json" aria-label="Catalog file" onChange={(e) => void pick(e.target.files?.[0])} />
        <label className="check small"><input type="checkbox" checked={overwrite} onChange={(e) => option({ overwrite: e.target.checked })} />
          Replace my records that have the same id</label>
        <label className="check small"><input type="checkbox" checked={prefs} onChange={(e) => option({ prefs: e.target.checked })} />
          Also restore on/off switches of built-in sources</label>
      </div>
      {busy && <p><Spinner /> Checking {fileName}…</p>}
      <ErrorBox error={err} />
      {preview && !busy && (
        <>
          <p className="small muted">{fileName}: exported {fmtDate(preview.file.exported_at)} by Sextant {preview.file.app_version ?? "?"}.</p>
          <table className="data">
            <thead><tr><th>In the file</th><th className="c">Add</th><th className="c">Replace</th><th className="c">Skip</th></tr></thead>
            <tbody>
              {rows(preview).map(([label, c]) => c && (
                <tr key={label}><td>{label}</td><td className="c"><b>{c.add}</b></td><td className="c">{c.update ?? "—"}</td><td className="c">{c.skip}</td></tr>))}
              <tr><td>On/off switches of built-in sources ({preview.source_preferences.in_file} in the file)</td>
                <td className="c"><b>{preview.source_preferences.apply}</b></td><td className="c">—</td>
                <td className="c">{prefs ? preview.source_preferences.skip + preview.source_preferences.unchanged : preview.source_preferences.in_file}</td></tr>
            </tbody>
          </table>
          {preview.skipped.length > 0 && (
            <details style={{ marginTop: 8 }}><summary className="small">Why records are skipped ({preview.skipped.length})</summary>
              <ul className="small">{preview.skipped.map((x, i) => <li key={i}><b>{x.title}</b> ({x.type}): {x.reason}</li>)}</ul></details>
          )}
          {(preview.warnings.length > 0 || preview.dropped_fields > 0) && (
            <details style={{ marginTop: 8 }} open={preview.warnings.length > 0 && preview.warnings.length <= 5}>
              <summary className="small">Adjustments ({preview.warnings.length + (preview.dropped_fields ? 1 : 0)})</summary>
              <ul className="small">{preview.warnings.map((w, i) => <li key={i}>{w}</li>)}
                {preview.dropped_fields > 0 && <li>{preview.dropped_fields} unknown field(s) in the file were ignored.</li>}</ul></details>
          )}
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 14 }}>
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || preview.changes === 0} onClick={() => void run(doc, false, overwrite, prefs)}>
              {preview.changes === 0 ? "Nothing new to import" : `Import ${preview.changes} change(s)`}</button>
          </div>
        </>
      )}
    </Modal>
  );
}
