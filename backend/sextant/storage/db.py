"""Investigation database (SQLite, WAL, encrypted identifying fields).

Structural columns (types, statuses, scores, timestamps, connector names, foreign keys) are stored
in the clear so the engine can query and count. Everything identifying (entity values and labels,
property values, URLs, evidence text, seeds, credentials) sits in ``*_enc`` columns encrypted with
Fernet. Ids and dedupe keys are HMAC blind indexes, salted per investigation, so the same email
cannot be linked across investigations by reading the file.

Persistence is incremental: the engine flushes only what changed since the last flush
(``Graph.dirty``), in one transaction, every couple of seconds while an investigation runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..model import (
    Check, Dirty, Entity, Graph, Observation, Reason, RelEvidence, Relationship, Source, Task, TransformRun,
    entity_from_dict, utcnow,
)
from .crypto import KEY_FILE, SALT_FILE, Vault

DB_FILE = "sextant.sqlite3"
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS investigations(
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'person', depth TEXT NOT NULL DEFAULT '2', data_enc TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS entities(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, key_hash TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, depth INTEGER NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, id));
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(investigation_id, type);

CREATE TABLE IF NOT EXISTS entity_properties(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL, name TEXT NOT NULL, value_hash TEXT NOT NULL, confidence REAL NOT NULL,
    observations INTEGER NOT NULL, value_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, entity_id, name, value_hash));

CREATE TABLE IF NOT EXISTS sources(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, connector TEXT NOT NULL, kind TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    reliability TEXT NOT NULL, data_enc TEXT NOT NULL, PRIMARY KEY(investigation_id, id));

CREATE TABLE IF NOT EXISTS observations(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, entity_id TEXT NOT NULL, prop TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT,
    observed_at TEXT NOT NULL, confidence REAL NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, id));
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(investigation_id, entity_id);

CREATE TABLE IF NOT EXISTS relationships(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, key_hash TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL, type TEXT NOT NULL,
    confidence REAL NOT NULL, status TEXT NOT NULL, first_seen TEXT NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, id));

CREATE TABLE IF NOT EXISTS evidence(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    weight REAL NOT NULL, source_id TEXT, run_id TEXT, observed_at TEXT, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, subject_type, subject_id, seq));

CREATE TABLE IF NOT EXISTS confidence_scores(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL, model TEXT NOT NULL, score REAL NOT NULL, tier TEXT NOT NULL, value_score REAL NOT NULL,
    label TEXT NOT NULL, computed_at TEXT NOT NULL, PRIMARY KEY(investigation_id, entity_id, model));

CREATE TABLE IF NOT EXISTS transform_runs(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, task_id TEXT, transform TEXT NOT NULL, connector TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, requests INTEGER NOT NULL, new_entities INTEGER NOT NULL,
    depth INTEGER NOT NULL, trigger TEXT NOT NULL, data_enc TEXT NOT NULL, PRIMARY KEY(investigation_id, id));

CREATE TABLE IF NOT EXISTS search_tasks(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, transform TEXT NOT NULL, status TEXT NOT NULL, priority REAL NOT NULL, depth INTEGER NOT NULL,
    trigger TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, id));

CREATE TABLE IF NOT EXISTS checks(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL, connector TEXT NOT NULL, status TEXT NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, key_hash));

CREATE TABLE IF NOT EXISTS user_decisions(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, seq));

CREATE TABLE IF NOT EXISTS http_cache(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL, expires_at REAL NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, key_hash));

CREATE TABLE IF NOT EXISTS connectors(
    name TEXT PRIMARY KEY, enabled INTEGER, auto_run INTEGER, updated_at TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_credentials(
    key TEXT PRIMARY KEY, value_enc TEXT NOT NULL, updated_at TEXT NOT NULL, verified_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, data_enc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jurisdictions(
    code TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL, custom INTEGER NOT NULL DEFAULT 0, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_evidence(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    digest TEXT NOT NULL, retrieved_at TEXT NOT NULL, size INTEGER NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, digest));
CREATE TABLE IF NOT EXISTS source_health(name TEXT PRIMARY KEY, updated_at TEXT NOT NULL, data_json TEXT NOT NULL);
-- The assistant's memory per investigation: actions you approved, chat turns and every tool call the AI made
-- (append-only, encrypted, deleted with the investigation).
CREATE TABLE IF NOT EXISTS assistant_log(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '', data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, seq));
-- Usage and cost of every billable call (AI requests, priced APIs), one row per request or run. No personal data:
-- provider, model, token counts, prices. investigation_id is NULL for calls outside an investigation (a test, source
-- discovery from the catalog).
CREATE TABLE IF NOT EXISTS usage_records(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id TEXT REFERENCES investigations(id) ON DELETE CASCADE,
    at TEXT NOT NULL, provider TEXT NOT NULL, service TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 1,
    estimated_cost REAL, actual_cost REAL, currency TEXT NOT NULL DEFAULT 'USD', cost_status TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_usage_inv ON usage_records(investigation_id);
-- Structured notes the investigator and the assistant keep per investigation (encrypted, deleted with it).
CREATE TABLE IF NOT EXISTS investigation_notes(
    investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
    id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data_enc TEXT NOT NULL,
    PRIMARY KEY(investigation_id, id));
CREATE TABLE IF NOT EXISTS source_capabilities(
    id TEXT PRIMARY KEY, jurisdiction TEXT NOT NULL, kind TEXT NOT NULL, access TEXT NOT NULL,
    name_search INTEGER NOT NULL, connector TEXT, custom INTEGER NOT NULL DEFAULT 0, data_json TEXT NOT NULL);

-- The user layer of the catalog (registry/catalog.py). These tables hold ONLY records the investigator created;
-- system sources, categories and platforms live in the shipped code and data files and are never copied here, so
-- deleting every row of these tables (the catalog reset) restores the defaults of the installed version.
CREATE TABLE IF NOT EXISTS catalog_categories(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, grp TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_platforms(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_sources(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, url_key TEXT NOT NULL, category TEXT NOT NULL,
    source_type TEXT NOT NULL, access TEXT NOT NULL, connector TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    review TEXT NOT NULL DEFAULT 'not_verified', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_catalog_sources_url ON catalog_sources(url_key);
CREATE TABLE IF NOT EXISTS catalog_source_jurisdictions(
    source_id TEXT NOT NULL REFERENCES catalog_sources(id) ON DELETE CASCADE, code TEXT NOT NULL,
    PRIMARY KEY(source_id, code));
CREATE INDEX IF NOT EXISTS idx_catalog_sj_code ON catalog_source_jurisdictions(code);
CREATE TABLE IF NOT EXISTS catalog_source_platforms(
    source_id TEXT NOT NULL REFERENCES catalog_sources(id) ON DELETE CASCADE, platform_id TEXT NOT NULL,
    PRIMARY KEY(source_id, platform_id));
CREATE TABLE IF NOT EXISTS catalog_audit_ignores(code TEXT NOT NULL, item TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY(code, item));
"""

CATALOG_SOURCE_DATA = ("description", "kind", "inputs", "official", "name_search", "person_data", "notes", "link_check",
                       "provider_class", "pricing", "auth", "terms_url", "data_types", "access_models", "api_available",
                       "web_app", "documentation_url", "coverage", "limitations", "last_verified")


@dataclass
class GraphWrite:
    """Rows for one incremental flush, encoded from the graph by ``Database.prepare_graph``."""
    ops: list[tuple[str, list[tuple]]]        # (sql, rows) for executemany, in order
    rows: int
    dirty: Dirty                              # what was detached from the graph — given back if the write fails


class Database:
    def __init__(self, data_dir: Path, vault: Vault) -> None:
        self.data_dir = data_dir
        self.path = data_dir / DB_FILE
        self.vault = vault
        self._lock = threading.RLock()
        self._conn = self._connect()

    # ------------------------------------------------------------------ lifecycle
    def _connect(self) -> sqlite3.Connection:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:  # pragma: no cover - some shared filesystems
            pass
        fresh = not self.path.exists()
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if fresh:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.executescript(SCHEMA)
        # an existing database keeps its old version number until _migrate has upgraded it
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                     (SCHEMA_VERSION if fresh else "2",))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('vault_check', ?)", (self.vault.encrypt("ok"),))
        self.vault.decrypt(conn.execute("SELECT value FROM meta WHERE key='vault_check'").fetchone()[0])  # wrong key → VaultError
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Schema 2 → 3: sources added on a country page (``source_capabilities`` rows with ``custom = 1``) move to the
        catalog's user layer with the same ids, in one transaction. Their old rows are removed so there is exactly one
        copy of each source."""
        version = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] or 0)
        if version >= 3:
            return
        from ..registry.catalog import category_for_kind, url_key
        conn.execute("BEGIN")
        try:
            rows = conn.execute("SELECT id, jurisdiction, kind, access, name_search, connector, data_json "
                                "FROM source_capabilities WHERE custom=1").fetchall()
            for sid, cc, kind, access, name_search, connector, data in rows:
                d = json.loads(data or "{}")
                at = d.get("added_at") or utcnow()
                url = d.get("url") or ""
                conn.execute("INSERT OR IGNORE INTO catalog_sources VALUES(?,?,?,?,?,?,?,?,1,'not_verified',?,?,?)",
                             (sid, d.get("title") or sid, url, url_key(url), category_for_kind(kind), "reference",
                              access or "unknown", connector or None, at, at,
                              json.dumps({"kind": kind or "", "notes": d.get("notes") or "",
                                          "name_search": bool(name_search), "inputs": [], "official": False})))
                conn.execute("INSERT OR IGNORE INTO catalog_source_jurisdictions VALUES(?,?)", (sid, cc))
            conn.execute("DELETE FROM source_capabilities WHERE custom=1")
            conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.fetchall()

    # ------------------------------------------------------------------ helpers
    def enc(self, obj: Any) -> str:
        return self.vault.encrypt(obj)

    def dec(self, token: str | None) -> Any:
        return self.vault.decrypt(token)

    def blind(self, inv_id: str, value: str, n: int = 24) -> str:
        return self.vault.blind(f"{inv_id}|{value}", n)

    def id_fn(self, inv_id: str):
        return lambda key: self.vault.blind(f"{inv_id}|{key}", 16)

    # ------------------------------------------------------------------ investigations
    def create_investigation(self, data: dict, target_type: str, depth: str) -> str:
        inv_id = uuid.uuid4().hex[:16]
        now = utcnow()
        with self.tx() as c:
            c.execute("INSERT INTO investigations(id, created_at, updated_at, status, target_type, depth, data_enc) "
                      "VALUES(?,?,?,?,?,?,?)", (inv_id, now, now, "created", target_type, str(depth), self.enc(data)))
        return inv_id

    def update_investigation(self, inv_id: str, *, status: str | None = None, data: dict | None = None,
                             data_enc: str | None = None) -> None:
        """``data_enc`` is ``data`` already encrypted — for callers that must snapshot live state before going to a thread."""
        sets, args = ["updated_at=?"], [utcnow()]
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if data is not None:
            data_enc = self.enc(data)
        if data_enc is not None:
            sets.append("data_enc=?")
            args.append(data_enc)
        with self.tx() as c:
            c.execute(f"UPDATE investigations SET {', '.join(sets)} WHERE id=?", (*args, inv_id))

    def get_investigation(self, inv_id: str) -> dict | None:
        rows = self._q("SELECT id, created_at, updated_at, status, target_type, depth, data_enc FROM investigations WHERE id=?",
                       (inv_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "created_at": r[1], "updated_at": r[2], "status": r[3], "target_type": r[4],
                "depth": r[5], "data": self.dec(r[6])}

    def list_investigations(self) -> list[dict]:
        out = []
        for r in self._q("SELECT id, created_at, updated_at, status, target_type, depth, data_enc FROM investigations "
                         "ORDER BY created_at DESC"):
            data = self.dec(r[6]) or {}
            counts = self._q("SELECT COUNT(*) FROM entities WHERE investigation_id=?", (r[0],))[0][0]
            out.append({"id": r[0], "created_at": r[1], "updated_at": r[2], "status": r[3], "target_type": r[4],
                        "depth": r[5], "title": data.get("title"), "stats": data.get("stats") or {"entities": counts},
                        "seeds": data.get("seed_summary")})
        return out

    def delete_investigation(self, inv_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM investigations WHERE id=?", (inv_id,))
        with self._lock:
            self._conn.execute("VACUUM")

    # ------------------------------------------------------------------ graph persistence
    def flush_graph(self, inv_id: str, g: Graph) -> int:
        """Writes everything ``g.dirty`` marks as changed; clears the dirty set. Returns rows written."""
        pending = self.prepare_graph(inv_id, g)
        if pending is None:
            return 0
        try:
            return self.write_graph(pending)
        except BaseException:
            g.dirty.absorb(pending.dirty)
            raise

    def prepare_graph(self, inv_id: str, g: Graph) -> GraphWrite | None:
        """Encodes every row ``g.dirty`` marks as changed and hands the graph a fresh dirty set.

        This is the only step that reads the graph. Run it on the thread that owns the graph (the event loop), then
        give the result to :meth:`write_graph` in a worker thread: transforms keep merging results while the
        database is written, and reading the graph from the worker raced them ("Set changed size during iteration")
        and dropped changes made mid-write when the dirty set was cleared afterwards.
        """
        d = g.dirty
        if not d:
            return None
        g.dirty = Dirty()
        idf = self.id_fn(inv_id)
        now = utcnow()
        ops: list[tuple[str, list[tuple]]] = []
        n = 0

        def op(sql: str, rows: list[tuple]) -> None:
            if rows:
                ops.append((sql, rows))

        gone = [idf(key) for key in d.deleted_entities]
        for table, col in (("entities", "id"), ("entity_properties", "entity_id"), ("confidence_scores", "entity_id")):
            op(f"DELETE FROM {table} WHERE investigation_id=? AND {col}=?", [(inv_id, eid) for eid in gone])
        op("DELETE FROM evidence WHERE investigation_id=? AND subject_type='entity' AND subject_id=?",
           [(inv_id, eid) for eid in gone])
        gone = [idf("rel|" + key) for key in d.deleted_relationships]
        op("DELETE FROM relationships WHERE investigation_id=? AND id=?", [(inv_id, rid) for rid in gone])
        op("DELETE FROM evidence WHERE investigation_id=? AND subject_type='relationship' AND subject_id=?",
           [(inv_id, rid) for rid in gone])

        src_rows = []
        for sid in d.sources:
            s = g.sources.get(sid)
            if s:
                src_rows.append((inv_id, s.id, s.connector, s.kind, s.retrieved_at, s.reliability,
                                 self.enc({"url": s.url, "title": s.title, "transform": s.transform, "request": s.request,
                                           "license": s.license, "last_retrieved": s.last_retrieved})))
        op("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?)", src_rows)
        n += len(src_rows)

        ent_rows, prop_rows, score_rows, ev_rows = [], [], [], []
        for key in d.entities:
            e = g.entities.get(key)
            if e is None:
                continue
            ent_rows.append((inv_id, e.id, self.blind(inv_id, key), e.type, e.status, e.depth, e.first_seen,
                             e.last_seen or None, self.enc({"value": e.value, "label": e.label,
                                                            "discovered_by": e.discovered_by})))
            prop_rows += [(inv_id, e.id, name, self.blind(inv_id, pv["k"], 16), pv["conf"], len(pv["obs"]),
                           self.enc({"v": pv["v"], "obs": pv["obs"]}))
                          for name, vals in e.props.items() for pv in vals]
            score_rows.append((inv_id, e.id, "identity-v1", e.confidence, e.tier or "", e.value_score, e.match_label or "",
                               now))
            ev_rows += [(inv_id, "entity", e.id, i, r.kind, r.weight, (r.sources or [None])[0], None, now,
                         self.enc(r.to_dict())) for i, r in enumerate(e.reasons)]
        ids = [(inv_id, row[1]) for row in ent_rows]
        op("INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?,?,?,?,?)", ent_rows)
        op("DELETE FROM entity_properties WHERE investigation_id=? AND entity_id=?", ids)
        op("INSERT OR REPLACE INTO entity_properties VALUES(?,?,?,?,?,?,?)", prop_rows)
        op("INSERT OR REPLACE INTO confidence_scores VALUES(?,?,?,?,?,?,?,?)", score_rows)
        op("DELETE FROM evidence WHERE investigation_id=? AND subject_type='entity' AND subject_id=?", ids)
        op("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)", ev_rows)
        n += len(ent_rows)

        obs_rows = []
        for oid in d.observations:
            o = g.observations.get(oid)
            if o is None:
                continue
            ent = g.get(o.entity)
            obs_rows.append((inv_id, o.id, ent.id if ent else idf(o.entity), o.prop, o.source, o.run or None,
                             o.observed_at, o.confidence, self.enc({"entity": o.entity, "value": o.value, "evidence": o.evidence})))
        op("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?)", obs_rows)
        n += len(obs_rows)

        rel_rows, ev_rows = [], []
        for rkey in d.relationships:
            r = g.relationships.get(rkey)
            if r is None:
                continue
            s, t = g.entities.get(r.source), g.entities.get(r.target)
            if s is None or t is None:
                continue
            rel_rows.append((inv_id, r.id, self.blind(inv_id, rkey), s.id, t.id, r.type, r.confidence, r.status, r.first_seen,
                             self.enc({"source": r.source, "target": r.target, "props": r.props})))
            ev_rows += [(inv_id, "relationship", r.id, i, "provenance", ev.confidence, ev.source, ev.run or None, ev.observed_at,
                         self.enc({"description": ev.description, "origin": ev.origin})) for i, ev in enumerate(r.evidence)]
        op("INSERT OR REPLACE INTO relationships VALUES(?,?,?,?,?,?,?,?,?,?)", rel_rows)
        op("DELETE FROM evidence WHERE investigation_id=? AND subject_type='relationship' AND subject_id=?",
           [(inv_id, row[1]) for row in rel_rows])
        op("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)", ev_rows)
        n += len(rel_rows)

        chk_rows = []
        for ckey in d.checks:
            ch = g.checks.get(ckey)
            if ch:
                chk_rows.append((inv_id, self.blind(inv_id, ckey), ch.connector, ch.status, self.enc(ch.to_dict())))
        op("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?)", chk_rows)
        n += len(chk_rows)
        return GraphWrite(ops, n, d)

    def write_graph(self, pending: GraphWrite) -> int:
        """Runs what :meth:`prepare_graph` encoded, in one transaction. Never touches the graph — safe in a worker thread."""
        with self.tx() as c:
            for sql, rows in pending.ops:
                c.executemany(sql, rows)
        return pending.rows

    def load_graph(self, inv_id: str, target_key: str | None, aliases: dict, merge_log: list) -> Graph:
        g = Graph(self.id_fn(inv_id))
        sources = []
        for r in self._q("SELECT id, connector, kind, retrieved_at, reliability, data_enc FROM sources WHERE investigation_id=?",
                         (inv_id,)):
            d = self.dec(r[5])
            sources.append(Source(id=r[0], connector=r[1], kind=r[2], retrieved_at=r[3], reliability=r[4], url=d.get("url"),
                                  title=d.get("title", ""), transform=d.get("transform", ""), request=d.get("request", ""),
                                  license=d.get("license"), last_retrieved=d.get("last_retrieved", "")))
        reasons: dict[str, list[Reason]] = {}
        for r in self._q("SELECT subject_id, data_enc FROM evidence WHERE investigation_id=? AND subject_type='entity' "
                         "ORDER BY subject_id, seq", (inv_id,)):
            reasons.setdefault(r[0], []).append(Reason.from_dict(self.dec(r[1])))
        scores = {r[0]: r[1:] for r in self._q(
            "SELECT entity_id, score, tier, value_score, label FROM confidence_scores WHERE investigation_id=?", (inv_id,))}
        entities = []
        for r in self._q("SELECT id, type, status, depth, first_seen, last_seen, data_enc FROM entities WHERE investigation_id=?",
                         (inv_id,)):
            d = self.dec(r[6])
            sc = scores.get(r[0], (0.0, "", 0.0, ""))
            e = Entity(type=r[1], value=d["value"], label=d.get("label", ""), id=r[0], status=r[2], depth=r[3],
                       first_seen=r[4], last_seen=r[5] or "", discovered_by=d.get("discovered_by", ""),
                       confidence=sc[0], tier=sc[1], value_score=sc[2], match_label=sc[3], reasons=reasons.get(r[0], []))
            entities.append(e)
        observations = []
        for r in self._q("SELECT id, prop, source_id, run_id, observed_at, confidence, data_enc FROM observations "
                         "WHERE investigation_id=?", (inv_id,)):
            d = self.dec(r[6])
            observations.append(Observation(id=r[0], entity=d["entity"], prop=r[1], value=d.get("value"), source=r[2],
                                            run=r[3] or "", observed_at=r[4], confidence=r[5], evidence=d.get("evidence", "")))
        rel_ev: dict[str, list[RelEvidence]] = {}
        for r in self._q("SELECT subject_id, weight, source_id, run_id, observed_at, data_enc FROM evidence "
                         "WHERE investigation_id=? AND subject_type='relationship' ORDER BY subject_id, seq", (inv_id,)):
            d = self.dec(r[5])
            rel_ev.setdefault(r[0], []).append(RelEvidence(source=r[2] or "", description=d.get("description", ""),
                                                           run=r[3] or "", confidence=r[1], observed_at=r[4] or "",
                                                           origin=d.get("origin", "")))
        rels = []
        for r in self._q("SELECT id, type, confidence, status, first_seen, data_enc FROM relationships WHERE investigation_id=?",
                         (inv_id,)):
            d = self.dec(r[5])
            rels.append(Relationship(source=d["source"], target=d["target"], type=r[1], confidence=r[2],
                                     evidence=rel_ev.get(r[0], []), status=r[3], props=d.get("props") or {}, id=r[0],
                                     first_seen=r[4]))
        checks = [Check.from_dict(self.dec(r[0])) for r in
                  self._q("SELECT data_enc FROM checks WHERE investigation_id=?", (inv_id,))]
        g.restore(entities, rels, observations, sources, checks, aliases, merge_log, target_key)
        return g

    # ------------------------------------------------------------------ runs & tasks
    def save_runs(self, inv_id: str, runs: list[TransformRun]) -> None:
        if not runs:
            return
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO transform_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (inv_id, r.id, r.task_id, r.transform, r.connector, r.status, r.started_at, r.finished_at, r.requests,
                 r.new_entities, r.depth, r.trigger,
                 self.enc({"input_key": r.input_key, "input_label": r.input_label, "message": r.message, "sent": r.sent,
                           "destinations": r.destinations, "new_relationships": r.new_relationships}))
                for r in runs])

    def load_runs(self, inv_id: str) -> list[TransformRun]:
        out = []
        for r in self._q("SELECT id, task_id, transform, connector, status, started_at, finished_at, requests, new_entities, "
                         "depth, trigger, data_enc FROM transform_runs WHERE investigation_id=? ORDER BY started_at", (inv_id,)):
            d = self.dec(r[11])
            out.append(TransformRun(id=r[0], task_id=r[1] or "", transform=r[2], connector=r[3], status=r[4],
                                    started_at=r[5], finished_at=r[6], requests=r[7], new_entities=r[8], depth=r[9],
                                    trigger=r[10], input_key=d.get("input_key", ""), input_label=d.get("input_label", ""),
                                    message=d.get("message", ""), sent=d.get("sent", ""),
                                    destinations=d.get("destinations") or [], new_relationships=d.get("new_relationships", 0)))
        return out

    def save_tasks(self, inv_id: str, tasks: list[Task]) -> None:
        if not tasks:
            return
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO search_tasks VALUES(?,?,?,?,?,?,?,?,?,?)", [
                (inv_id, t.id, t.transform, t.status, t.priority, t.depth, t.trigger, t.created_at, t.run_id or None,
                 self.enc({"entity_key": t.entity_key, "reason": t.reason, "note": t.note})) for t in tasks])

    def load_tasks(self, inv_id: str) -> list[Task]:
        out = []
        for r in self._q("SELECT id, transform, status, priority, depth, trigger, created_at, run_id, data_enc FROM search_tasks "
                         "WHERE investigation_id=?", (inv_id,)):
            d = self.dec(r[8])
            out.append(Task(id=r[0], transform=r[1], entity_key=d.get("entity_key", ""), priority=r[3], depth=r[4],
                            reason=d.get("reason", ""), status=r[2], trigger=r[5], created_at=r[6], run_id=r[7] or "",
                            note=d.get("note", "")))
        return out

    # ------------------------------------------------------------------ audit log of investigator decisions
    def log_decision(self, inv_id: str, action: str, detail: dict) -> None:
        with self.tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM user_decisions WHERE investigation_id=?", (inv_id,)).fetchone()[0]
            c.execute("INSERT INTO user_decisions VALUES(?,?,?,?,?)", (inv_id, seq, utcnow(), action, self.enc(detail)))

    def decisions(self, inv_id: str) -> list[dict]:
        return [{"seq": r[0], "at": r[1], "action": r[2], **(self.dec(r[3]) or {})}
                for r in self._q("SELECT seq, at, action, data_enc FROM user_decisions WHERE investigation_id=? ORDER BY seq",
                                 (inv_id,))]

    # ------------------------------------------------------------------ assistant memory (per investigation)
    def assistant_append(self, inv_id: str, kind: str, data: dict, provider: str = "") -> int:
        with self.tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM assistant_log WHERE investigation_id=?",
                            (inv_id,)).fetchone()[0]
            c.execute("INSERT INTO assistant_log VALUES(?,?,?,?,?,?)", (inv_id, seq, utcnow(), kind, provider, self.enc(data)))
        return seq

    def assistant_log(self, inv_id: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
        rows = self._q("SELECT seq, at, kind, provider, data_enc FROM assistant_log WHERE investigation_id=? ORDER BY seq",
                       (inv_id,))
        return [{**(self.dec(r[4]) or {}), "seq": r[0], "at": r[1], "kind": r[2], "provider": r[3]}
                for r in rows if kinds is None or r[2] in kinds]

    # ------------------------------------------------------------------ usage and cost records
    USAGE_COLS = ("investigation_id", "at", "provider", "service", "model", "prompt_tokens", "completion_tokens",
                  "cached_tokens", "reasoning_tokens", "total_tokens", "requests", "estimated_cost", "actual_cost",
                  "currency", "cost_status", "data_json")

    def usage_add(self, rec: dict) -> int:
        row = {k: rec.get(k) for k in self.USAGE_COLS}
        row["at"] = row["at"] or utcnow()
        row["model"] = row["model"] or ""
        row["currency"] = row["currency"] or "USD"
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "total_tokens"):
            row[k] = int(row[k] or 0)
        row["requests"] = int(row["requests"] or 1)
        row["data_json"] = json.dumps(rec.get("data") or {})
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO usage_records({', '.join(self.USAGE_COLS)}) VALUES({', '.join('?' * len(self.USAGE_COLS))})",
                            tuple(row[k] for k in self.USAGE_COLS))
            return int(cur.lastrowid)

    def usage_list(self, inv_id: str | None = None, *, all_records: bool = False, limit: int = 5000) -> list[dict]:
        if all_records:
            rows = self._q(f"SELECT id, {', '.join(self.USAGE_COLS)} FROM usage_records ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._q(f"SELECT id, {', '.join(self.USAGE_COLS)} FROM usage_records WHERE investigation_id IS ? "
                           "ORDER BY id LIMIT ?", (inv_id, limit))
        out = []
        for r in rows:
            d = dict(zip(("id", *self.USAGE_COLS), r))
            d["data"] = json.loads(d.pop("data_json") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ investigation notes
    def notes(self, inv_id: str) -> list[dict]:
        return [{**(self.dec(r[3]) or {}), "id": r[0], "created_at": r[1], "updated_at": r[2]}
                for r in self._q("SELECT id, created_at, updated_at, data_enc FROM investigation_notes WHERE investigation_id=? "
                                 "ORDER BY created_at", (inv_id,))]

    def note_put(self, inv_id: str, note_id: str, data: dict) -> dict:
        now = utcnow()
        with self.tx() as c:
            row = c.execute("SELECT created_at FROM investigation_notes WHERE investigation_id=? AND id=?",
                            (inv_id, note_id)).fetchone()
            c.execute("INSERT OR REPLACE INTO investigation_notes VALUES(?,?,?,?,?)",
                      (inv_id, note_id, row[0] if row else now, now, self.enc(data)))
        return {**data, "id": note_id, "created_at": row[0] if row else now, "updated_at": now}

    def note_delete(self, inv_id: str, note_id: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM investigation_notes WHERE investigation_id=? AND id=?", (inv_id, note_id)).rowcount > 0

    # ------------------------------------------------------------------ scoped resets
    def reset_history(self) -> int:
        """Deletes every investigation (cascade: graph, runs, assistant memory, notes, caches, raw evidence, usage)."""
        n = self._q("SELECT COUNT(*) FROM investigations")[0][0]
        with self.tx() as c:
            c.execute("DELETE FROM investigations")
            c.execute("DELETE FROM usage_records WHERE investigation_id IS NULL")
        with self._lock:
            self._conn.execute("VACUUM")
        return n

    def reset_credentials(self) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM api_credentials").rowcount

    def reset_settings(self, keys: list[str], prefixes: tuple[str, ...] = ()) -> int:
        n = 0
        with self.tx() as c:
            for k in keys:
                n += c.execute("DELETE FROM settings WHERE key=?", (k,)).rowcount
            for p in prefixes:
                n += c.execute("DELETE FROM settings WHERE key LIKE ?", (p + "%",)).rowcount
        return n

    # ------------------------------------------------------------------ http cache (per investigation)
    def cache_get(self, inv_id: str, key: str) -> dict | None:
        rows = self._q("SELECT expires_at, data_enc FROM http_cache WHERE investigation_id=? AND key_hash=?",
                       (inv_id, self.blind(inv_id, key)))
        if not rows or rows[0][0] < time.time():
            return None
        return self.dec(rows[0][1])

    def cache_set(self, inv_id: str, key: str, value: dict, ttl: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO http_cache VALUES(?,?,?,?)",
                               (inv_id, self.blind(inv_id, key), time.time() + ttl, self.enc(value)))

    # ------------------------------------------------------------------ settings, credentials, connector prefs
    def get_setting(self, key: str) -> Any:
        rows = self._q("SELECT data_enc FROM settings WHERE key=?", (key,))
        return self.dec(rows[0][0]) if rows else None

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, self.enc(value)))

    def get_credential(self, key: str) -> str | None:
        rows = self._q("SELECT value_enc FROM api_credentials WHERE key=?", (key,))
        return self.dec(rows[0][0]) if rows else None

    def set_credential(self, key: str, value: str | None) -> None:
        with self.tx() as c:
            if not value:
                c.execute("DELETE FROM api_credentials WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO api_credentials(key, value_enc, updated_at) VALUES(?,?,?)",
                          (key, self.enc(value), utcnow()))

    def credential_keys(self) -> set[str]:
        return {r[0] for r in self._q("SELECT key FROM api_credentials")}

    def connector_prefs(self) -> dict[str, dict]:
        out = {}
        for r in self._q("SELECT name, enabled, auto_run, data_json FROM connectors"):
            d = json.loads(r[3] or "{}")
            d.update({"enabled": None if r[1] is None else bool(r[1]), "auto_run": None if r[2] is None else bool(r[2])})
            out[r[0]] = d
        return out

    def set_connector_pref(self, name: str, *, enabled: bool | None = None, auto_run: bool | None = None,
                           extra: dict | None = None, clear: bool = False) -> None:
        cur = self.connector_prefs().get(name, {})
        if clear:
            with self.tx() as c:
                c.execute("DELETE FROM connectors WHERE name=?", (name,))
            return
        en = cur.get("enabled") if enabled is None else enabled
        ar = cur.get("auto_run") if auto_run is None else auto_run
        data = {k: v for k, v in cur.items() if k not in ("enabled", "auto_run")}
        data.update(extra or {})
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO connectors VALUES(?,?,?,?,?)",
                      (name, None if en is None else int(en), None if ar is None else int(ar), utcnow(), json.dumps(data)))

    # ------------------------------------------------------------------ jurisdiction catalog (seeded from the registry)
    # ------------------------------------------------------------------ raw evidence
    def save_raw(self, inv_id: str, items: dict[str, dict]) -> None:
        """Raw responses that facts were drawn from, keyed by sha256 (body, URL and headers encrypted)."""
        import base64
        with self.tx() as c:
            for digest, v in items.items():
                payload = {**{k: v.get(k) for k in ("url", "method", "status", "content_type", "connector")},
                           "body": base64.b64encode(v.get("body") or b"").decode()}
                c.execute("INSERT OR IGNORE INTO raw_evidence VALUES(?,?,?,?,?)",
                          (inv_id, digest, v.get("retrieved_at") or utcnow(), int(v.get("size") or 0), self.enc(payload)))

    def get_raw(self, inv_id: str, digest: str) -> dict | None:
        rows = self._q("SELECT retrieved_at, size, data_enc FROM raw_evidence WHERE investigation_id=? AND digest=?",
                       (inv_id, digest))
        if not rows:
            return None
        retrieved_at, size, data = rows[0]
        return {"digest": digest, "retrieved_at": retrieved_at, "size": size, **self.dec(data)}

    def raw_count(self, inv_id: str) -> int:
        return self._q("SELECT COUNT(*) FROM raw_evidence WHERE investigation_id=?", (inv_id,))[0][0]

    # ------------------------------------------------------------------ source health (no personal data)
    def record_health(self, outcomes: list[tuple[str, str, str]]) -> None:
        from ..registry.health import merge_record
        with self.tx() as c:
            for name, status, at in outcomes:
                row = c.execute("SELECT data_json FROM source_health WHERE name=?", (name,)).fetchone()
                rec = merge_record(json.loads(row[0]) if row else None, status, at or utcnow())
                c.execute("INSERT OR REPLACE INTO source_health VALUES(?,?,?)", (name, utcnow(), json.dumps(rec)))

    def health_records(self) -> dict[str, dict]:
        return {name: json.loads(data) for name, data in self._q("SELECT name, data_json FROM source_health")}

    # ------------------------------------------------------------------ catalog: the user layer (registry/catalog.py)
    def catalog_version(self) -> int:
        rows = self._q("SELECT value FROM meta WHERE key='catalog_version'")
        return int(rows[0][0]) if rows else 0

    @staticmethod
    def _bump_catalog(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('catalog_version', '0')")
        c.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='catalog_version'")

    def catalog_load(self) -> dict:
        """Every user-created catalog record, with its relationships (a handful of queries, cached by the caller)."""
        juris: dict[str, list[str]] = {}
        for sid, code in self._q("SELECT source_id, code FROM catalog_source_jurisdictions ORDER BY source_id, code"):
            juris.setdefault(sid, []).append(code)
        plats: dict[str, list[str]] = {}
        for sid, pid in self._q("SELECT source_id, platform_id FROM catalog_source_platforms ORDER BY source_id, platform_id"):
            plats.setdefault(sid, []).append(pid)
        sources = []
        for (sid, title, url, _key, category, stype, access, connector, enabled, review, created, updated,
             data) in self._q("SELECT * FROM catalog_sources ORDER BY created_at, id"):
            d = json.loads(data or "{}")
            sources.append({"id": sid, "title": title, "url": url, "category": category, "source_type": stype,
                            "access": access, "connector": connector or "", "enabled": bool(enabled), "review": review,
                            "created_at": created, "updated_at": updated, "jurisdictions": juris.get(sid, []),
                            "platforms": plats.get(sid, []), **{k: d.get(k) for k in CATALOG_SOURCE_DATA}})
        categories = [{"id": cid, "title": title, "group": grp, "created_at": created, "updated_at": updated,
                       **json.loads(data or "{}")}
                      for cid, title, grp, created, updated, data in
                      self._q("SELECT * FROM catalog_categories ORDER BY title COLLATE NOCASE")]
        platforms = [{"id": pid, "name": name, "host": host, "category": category, "created_at": created,
                      "updated_at": updated, **json.loads(data or "{}")}
                     for pid, name, host, category, created, updated, data in
                     self._q("SELECT * FROM catalog_platforms ORDER BY name COLLATE NOCASE")]
        ignores = [(code, item) for code, item in self._q("SELECT code, item FROM catalog_audit_ignores")]
        return {"version": self.catalog_version(), "sources": sources, "categories": categories, "platforms": platforms,
                "ignores": ignores}

    @staticmethod
    def _created(c: sqlite3.Connection, table: str, row_id: str) -> str | None:
        row = c.execute(f"SELECT created_at FROM {table} WHERE id=?", (row_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _write_source(c: sqlite3.Connection, s: dict, created: str, updated: str) -> None:
        c.execute("INSERT OR REPLACE INTO catalog_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (s["id"], s["title"], s["url"], s["url_key"], s["category"], s["source_type"], s["access"],
                   s.get("connector") or None, int(bool(s.get("enabled", True))), s.get("review") or "not_verified",
                   created, updated, json.dumps({k: s.get(k) for k in CATALOG_SOURCE_DATA})))
        c.execute("DELETE FROM catalog_source_jurisdictions WHERE source_id=?", (s["id"],))
        c.executemany("INSERT INTO catalog_source_jurisdictions VALUES(?,?)", [(s["id"], j) for j in s["jurisdictions"]])
        c.execute("DELETE FROM catalog_source_platforms WHERE source_id=?", (s["id"],))
        c.executemany("INSERT INTO catalog_source_platforms VALUES(?,?)", [(s["id"], p) for p in s["platforms"]])

    @staticmethod
    def _write_category(c: sqlite3.Connection, cat: dict, created: str, updated: str) -> None:
        c.execute("INSERT OR REPLACE INTO catalog_categories VALUES(?,?,?,?,?,?)",
                  (cat["id"], cat["title"], cat["group"], created, updated,
                   json.dumps({"description": cat.get("description") or ""})))

    @staticmethod
    def _write_platform(c: sqlite3.Connection, p: dict, created: str, updated: str) -> None:
        c.execute("INSERT OR REPLACE INTO catalog_platforms VALUES(?,?,?,?,?,?,?)",
                  (p["id"], p["name"], p["host"], p["category"], created, updated,
                   json.dumps({k: p.get(k) for k in ("url", "profile_url", "countries", "region", "notes", "status",
                                                     "public_profile")})))

    def catalog_put_source(self, s: dict) -> None:
        """Inserts or replaces one user source and its jurisdiction and platform relationships (one transaction)."""
        with self.tx() as c:
            now = utcnow()
            self._write_source(c, s, self._created(c, "catalog_sources", s["id"]) or now, now)
            self._bump_catalog(c)

    def catalog_delete_source(self, source_id: str) -> bool:
        with self.tx() as c:
            gone = c.execute("DELETE FROM catalog_sources WHERE id=?", (source_id,)).rowcount > 0   # relationships cascade
            if gone:
                self._bump_catalog(c)
            return gone

    def catalog_put_category(self, cat: dict) -> None:
        with self.tx() as c:
            now = utcnow()
            self._write_category(c, cat, self._created(c, "catalog_categories", cat["id"]) or now, now)
            self._bump_catalog(c)

    def catalog_delete_category(self, cat_id: str) -> list[str]:
        """Deletes a user category unless sources still use it; returns the ids of those sources (empty = deleted)."""
        with self.tx() as c:
            used = [r[0] for r in c.execute("SELECT id FROM catalog_sources WHERE category=?", (cat_id,))]
            if not used and c.execute("DELETE FROM catalog_categories WHERE id=?", (cat_id,)).rowcount:
                self._bump_catalog(c)
            return used

    def catalog_put_platform(self, p: dict) -> None:
        with self.tx() as c:
            now = utcnow()
            self._write_platform(c, p, self._created(c, "catalog_platforms", p["id"]) or now, now)
            self._bump_catalog(c)

    def catalog_import(self, *, categories: list[dict], platforms: list[dict], sources: list[dict],
                       ignores: list[tuple[str, str]], preferences: dict[str, dict]) -> None:
        """Writes an import plan (registry/catalog.py: plan_import) in ONE transaction: all of it or nothing.
        Records keep the creation and change times of the exported file."""
        with self.tx() as c:
            now = utcnow()
            for cat in categories:
                self._write_category(c, cat, cat.get("created_at") or now, cat.get("updated_at") or now)
            for p in platforms:
                self._write_platform(c, p, p.get("created_at") or now, p.get("updated_at") or now)
            for s in sources:
                self._write_source(c, s, s.get("created_at") or now, s.get("updated_at") or now)
            c.executemany("INSERT OR IGNORE INTO catalog_audit_ignores VALUES(?,?,?)",
                          [(code, item, now) for code, item in ignores])
            for name, pref in preferences.items():
                row = c.execute("SELECT data_json FROM connectors WHERE name=?", (name,)).fetchone()
                en, ar = pref.get("enabled"), pref.get("auto_run")
                c.execute("INSERT OR REPLACE INTO connectors VALUES(?,?,?,?,?)",
                          (name, None if en is None else int(en), None if ar is None else int(ar), now,
                           row[0] if row else "{}"))
            self._bump_catalog(c)

    def catalog_delete_platform(self, platform_id: str) -> bool:
        """Deletes a user platform and every source → platform link to it (no dangling relationships)."""
        with self.tx() as c:
            c.execute("DELETE FROM catalog_source_platforms WHERE platform_id=?", (platform_id,))
            gone = c.execute("DELETE FROM catalog_platforms WHERE id=?", (platform_id,)).rowcount > 0
            if gone:
                self._bump_catalog(c)
            return gone

    def catalog_set_ignore(self, code: str, item: str, ignored: bool) -> None:
        with self.tx() as c:
            if ignored:
                c.execute("INSERT OR REPLACE INTO catalog_audit_ignores VALUES(?,?,?)", (code, item, utcnow()))
            else:
                c.execute("DELETE FROM catalog_audit_ignores WHERE code=? AND item=?", (code, item))
            self._bump_catalog(c)

    def catalog_custom_counts(self) -> dict[str, int]:
        """Everything the catalog reset would remove, counted (for the summary shown before it runs)."""
        one = lambda sql: self._q(sql)[0][0]  # noqa: E731
        return {
            "sources": one("SELECT COUNT(*) FROM catalog_sources WHERE source_type != 'reference'"),
            "links": one("SELECT COUNT(*) FROM catalog_sources WHERE source_type = 'reference'"),
            "categories": one("SELECT COUNT(*) FROM catalog_categories"),
            "platforms": one("SELECT COUNT(*) FROM catalog_platforms"),
            "jurisdiction_links": one("SELECT COUNT(*) FROM catalog_source_jurisdictions"),
            "platform_links": one("SELECT COUNT(*) FROM catalog_source_platforms"),
            "connector_links": one("SELECT COUNT(*) FROM catalog_sources WHERE connector IS NOT NULL AND connector != ''"),
            "audit_ignores": one("SELECT COUNT(*) FROM catalog_audit_ignores"),
            "source_preferences": one("SELECT COUNT(*) FROM connectors"),
            "discovery_results": one("SELECT COUNT(*) FROM settings WHERE key LIKE 'discovery:%'"),
            "legacy_custom": one("SELECT COUNT(*) FROM source_capabilities WHERE custom=1"),
        }

    def catalog_reset(self) -> dict[str, int]:
        """The catalog reset: removes every user-created catalog record and relationship, the investigator's
        on/off and auto-run overrides of built-in sources, ignored audit suggestions and saved connector-search
        results — in one transaction. Investigations, API keys and settings are not touched. Built-in records are
        not stored here, so nothing needs restoring: the defaults of the installed version apply again."""
        counts = self.catalog_custom_counts()
        with self.tx() as c:
            for table in ("catalog_source_platforms", "catalog_source_jurisdictions", "catalog_sources", "catalog_categories",
                          "catalog_platforms", "catalog_audit_ignores", "connectors"):
                c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM settings WHERE key LIKE 'discovery:%'")
            c.execute("DELETE FROM source_capabilities WHERE custom=1")
            self._bump_catalog(c)
        return counts

    def seed_jurisdictions(self, jurisdictions: list[dict]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM jurisdictions WHERE custom=0")
            c.execute("DELETE FROM source_capabilities WHERE custom=0")
            for j in jurisdictions:
                c.execute("INSERT OR REPLACE INTO jurisdictions VALUES(?,?,?,0,?)",
                          (j["code"], j["name"], j["region"], json.dumps({k: j[k] for k in ("subregion", "legal_notes")})))
                for s in j["sources"]:
                    c.execute("INSERT OR REPLACE INTO source_capabilities VALUES(?,?,?,?,?,?,0,?)",
                              (s["id"], j["code"], s["kind"], s["access"], int(s["name_search"]), s.get("connector") or None,
                               json.dumps({k: s.get(k) for k in ("title", "url", "api", "notes", "person_data", "anonymized")})))

    # ------------------------------------------------------------------ wipe
    def wipe_all(self) -> None:
        """Deletes the database, the key file and the salt (irreversible)."""
        self.close()
        for name in (DB_FILE, DB_FILE + "-wal", DB_FILE + "-shm", KEY_FILE, SALT_FILE):
            try:
                (self.data_dir / name).unlink()
            except FileNotFoundError:
                pass


class DbCache:
    """Per-investigation HTTP cache backed by the encrypted database (deleted with the investigation)."""

    def __init__(self, db: Database, inv_id: str) -> None:
        self.db, self.inv_id = db, inv_id

    async def get(self, key: str) -> dict | None:
        import asyncio
        return await asyncio.to_thread(self.db.cache_get, self.inv_id, key)

    async def set(self, key: str, value: dict, ttl: int) -> None:
        import asyncio
        await asyncio.to_thread(self.db.cache_set, self.inv_id, key, value, ttl)


__all__ = ["Database", "DbCache", "Vault", "entity_from_dict"]
