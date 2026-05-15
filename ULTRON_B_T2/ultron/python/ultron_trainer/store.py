"""SQLite persistence for Module TT (Trainer Twin).

Three tables — workouts, sleep_logs, body_metrics — plus schema_meta.
Streaks are not stored: they're derived on-the-fly in ``analytics.py``
so we never have to handle invalidation when an old row gets edited.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .config import TrainerConfig
from .models import BodyMetric, SleepLog, Workout

logger = logging.getLogger("ultron.trainer.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    exercise TEXT NOT NULL,
    sets INTEGER NOT NULL DEFAULT 1,
    reps INTEGER NOT NULL DEFAULT 0,
    weight_kg REAL NOT NULL DEFAULT 0,
    duration_secs INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_workouts_ts ON workouts(ts);
CREATE INDEX IF NOT EXISTS ix_workouts_exercise ON workouts(exercise);

CREATE TABLE IF NOT EXISTS sleep_logs (
    date TEXT PRIMARY KEY,
    bedtime_ts REAL NOT NULL,
    wake_ts REAL NOT NULL,
    quality INTEGER NOT NULL DEFAULT 3,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS body_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    weight_kg REAL,
    mood INTEGER,
    energy INTEGER,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_metrics_ts ON body_metrics(ts);
"""


class TrainerStore:
    def __init__(self, config: TrainerConfig) -> None:
        self._cfg = config
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
                (str(SCHEMA_VERSION),),
            )
        logger.info("trainer store ready at %s", self._path)

    # ── Workouts ────────────────────────────────────────────────────────

    def record_workout(self, w: Workout) -> int:
        if not w.exercise:
            raise ValueError("exercise is required")
        if w.sets < 0 or w.reps < 0 or w.weight_kg < 0 or w.duration_secs < 0:
            raise ValueError("workout numeric fields must be non-negative")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO workouts(ts,exercise,sets,reps,weight_kg,duration_secs,note)
                VALUES(?,?,?,?,?,?,?)
                """,
                (w.ts, w.exercise, w.sets, w.reps, w.weight_kg, w.duration_secs, w.note),
            )
            return int(cur.lastrowid or 0)

    def list_workouts(
        self,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        exercise: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?"); params.append(until_ts)
        if exercise:
            clauses.append("exercise = ?"); params.append(exercise)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM workouts {where} ORDER BY ts DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_workout(self, wid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM workouts WHERE id=?", (wid,))
            return cur.rowcount > 0

    # ── Sleep ───────────────────────────────────────────────────────────

    def record_sleep(self, s: SleepLog) -> None:
        if not s.date:
            raise ValueError("date is required (YYYY-MM-DD)")
        if s.wake_ts <= s.bedtime_ts:
            raise ValueError("wake_ts must be after bedtime_ts")
        q = max(1, min(5, int(s.quality)))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sleep_logs(date,bedtime_ts,wake_ts,quality,note)
                VALUES(?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    bedtime_ts=excluded.bedtime_ts,
                    wake_ts=excluded.wake_ts,
                    quality=excluded.quality,
                    note=excluded.note
                """,
                (s.date, s.bedtime_ts, s.wake_ts, q, s.note),
            )

    def list_sleep(
        self,
        *,
        since_date: Optional[str] = None,
        until_date: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_date:
            clauses.append("date >= ?"); params.append(since_date)
        if until_date:
            clauses.append("date <= ?"); params.append(until_date)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sleep_logs {where} ORDER BY date DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Body metrics ────────────────────────────────────────────────────

    def record_metric(self, m: BodyMetric) -> int:
        if m.weight_kg is None and m.mood is None and m.energy is None:
            raise ValueError("at least one metric (weight_kg/mood/energy) is required")
        if m.mood is not None and not 1 <= m.mood <= 5:
            raise ValueError("mood must be 1..5")
        if m.energy is not None and not 1 <= m.energy <= 5:
            raise ValueError("energy must be 1..5")
        if m.weight_kg is not None and m.weight_kg <= 0:
            raise ValueError("weight_kg must be > 0")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO body_metrics(ts,weight_kg,mood,energy,note)
                VALUES(?,?,?,?,?)
                """,
                (m.ts, m.weight_kg, m.mood, m.energy, m.note),
            )
            return int(cur.lastrowid or 0)

    def list_metrics(
        self,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?"); params.append(until_ts)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM body_metrics {where} ORDER BY ts DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]
