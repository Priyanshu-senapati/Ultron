"""SQLite persistence for Module K (Knowledge Graph)."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .config import EDGE_KINDS, ENTITY_KINDS, KnowledgeGraphConfig

logger = logging.getLogger("ultron.kg.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(kind, name)
);
CREATE INDEX IF NOT EXISTS ix_entities_kind ON entities(kind);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(src_id, dst_id, kind),
    FOREIGN KEY (src_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (dst_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS ix_edges_kind ON edges(kind);
"""


class KGStore:
    def __init__(self, config: KnowledgeGraphConfig) -> None:
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
        logger.info("kg store ready at %s", self._path)

    # ── Entities ───────────────────────────────────────────────────────

    def upsert_entity(self, *, kind: str, name: str,
                      attrs: Optional[dict[str, Any]] = None) -> int:
        kind = (kind or "").strip()
        name = (name or "").strip()
        if not kind or not name:
            raise ValueError("kind and name are required")
        if kind not in ENTITY_KINDS:
            logger.warning("non-canonical entity kind %r — storing anyway", kind)
        attrs_json = json.dumps(attrs or {}, ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO entities(kind,name,attrs,created_at) VALUES(?,?,?,?)
                ON CONFLICT(kind,name) DO UPDATE SET attrs=excluded.attrs
                RETURNING id
                """,
                (kind, name, attrs_json, time.time()),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_entity(self, eid: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
            return _row_to_entity(r) if r else None

    def find_entity(self, *, kind: Optional[str], name: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            if kind:
                r = conn.execute(
                    "SELECT * FROM entities WHERE kind=? AND name=?",
                    (kind, name),
                ).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM entities WHERE name=? ORDER BY id ASC LIMIT 1",
                    (name,),
                ).fetchone()
            return _row_to_entity(r) if r else None

    def search_entities(self, *, like: str, kind: Optional[str] = None,
                        limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self._cfg.max_query_rows))
        like_pat = f"%{like}%"
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM entities WHERE name LIKE ? AND kind=? "
                    "ORDER BY name LIMIT ?",
                    (like_pat, kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM entities WHERE name LIKE ? ORDER BY name LIMIT ?",
                    (like_pat, limit),
                ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def delete_entity(self, eid: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM entities WHERE id=?", (eid,))
            return cur.rowcount > 0

    # ── Edges ──────────────────────────────────────────────────────────

    def upsert_edge(self, *, src_id: int, dst_id: int, kind: str,
                    attrs: Optional[dict[str, Any]] = None) -> int:
        if src_id == dst_id:
            raise ValueError("self-loops not allowed")
        kind = (kind or "").strip()
        if not kind:
            raise ValueError("edge kind is required")
        if kind not in EDGE_KINDS:
            logger.warning("non-canonical edge kind %r — storing anyway", kind)
        with self._connect() as conn:
            for col, val in (("src_id", src_id), ("dst_id", dst_id)):
                if not conn.execute(
                    "SELECT 1 FROM entities WHERE id=?", (val,)
                ).fetchone():
                    raise ValueError(f"{col}={val} does not exist")
            cur = conn.execute(
                """
                INSERT INTO edges(src_id,dst_id,kind,attrs,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(src_id,dst_id,kind) DO UPDATE SET attrs=excluded.attrs
                RETURNING id
                """,
                (src_id, dst_id, kind,
                 json.dumps(attrs or {}, ensure_ascii=False), time.time()),
            )
            row = cur.fetchone()
            return int(row["id"])

    def list_edges(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM edges").fetchall()
        return [_row_to_edge(r) for r in rows]

    def list_entities(self, *, kind: Optional[str] = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self._cfg.max_query_rows))
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM entities WHERE kind=? ORDER BY name LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM entities ORDER BY kind, name LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def delete_edge(self, edge_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM edges WHERE id=?", (edge_id,))
            return cur.rowcount > 0


# ── Row → dict helpers ────────────────────────────────────────────────


def _row_to_entity(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "kind": r["kind"],
        "name": r["name"],
        "attrs": _safe_json(r["attrs"]),
        "created_at": float(r["created_at"]),
    }


def _row_to_edge(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "src_id": int(r["src_id"]),
        "dst_id": int(r["dst_id"]),
        "kind": r["kind"],
        "attrs": _safe_json(r["attrs"]),
        "created_at": float(r["created_at"]),
    }


def _safe_json(s: str | None) -> dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}
