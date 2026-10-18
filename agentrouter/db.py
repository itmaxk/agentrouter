"""SQLite state: raw probes, merged up/down intervals, switch log, key/value state."""
import json
import sqlite3
import time
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, endpoint TEXT NOT NULL DEFAULT 'chat',
    endpoint_types TEXT, first_seen REAL, last_seen REAL, listed INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, model TEXT NOT NULL, endpoint TEXT, status TEXT NOT NULL,
    latency_ms INTEGER, http_code INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS probes_model_ts ON probes(model, ts);
CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY, model TEXT NOT NULL, up INTEGER NOT NULL,
    start_ts REAL NOT NULL, end_ts REAL NOT NULL, open INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS intervals_model_ts ON intervals(model, start_ts);
CREATE TABLE IF NOT EXISTS switches (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, target TEXT NOT NULL,
    from_model TEXT, to_model TEXT NOT NULL, reason TEXT, applied INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "state.db"
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # --- models ---
    def upsert_model(self, mid, endpoint_types, ts):
        self.conn.execute(
            "INSERT INTO models(id, endpoint_types, first_seen, last_seen, listed) VALUES(?,?,?,?,1) "
            "ON CONFLICT(id) DO UPDATE SET endpoint_types=excluded.endpoint_types, last_seen=excluded.last_seen, listed=1",
            (mid, json.dumps(endpoint_types), ts, ts))

    def mark_unlisted(self, keep_ids):
        marks = ",".join("?" * len(keep_ids)) or "''"
        self.conn.execute(f"UPDATE models SET listed=0 WHERE id NOT IN ({marks})", list(keep_ids))

    def set_endpoint(self, mid, endpoint):
        self.conn.execute("UPDATE models SET endpoint=? WHERE id=?", (endpoint, mid))

    def models(self, listed_only=True):
        return self.q("SELECT * FROM models" + (" WHERE listed=1" if listed_only else "") + " ORDER BY id")

    # --- probes / intervals ---
    def add_probe(self, ts, r):
        self.conn.execute(
            "INSERT INTO probes(ts, model, endpoint, status, latency_ms, http_code, error) VALUES(?,?,?,?,?,?,?)",
            (ts, r.model, r.endpoint, r.status, r.latency_ms, r.http_code, r.error))

    def last_probe(self, model):
        rows = self.q("SELECT * FROM probes WHERE model=? ORDER BY ts DESC LIMIT 1", (model,))
        return rows[0] if rows else None

    def open_interval(self, model):
        rows = self.q("SELECT * FROM intervals WHERE model=? AND open=1 ORDER BY start_ts DESC LIMIT 1", (model,))
        return rows[0] if rows else None

    def record_status(self, model, up: bool, ts: float, max_gap: float):
        """Extend the open interval, or close it and start a new one on a flip or an observation gap."""
        cur = self.open_interval(model)
        if cur and ts - cur["end_ts"] > max_gap:
            self.conn.execute("UPDATE intervals SET open=0 WHERE id=?", (cur["id"],))
            cur = None
        if cur and bool(cur["up"]) == up:
            self.conn.execute("UPDATE intervals SET end_ts=? WHERE id=?", (ts, cur["id"]))
            return False
        if cur:
            self.conn.execute("UPDATE intervals SET end_ts=?, open=0 WHERE id=?", (ts, cur["id"]))
        self.conn.execute("INSERT INTO intervals(model, up, start_ts, end_ts, open) VALUES(?,?,?,?,1)",
                          (model, int(up), ts, ts))
        return cur is not None

    def intervals(self, model=None, since=0.0):
        sql, args = "SELECT * FROM intervals WHERE end_ts>=?", [since]
        if model:
            sql += " AND model=?"
            args.append(model)
        return self.q(sql + " ORDER BY start_ts", args)

    def prune(self, retention_days):
        self.conn.execute("DELETE FROM probes WHERE ts<?", (time.time() - retention_days * 86400,))

    # --- switches / state ---
    def add_switch(self, ts, target, from_model, to_model, reason, applied=True):
        self.conn.execute("INSERT INTO switches(ts, target, from_model, to_model, reason, applied) VALUES(?,?,?,?,?,?)",
                          (ts, target, from_model, to_model, reason, int(applied)))

    def switches(self, limit=50, target=None):
        if target:
            return self.q("SELECT * FROM switches WHERE target=? ORDER BY ts DESC LIMIT ?", (target, limit))
        return self.q("SELECT * FROM switches ORDER BY ts DESC LIMIT ?", (limit,))

    def last_switch_ts(self, target):
        rows = self.q("SELECT ts FROM switches WHERE target=? AND applied=1 ORDER BY ts DESC LIMIT 1", (target,))
        return rows[0]["ts"] if rows else 0.0

    def get(self, key, default=None):
        rows = self.q("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key, value):
        if value is None:
            self.conn.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES(?,?)", (key, json.dumps(value)))
