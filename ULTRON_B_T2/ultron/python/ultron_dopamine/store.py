"""SQLite persistence for Module Y."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_PATTERNS, DopamineConfig

logger = logging.getLogger("ultron.dopamine.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    name TEXT PRIMARY KEY,
    substring TEXT NOT NULL,
    weight INTEGER NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pattern TEXT NOT NULL,
    weight INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_marks_ts ON marks(ts);
CREATE INDEX IF NOT EXISTS ix_marks_pattern ON marks(pattern);
CREATE INDEX IF NOT EXISTS ix_marks_kind ON marks(kind);
"""


class DopamineStore:
    def __init__(self, config: DopamineConfig) -> None:
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
            # Seed defaults — won't overwrite user-edited rows.
            for name, sub, w, k in DEFAULT_PATTERNS:
                conn.execute(
                    "INSERT OR IGNORE INTO patterns(name,substring,weight,kind) "
                    "VALUES(?,?,?,?)",
                    (name, sub, w, k),
                )
        logger.info("dopamine store ready at %s", self._path)

    # ── Patterns ───────────────────────────────────────────────────────

    def list_patterns(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name,substring,weight,kind FROM patterns ORDER BY weight DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_pattern(self, *, name: str, substring: str,
                       weight: int, kind: str) -> None:
        if not name or not substring:
            raise ValueError("name and substring are required")
        if kind not in ("rewarding", "wasteful", "neutral"):
            raise ValueError(f"bad kind {kind!r}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO patterns(name,substring,weight,kind) VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    substring=excluded.substring,
                    weight=excluded.weight,
                    kind=excluded.kind
                """,
                (name, substring, int(weight), kind),
            )

    def delete_pattern(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM patterns WHERE name=?", (name,))
            return cur.rowcount > 0

    # ── Marks ──────────────────────────────────────────────────────────

    def record_mark(self, *, ts: float, pattern: str, weight: int,
                    kind: str, source: str = "", context: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO marks(ts,pattern,weight,kind,source,context)
                VALUES(?,?,?,?,?,?)
                """,
                (ts, pattern, int(weight), kind, source, context),
            )
            return int(cur.lastrowid or 0)

    def list_marks(self, *,
                   since_ts: Optional[float] = None,
                   until_ts: Optional[float] = None,
                   kind: Optional[str] = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?"); params.append(until_ts)
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(limit, self._cfg.max_query_rows))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM marks {where} ORDER BY ts DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    def rollup_by_pattern(self, *, since_ts: float, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self._cfg.max_query_rows))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pattern, kind, COUNT(*) AS hits, SUM(weight) AS total_weight
                FROM marks WHERE ts >= ?
                GROUP BY pattern, kind
                ORDER BY ABS(total_weight) DESC
                LIMIT ?
                """,
                (since_ts, limit),
            ).fetchall()
        return [dict(r) for r in rows]
