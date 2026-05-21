"""SQLite log of computed readiness snapshots."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .calculator import ReadinessScore
from .config import ReadinessConfig

logger = logging.getLogger("ultron.readiness.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readiness_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    total REAL NOT NULL,
    bucket TEXT NOT NULL,
    components_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_readiness_ts ON readiness_snapshots(ts);
"""


class ReadinessStore:
    def __init__(self, config: ReadinessConfig) -> None:
        self._path = Path(config.db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, score: ReadinessScore) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO readiness_snapshots"
                "(ts,total,bucket,components_json,inputs_json) VALUES(?,?,?,?,?)",
                (
                    score.computed_ts,
                    score.total,
                    score.bucket,
                    json.dumps([c.as_dict() for c in score.components]),
                    json.dumps(score.inputs),
                ),
            )
            return int(cur.lastrowid or 0)

    def recent(self, *, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM readiness_snapshots ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "total": r["total"],
                "bucket": r["bucket"],
                "components": json.loads(r["components_json"]),
                "inputs": json.loads(r["inputs_json"]),
            })
        return out

    def latest(self) -> Optional[dict[str, Any]]:
        rows = self.recent(limit=1)
        return rows[0] if rows else None
