import { useEffect, useState } from "react";
import { api, inv } from "../api";
import type { EntityDetail } from "../types";
import { Badge, Confidence, ErrorBox, Ext, MatchBadge, SourceChip, Spinner, TypeDot, Value, fmtDate, pct, useOntology } from "../ui";

interface RawEvidence {
  sha256: string; url: string; method: string; status: number; content_type: string; size: number; retrieved_at: string;
  connector: string; text: string | null; binary_size: number | null;
}

interface Props {
  invId: string;
  entityId: string;
  version: number;
  onSelect: (id: string) => void;
  onChanged: () => void;
  onTransforms: (id: string) => void;
  onClose: () => void;
}

export default function Inspector({ invId, entityId, version, onSelect, onChanged, onTransforms, onClose }: Props) {
  const [d, setD] = useState<EntityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [openRel, setOpenRel] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [raw, setRaw] = useState<RawEvidence | null>(null);
  const o = useOntology();

  async function showRaw(digest: string) {
    try {
      setRaw(await api.get<RawEvidence>(inv(invId, `/raw/${digest}`)));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    let live = true;
    api.get<EntityDetail>(inv(invId, `/entities/${entityId}`))
      .then((x) => live && (setD(x), setError(null)))
      .catch((e) => live && setError((e as Error).message));
    return () => {
      live = false;
    };
  }, [invId, entityId, version]);

  async function decide(action: "confirm" | "separate" | "ignore" | "reset") {
    setBusy(true);
    try {
      await api.post(inv(invId, `/entities/${entityId}/decision`), { action, note });
      setNote("");
      onChanged();
      setD(await api.get<EntityDetail>(inv(invId, `/entities/${entityId}`)));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function investigate() {
    setBusy(true);
    try {
      await api.post(inv(invId, `/entities/${entityId}/transforms`), { transforms: "auto", expand_levels: 1 });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function unmerge(key: string) {
    await api.post(inv(invId, `/entities/${entityId}/unmerge`), { merged_key: key });
    onChanged();
  }

  async function rejectRel(id: string) {
    await api.post(inv(invId, `/relationships/${id}/reject`));
    onChanged();
  }

  if (error && !d) return <div className="inspector"><div className="sect"><ErrorBox error={error} /></div></div>;
  if (!d) return <div className="inspector"><div className="sect"><Spinner /></div></div>;
  const e = d.entity;
  const a = d.assessment;
  const thumb = d.properties.find((p) => p.name === "avatar_thumb" || p.name === "thumb")?.values[0]?.value as string | undefined;
  const props = d.properties.filter((p) => !["avatar_thumb", "thumb", "avatar_fp", "dhash", "phash", "dhash_flip", "phash_flip"].includes(p.name));
  return (
    <div className="inspector">
      <div className="sect">
        <div className="row between">
          <span className="row small muted"><TypeDot type={e.type} />{e.type_title} · depth {e.depth}</span>
          <button className="btn ghost sm" onClick={onClose} title="Close">✕</button>
        </div>
        <div className="row" style={{ marginTop: 8, alignItems: "flex-start" }}>
          {thumb && <img src={thumb} alt="" className="thumb" />}
          <div style={{ minWidth: 0 }}>
            <div className="title">{e.label}</div>
            {e.value !== e.label && <div className="small faint mono wrapword">{e.value}</div>}
          </div>
        </div>
        <div className="row wrap" style={{ marginTop: 10 }}>
          <MatchBadge e={e} />
          {e.status !== "candidate" && <Badge kind={e.status === "confirmed" ? "good" : e.status === "seed" ? "info" : ""}>{e.status}</Badge>}
          {e.tier && <Badge>{e.tier} value</Badge>}
        </div>
        {e.resolvable && e.status !== "seed" && <div style={{ marginTop: 10 }}><Confidence value={a.confidence} /></div>}
        <div className="row wrap" style={{ marginTop: 12 }}>
          {d.links.map((l) => <Ext key={l.url} href={l.url}><span className="btn sm">↗ Open {l.label}</span></Ext>)}
          <button className="btn sm primary" disabled={busy} onClick={() => void investigate()}>Investigate entity</button>
          <button className="btn sm" onClick={() => onTransforms(entityId)}>Transforms…</button>
        </div>
      </div>

      {e.status !== "seed" && (
        <div className="sect">
          <h3>Decision</h3>
          <div className="row wrap">
            <button className="btn sm good" disabled={busy || e.status === "confirmed"} onClick={() => void decide("confirm")}
              title="This pertains to the target. Becomes an anchor for scoring everything else.">CONFIRM</button>
            <button className="btn sm warn" disabled={busy || e.status === "separated"} onClick={() => void decide("separate")}
              title="A different person/entity (e.g. a namesake). Kept, but excluded from the target.">SEPARATE</button>
            <button className="btn sm" disabled={busy || e.status === "ignored"} onClick={() => void decide("ignore")}
              title="Irrelevant noise.">IGNORE</button>
            {e.status !== "candidate" && <button className="btn sm ghost" disabled={busy} onClick={() => void decide("reset")}>Reset</button>}
          </div>
          <input type="text" style={{ marginTop: 8 }} placeholder="Optional note for the audit log" value={note}
            onChange={(ev) => setNote(ev.target.value)} />
        </div>
      )}

      {(a.support.length > 0 || a.contradictions.length > 0 || a.info.length > 0) && (
        <div className="sect">
          <h3>{e.resolvable ? `Identity assessment · ${a.label || pct(a.confidence)}` : "Relevance"}</h3>
          {a.support.length > 0 && <div className="small muted" style={{ marginBottom: 2 }}>Evidence</div>}
          {a.support.map((r, i) => (
            <div key={`s${i}`} className="reason support"><span className="w">+{r.weight.toFixed(1)}</span><span>{r.description}</span></div>
          ))}
          {a.contradictions.length > 0 && <div className="small muted" style={{ margin: "6px 0 2px" }}>Contradictions</div>}
          {a.contradictions.map((r, i) => (
            <div key={`c${i}`} className="reason contradiction"><span className="w">{r.weight.toFixed(1)}</span><span>{r.description}</span></div>
          ))}
          {a.info.map((r, i) => (
            <div key={`i${i}`} className="reason info"><span className="w">·</span><span>{r.description}</span></div>
          ))}
          {e.resolvable && <p className="small faint" style={{ marginBottom: 0 }}>{a.note}</p>}
        </div>
      )}

      <div className="sect">
        <h3>Properties</h3>
        {props.length === 0 && <div className="small muted">No properties yet.</div>}
        {props.map((p) => (
          <div key={p.name} className="prop">
            <div className="k">{p.title}{p.local_only && <span title="Never sent to any source"> · 🔒 local only</span>}</div>
            {p.values.map((v, i) => (
              <div key={i} className="v">
                <Value v={v.value} />
                <div>{v.observations.slice(0, 4).map((ob) => <SourceChip key={ob.id} s={ob.source} />)}
                  {v.observations.length > 4 && <span className="small faint"> +{v.observations.length - 4}</span>}</div>
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className="sect">
        <h3>Relationships ({d.relationships.length})</h3>
        {d.relationships.slice(0, 120).map((r) => (
          <div key={r.id} className="relrow">
            <div className="row" style={{ gap: 6 }}>
              <span className="muted small" style={{ minWidth: 92 }}>{r.title}</span>
              <TypeDot type={r.other.type} />
              <a href="#" onClick={(ev) => { ev.preventDefault(); onSelect(r.other.id); }} className="wrapword" style={{ flex: 1 }}>
                {r.other.label}
              </a>
              <span className="small faint">{pct(r.confidence)}</span>
              <button className="btn ghost sm" onClick={() => setOpenRel(openRel === r.id ? null : r.id)}>{openRel === r.id ? "▾" : "▸"}</button>
            </div>
            {r.status === "rejected" && <div className="small" style={{ color: "var(--bad)" }}>Rejected by the investigator</div>}
            {openRel === r.id && (
              <div className="col small" style={{ marginTop: 4, paddingLeft: 98 }}>
                {r.evidence.map((ev, i) => (
                  <div key={i}>
                    {ev.description} <span className="faint">({pct(ev.confidence)}, {fmtDate(ev.observed_at)})</span> <SourceChip s={ev.source} />
                  </div>
                ))}
                {Object.keys(r.props).length > 0 && <div><Value v={r.props} /></div>}
                {r.status !== "rejected" && <div><button className="btn sm bad" onClick={() => void rejectRel(r.id)}>Unlink (reject this relationship)</button></div>}
              </div>
            )}
          </div>
        ))}
      </div>

      {d.merged.length > 0 && (
        <div className="sect">
          <h3>Merged into this entity</h3>
          {d.merged.map((m) => (
            <div key={m.key} className="row between small">
              <span>{m.label ?? m.key} <span className="faint">{fmtDate(m.at)}</span></span>
              <button className="btn sm" onClick={() => void unmerge(m.key)}>Separate</button>
            </div>
          ))}
        </div>
      )}

      {d.checks.length > 0 && (
        <div className="sect">
          <h3>Site checks</h3>
          {d.checks.map((c, i) => (
            <div key={i} className="small">{c.platform_title || c.platform}: <b>{c.status}</b> {c.detail && <span className="faint">— {c.detail}</span>}</div>
          ))}
        </div>
      )}

      <div className="sect">
        <h3>Sources ({d.sources.length})</h3>
        {d.sources.map((s) => (
          <div key={s.id} className="small" style={{ padding: "4px 0", borderBottom: "1px dashed #1e2835" }}>
            <b>{s.connector}</b> · {s.title} <span className="faint">({s.reliability})</span>
            <div>{s.url ? <Ext href={s.url} /> : <span className="faint">local analysis — nothing sent</span>}</div>
            <div className="faint">retrieved {fmtDate(s.retrieved_at)}{s.request ? ` · sent: ${s.request}` : ""}{s.license ? ` · ${s.license}` : ""}</div>
            {(s.raw_refs ?? []).length > 0 && (
              <div className="faint">raw evidence:{" "}
                {(s.raw_refs ?? []).map((r) => (
                  <button key={r} className="btn ghost sm mono" title="Show the stored raw response (sha256)" onClick={() => void showRaw(r)}>
                    sha256:{r.slice(0, 12)}…
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
        {raw && (
          <div className="card pad" style={{ marginTop: 8 }}>
            <div className="row between small"><b>Raw response</b><button className="btn ghost sm" onClick={() => setRaw(null)}>✕</button></div>
            <div className="small faint wrapword">sha256 {raw.sha256} · {raw.method} {raw.url} · HTTP {raw.status} · {raw.content_type || "?"} ·
              {" "}{raw.size.toLocaleString()} bytes · retrieved {fmtDate(raw.retrieved_at)} · connector {raw.connector}</div>
            {raw.text !== null ? <pre className="mono small" style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap" }}>{raw.text.slice(0, 200000)}</pre>
              : <div className="small">Binary content ({raw.binary_size} bytes) — not displayed.</div>}
          </div>
        )}
        <div className="small faint" style={{ marginTop: 6 }}>First seen {fmtDate(e.first_seen)} · type {o.title(e.type)}</div>
      </div>
    </div>
  );
}
