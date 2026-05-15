"""SQLite persistence for Module S+J."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .config import (
    BLOCK_KINDS, EVENT_KINDS, GOAL_STATUSES, OUTCOME_STATUSES, PlannerConfig,
)
from .models import Block, Event, Goal, Outcome

logger = logging.getLogger("ultron.planner.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    dream_kind TEXT NOT NULL DEFAULT 'personal',
    target_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goals_status ON goals(status);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    weight REAL NOT NULL DEFAULT 1.0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_outcomes_goal ON outcomes(goal_id);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'focus',
    outcome_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (outcome_id) REFERENCES outcomes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_blocks_start ON blocks(ts_start);
CREATE INDEX IF NOT EXISTS ix_blocks_outcome ON blocks(outcome_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'alarm',
    payload TEXT NOT NULL DEFAULT '',
    fired_at REAL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_fired ON events(fired_at);
"""


class PlannerStore:
    def __init__(self, config: PlannerConfig) -> None:
        self._cfg = config
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
                (str(SCHEMA_VERSION),),
            )
        logger.info("planner store ready at %s", self._path)

    # ── Goals ──────────────────────────────────────────────────────────

    def upsert_goal(self, g: Goal) -> int:
        if not g.title:
            raise ValueError("goal title is required")
        if g.status not in GOAL_STATUSES:
            raise ValueError(f"bad goal status {g.status!r}")
        with self._connect() as conn:
            if g.id is None:
                cur = conn.execute(
                    "INSERT INTO goals(title,dream_kind,target_date,status,note,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (g.title, g.dream_kind, g.target_date, g.status, g.note, g.created_at),
                )
                return int(cur.lastrowid or 0)
            conn.execute(
                """
                UPDATE goals SET title=?, dream_kind=?, target_date=?, status=?, note=?
                WHERE id=?
                """,
                (g.title, g.dream_kind, g.target_date, g.status, g.note, g.id),
            )
            return g.id

    def list_goals(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM goals WHERE status=? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM goals ORDER BY status='active' DESC, created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_goal(self, goal_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            return dict(r) if r else None

    def delete_goal(self, goal_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
            return cur.rowcount > 0

    # ── Outcomes ───────────────────────────────────────────────────────

    def upsert_outcome(self, o: Outcome) -> int:
        if o.status not in OUTCOME_STATUSES:
            raise ValueError(f"bad outcome status {o.status!r}")
        if not o.title:
            raise ValueError("outcome title is required")
        with self._connect() as conn:
            # Validate goal_id exists.
            r = conn.execute("SELECT 1 FROM goals WHERE id=?", (o.goal_id,)).fetchone()
            if not r:
                raise ValueError(f"goal {o.goal_id} not found")
            if o.id is None:
                cur = conn.execute(
                    "INSERT INTO outcomes(goal_id,title,status,weight,note,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (o.goal_id, o.title, o.status, o.weight, o.note, o.created_at),
                )
                return int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE outcomes SET goal_id=?, title=?, status=?, weight=?, note=? WHERE id=?",
                (o.goal_id, o.title, o.status, o.weight, o.note, o.id),
            )
            return o.id

    def list_outcomes(self, *, goal_id: Optional[int] = None,
                      status: Optional[str] = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if goal_id is not None:
            clauses.append("goal_id = ?"); params.append(goal_id)
        if status:
            clauses.append("status = ?"); params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM outcomes {where} ORDER BY created_at DESC", params
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Blocks ─────────────────────────────────────────────────────────

    def schedule_block(self, b: Block) -> int:
        if b.kind not in BLOCK_KINDS:
            raise ValueError(f"bad block kind {b.kind!r}")
        if b.ts_end <= b.ts_start:
            raise ValueError("ts_end must be > ts_start")
        if not b.title:
            raise ValueError("block title is required")
        if b.outcome_id is not None:
            with self._connect() as conn:
                r = conn.execute("SELECT 1 FROM outcomes WHERE id=?", (b.outcome_id,)).fetchone()
                if not r:
                    raise ValueError(f"outcome {b.outcome_id} not found")
        with self._connect() as conn:
            if b.id is None:
                cur = conn.execute(
                    "INSERT INTO blocks(ts_start,ts_end,title,kind,outcome_id,note) "
                    "VALUES(?,?,?,?,?,?)",
                    (b.ts_start, b.ts_end, b.title, b.kind, b.outcome_id, b.note),
                )
                return int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE blocks SET ts_start=?, ts_end=?, title=?, kind=?, outcome_id=?, note=? "
                "WHERE id=?",
                (b.ts_start, b.ts_end, b.title, b.kind, b.outcome_id, b.note, b.id),
            )
            return b.id

    def list_blocks(self, *,
                    since_ts: Optional[float] = None,
                    until_ts: Optional[float] = None,
                    outcome_id: Optional[int] = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts_end >= ?"); params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts_start < ?"); params.append(until_ts)
        if outcome_id is not None:
            clauses.append("outcome_id = ?"); params.append(outcome_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM blocks {where} ORDER BY ts_start ASC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_block(self, bid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM blocks WHERE id=?", (bid,))
            return cur.rowcount > 0

    # ── Events / alarms ────────────────────────────────────────────────

    def schedule_event(self, e: Event) -> int:
        if e.kind not in EVENT_KINDS:
            raise ValueError(f"bad event kind {e.kind!r}")
        if not e.title:
            raise ValueError("event title is required")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO events(ts,title,kind,payload,fired_at) VALUES(?,?,?,?,?)",
                (e.ts, e.title, e.kind, e.payload, e.fired_at),
            )
            return int(cur.lastrowid or 0)

    def pending_events(self, *, until_ts: float, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self._cfg.max_query_rows))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE fired_at IS NULL AND ts <= ? "
                "ORDER BY ts ASC LIMIT ?",
                (until_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_event_fired(self, event_id: int, at: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE events SET fired_at=? WHERE id=?", (at, event_id))

    def list_events(self, *,
                    since_ts: Optional[float] = None,
                    until_ts: Optional[float] = None,
                    only_pending: bool = False,
                    limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?"); params.append(until_ts)
        if only_pending:
            clauses.append("fired_at IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY ts ASC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_event(self, eid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM events WHERE id=?", (eid,))
            return cur.rowcount > 0
