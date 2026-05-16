"""SQLite log of flow sessions."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import FlowConfig

logger = logging.getLogger("ultron.flow.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    duration_secs REAL NOT NULL,
    broken_by TEXT NOT NULL DEFAULT '',
    last_focus_app TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_flow_start ON flow_sessions(start_ts);
"""


class FlowStore:
    def __init__(self, config: FlowConfig) -> None:
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def record_session(
        self, *, start_ts: float, end_ts: float,
        broken_by: str = "", last_focus_app: str = "",
    ) -> int:
        duration = max(0.0, end_ts - start_ts)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO flow_sessions(start_ts,end_ts,duration_secs,"
                "broken_by,last_focus_app) VALUES(?,?,?,?,?)",
                (start_ts, end_ts, duration, broken_by, last_focus_app),
            )
            return int(cur.lastrowid or 0)

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM flow_sessions ORDER BY start_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, *, since_ts: Optional[float] = None) -> dict[str, Any]:
        since_ts = since_ts if since_ts is not None else (time.time() - 7 * 86400)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT duration_secs, broken_by FROM flow_sessions "
                "WHERE start_ts >= ?",
                (since_ts,),
            ).fetchall()
        if not rows:
            return {"sessions": 0, "total_minutes": 0.0,
                    "avg_minutes": 0.0, "longest_minutes": 0.0,
                    "top_breakers": []}
        durations = [float(r["duration_secs"]) for r in rows]
        breakers: dict[str, int] = {}
        for r in rows:
            b = (r["broken_by"] or "").strip() or "unknown"
            breakers[b] = breakers.get(b, 0) + 1
        top = sorted(breakers.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "since_ts": since_ts,
            "sessions": len(rows),
            "total_minutes": round(sum(durations) / 60.0, 1),
            "avg_minutes": round(sum(durations) / 60.0 / len(rows), 1),
            "longest_minutes": round(max(durations) / 60.0, 1),
            "top_breakers": [{"reason": k, "count": v} for k, v in top],
        }
