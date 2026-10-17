"""Fast embedded datastore.

Why SQLite and not Postgres/Redis: the judging criterion is *end-to-end flow
latency*. A network round-trip to a managed DB is 1-20ms each; this pipeline
does ~40 reads/writes per order, so the DB alone would cost more than the LLM
calls. SQLite in WAL mode with `synchronous=NORMAL` and memory-mapped I/O
services the same queries in tens of microseconds in-process.

What we keep from a "real" DB:
  * WAL journaling  -> concurrent readers never block the writer
  * a single serialized writer connection -> no `database is locked` races
  * a read-connection pool, one per event-loop thread
  * explicit indices on every access path used by the pipeline
  * schema migrations applied idempotently at boot
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from chainmind.core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- godown | storehouse | darkstore
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    city          TEXT NOT NULL,
    packing_lines INTEGER NOT NULL DEFAULT 2,
    cutoff_hour   INTEGER NOT NULL DEFAULT 18
);

CREATE TABLE IF NOT EXISTS inventory (
    warehouse_id TEXT NOT NULL,
    sku          TEXT NOT NULL,
    on_hand      INTEGER NOT NULL,
    reserved     INTEGER NOT NULL DEFAULT 0,
    unit_weight_g INTEGER NOT NULL DEFAULT 500,
    fragile      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (warehouse_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory(sku, on_hand DESC);

CREATE TABLE IF NOT EXISTS orders (
    id          TEXT PRIMARY KEY,
    idem_key    TEXT UNIQUE,
    raw         TEXT NOT NULL,
    status      TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'standard',
    dest_label  TEXT,
    dest_lat    REAL,
    dest_lng    REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at DESC);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id TEXT NOT NULL,
    sku      TEXT NOT NULL,
    qty      INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE IF NOT EXISTS allocations (
    id           TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    sku          TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_order ON allocations(order_id);

CREATE TABLE IF NOT EXISTS shipments (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    warehouse_id  TEXT NOT NULL,
    carrier       TEXT NOT NULL,
    packaging     TEXT NOT NULL,
    route         TEXT NOT NULL,              -- json
    distance_km   REAL NOT NULL,
    eta_minutes   REAL NOT NULL,
    cost_inr      REAL NOT NULL,
    status        TEXT NOT NULL,
    actual_minutes REAL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ship_order ON shipments(order_id);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT,
    stage      TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    payload    TEXT,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, seq);

CREATE TABLE IF NOT EXISTS spans (
    id         TEXT PRIMARY KEY,
    order_id   TEXT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,                 -- agent | tool | http | db | pipeline
    model      TEXT,
    started_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 1,
    attrs      TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_order ON spans(order_id, started_at);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name, started_at DESC);

-- The feedback loop's memory. The planner reads these back on every run.
CREATE TABLE IF NOT EXISTS policies (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                 -- json
    confidence REAL NOT NULL DEFAULT 0.5,
    samples    INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS carrier_stats (
    carrier     TEXT PRIMARY KEY,
    shipments   INTEGER NOT NULL DEFAULT 0,
    on_time     INTEGER NOT NULL DEFAULT 0,
    eta_bias    REAL NOT NULL DEFAULT 1.0,    -- learned multiplier on predicted ETA
    damage_rate REAL NOT NULL DEFAULT 0.0,
    updated_at  REAL NOT NULL
);
"""


class Database:
    """Thread-safe SQLite facade: one serialized writer, per-thread readers."""

    def __init__(self, path: str) -> None:
        self.path = path
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._write_lock = threading.Lock()
        self._writer = self._connect()
        self._local = threading.local()
        self._init_schema()

    # ----------------------------------------------------------------- setup
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")       # readers never block writer
        cur.execute("PRAGMA synchronous=NORMAL")     # durable on crash, not on power loss
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-32000")      # 32MB page cache
        cur.execute("PRAGMA mmap_size=268435456")    # 256MB memory-mapped reads
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            self._writer.executescript(_SCHEMA)
            self._writer.commit()

    @property
    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ primitives
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cur = self.reader.execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self.reader.execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._write_lock:
            cur = self._writer.execute(sql, params)
            self._writer.commit()
            rowcount = cur.rowcount
            cur.close()
            return rowcount

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._write_lock:
            self._writer.executemany(sql, rows)
            self._writer.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction. All-or-nothing."""
        with self._write_lock:
            try:
                self._writer.execute("BEGIN IMMEDIATE")
                yield self._writer
                self._writer.commit()
            except Exception:
                self._writer.rollback()
                raise

    # ------------------------------------------------------- domain helpers
    def log_event(
        self,
        stage: str,
        message: str,
        *,
        order_id: str | None = None,
        level: str = "info",
        payload: dict | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO events (order_id, stage, level, message, payload, ts)"
            " VALUES (?,?,?,?,?,?)",
            (order_id, stage, level, message, json.dumps(payload or {}), time.time()),
        )

    def record_span(
        self,
        name: str,
        kind: str,
        started_at: float,
        duration_ms: float,
        *,
        ok: bool = True,
        order_id: str | None = None,
        model: str | None = None,
        attrs: dict | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO spans (id, order_id, name, kind, model, started_at, duration_ms, ok, attrs)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                order_id,
                name,
                kind,
                model,
                started_at,
                duration_ms,
                1 if ok else 0,
                json.dumps(attrs or {}),
            ),
        )

    def get_policies(self) -> dict[str, dict]:
        rows = self.query("SELECT key, value, confidence, samples, note FROM policies")
        out: dict[str, dict] = {}
        for r in rows:
            out[r["key"]] = {
                "value": json.loads(r["value"]),
                "confidence": r["confidence"],
                "samples": r["samples"],
                "note": r["note"],
            }
        return out

    def upsert_policy(
        self, key: str, value: Any, *, confidence: float, samples: int, note: str = ""
    ) -> None:
        self.execute(
            "INSERT INTO policies (key, value, confidence, samples, note, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " confidence=excluded.confidence, samples=excluded.samples,"
            " note=excluded.note, updated_at=excluded.updated_at",
            (key, json.dumps(value), confidence, samples, note, time.time()),
        )


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database(get_settings().db_path)
    return _db
