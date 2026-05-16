"""memory_query tool — read-only query over ULTRON's memory.db.

Surfaces recent insight snapshots, app usage rollups, and salient
patterns logged by Module M (memory engine). Strictly SELECT — no
mutation. The DB is owned by ``ultron-memory-engine.exe``; we open it
read-only with ``mode=ro`` URI.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def _open_ro(db_path: Path) -> sqlite3.Connection:
    """Open the memory db read-only via URI mode."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def build(config: ToolsConfig) -> Tool:
    appdata = config.audit_log_path.parent  # …/ULTRON/data
    default_db = appdata / "memory.db"

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "recent_snapshots")).strip()
        limit = int(args.get("limit", 10))
        limit = max(1, min(limit, 500))

        if not default_db.exists():
            return {"kind": kind, "rows": [], "note": "memory.db not present yet"}

        conn = _open_ro(default_db)
        try:
            cur = conn.cursor()
            if kind == "recent_snapshots":
                cur.execute(
                    "SELECT ts_unix_ms, focus_app, tension, cognitive_load, phase "
                    "FROM insight_snapshots ORDER BY ts_unix_ms DESC LIMIT ?",
                    (limit,),
                )
            elif kind == "app_rollup":
                cur.execute(
                    "SELECT focus_app, COUNT(*) AS samples, AVG(tension) AS avg_tension "
                    "FROM insight_snapshots GROUP BY focus_app "
                    "ORDER BY samples DESC LIMIT ?",
                    (limit,),
                )
            elif kind == "patterns":
                cur.execute(
                    "SELECT kind, detail, score, last_seen_unix_ms "
                    "FROM patterns ORDER BY last_seen_unix_ms DESC LIMIT ?",
                    (limit,),
                )
            elif kind == "time_window":
                # "What was I doing between X and Y?" — returns the focus
                # app + tension snapshots covering the requested window,
                # plus a top-N rollup so the model can summarise without
                # quoting every row.
                since_ms = int(args.get("since_ts_unix_ms") or 0)
                until_ms = int(args.get("until_ts_unix_ms") or 0)
                if not since_ms or not until_ms or until_ms <= since_ms:
                    raise ValueError(
                        "time_window needs since_ts_unix_ms and until_ts_unix_ms"
                    )
                cur.execute(
                    "SELECT ts_unix_ms, focus_app, tension, cognitive_load, phase "
                    "FROM insight_snapshots "
                    "WHERE ts_unix_ms >= ? AND ts_unix_ms <= ? "
                    "ORDER BY ts_unix_ms ASC LIMIT ?",
                    (since_ms, until_ms, limit),
                )
                rows = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT focus_app, COUNT(*) AS samples "
                    "FROM insight_snapshots "
                    "WHERE ts_unix_ms >= ? AND ts_unix_ms <= ? "
                    "GROUP BY focus_app ORDER BY samples DESC LIMIT 10",
                    (since_ms, until_ms),
                )
                rollup = [dict(r) for r in cur.fetchall()]
                return {
                    "kind": kind,
                    "rows": rows,
                    "count": len(rows),
                    "rollup": rollup,
                    "since_ts_unix_ms": since_ms,
                    "until_ts_unix_ms": until_ms,
                }
            else:
                raise ValueError(
                    f"unknown kind {kind!r} — use "
                    "recent_snapshots|app_rollup|patterns|time_window"
                )
            rows = [dict(r) for r in cur.fetchall()]
            return {"kind": kind, "rows": rows, "count": len(rows)}
        except sqlite3.OperationalError as exc:
            return {"kind": kind, "rows": [], "note": f"db error: {exc}"}
        finally:
            conn.close()

    return Tool(
        name="memory_query",
        description=(
            "Read-only query over memory.db. kinds: "
            "recent_snapshots, app_rollup, patterns, "
            "time_window (focus history between two unix-ms timestamps)."
        ),
        category="memory",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["recent_snapshots", "app_rollup",
                             "patterns", "time_window"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "since_ts_unix_ms": {"type": "integer", "minimum": 0},
                "until_ts_unix_ms": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
