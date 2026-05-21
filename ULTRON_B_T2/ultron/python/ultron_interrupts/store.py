"""SQLite log for interrupt records + recovery times."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import InterruptConfig

logger = logging.getLogger("ultron.interrupts.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interrupts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    focus_app TEXT NOT NULL DEFAULT '',
    recovery_secs REAL,
    recovery_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_interrupts_ts ON interrupts(ts);
CREATE INDEX IF NOT EXISTS ix_interrupts_source ON interrupts(source);
"""


@dataclass
class Interrupt:
    """In-memory + persisted shape. ``id`` is populated by the store."""
    ts: float
    source: str
    detail: str = ""
    focus_app: str = ""
    recovery_secs: Optional[float] = None
    recovery_ts: Optional[float] = None
    id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "source": self.source,
            "detail": self.detail,
            "focus_app": self.focus_app,
            "recovery_secs": self.recovery_secs,
            "recovery_ts": self.recovery_ts,
        }


class InterruptStore:
    def __init__(self, config: InterruptConfig) -> None:
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, intr: Interrupt) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO interrupts(ts,source,detail,focus_app,recovery_secs,recovery_ts)"
                " VALUES(?,?,?,?,?,?)",
                (intr.ts, intr.source, intr.detail, intr.focus_app,
                 intr.recovery_secs, intr.recovery_ts),
            )
            intr.id = int(cur.lastrowid or 0)
            return intr.id

    def update_recovery(self, interrupt_id: int, recovery_secs: float,
                        recovery_ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE interrupts SET recovery_secs=?, recovery_ts=? WHERE id=?",
                (recovery_secs, recovery_ts, interrupt_id),
            )

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM interrupts ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, *, since_ts: Optional[float] = None) -> dict[str, Any]:
        since_ts = since_ts if since_ts is not None else (time.time() - 7 * 86400)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, recovery_secs, focus_app FROM interrupts WHERE ts >= ?",
                (since_ts,),
            ).fetchall()
        if not rows:
            return {
                "since_ts": since_ts,
                "count": 0,
                "by_source": [],
                "avg_recovery_secs": None,
                "longest_recovery_secs": None,
                "top_focus_apps": [],
            }
        by_source: dict[str, int] = {}
        by_focus: dict[str, int] = {}
        recoveries: list[float] = []
        for r in rows:
            src = (r["source"] or "").strip() or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
            app = (r["focus_app"] or "").strip()
            if app:
                by_focus[app] = by_focus.get(app, 0) + 1
            if r["recovery_secs"] is not None:
                recoveries.append(float(r["recovery_secs"]))
        return {
            "since_ts": since_ts,
            "count": len(rows),
            "by_source": [{"source": k, "count": v}
                          for k, v in sorted(by_source.items(),
                                             key=lambda kv: kv[1], reverse=True)],
            "avg_recovery_secs": (round(sum(recoveries) / len(recoveries), 1)
                                  if recoveries else None),
            "longest_recovery_secs": (round(max(recoveries), 1)
                                      if recoveries else None),
            "recovered_count": len(recoveries),
            "top_focus_apps": [{"app": k, "count": v}
                               for k, v in sorted(by_focus.items(),
                                                  key=lambda kv: kv[1],
                                                  reverse=True)[:5]],
        }

    def today(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """Stats since midnight local — useful for an end-of-day review."""
        now = now if now is not None else time.time()
        # Start-of-day in *server* local time.
        tm = time.localtime(now)
        midnight = time.mktime(time.struct_time((
            tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0,
            tm.tm_wday, tm.tm_yday, tm.tm_isdst,
        )))
        return self.stats(since_ts=midnight)
