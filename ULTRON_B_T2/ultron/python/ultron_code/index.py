"""SQLite-backed code index.

Schema::

    files (
      path TEXT PRIMARY KEY,   -- absolute path
      language TEXT,
      size INTEGER,
      mtime REAL,
      indexed_at_unix REAL
    )

    symbols (
      id INTEGER PRIMARY KEY,
      path TEXT,
      name TEXT,
      kind TEXT,
      line INTEGER,
      end_line INTEGER,
      signature TEXT,
      parent TEXT
    )
    INDEX symbols_name_idx ON symbols(name)
    INDEX symbols_path_idx ON symbols(path)

The index is rebuilt incrementally: rows in ``files`` whose ``mtime``
is unchanged are skipped. Removed files are pruned.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import CodeIntelConfig
from .parser import Symbol, extract
from .scanner import ScannedFile, iter_source_files

logger = logging.getLogger("ultron.code.index")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed_at_unix REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    parent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_path_idx ON symbols(path);
CREATE INDEX IF NOT EXISTS files_lang_idx ON files(language);
"""


@dataclass
class IndexStats:
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    pruned: int = 0
    symbols: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class CodeIndex:
    def __init__(self, config: CodeIntelConfig) -> None:
        self._cfg = config
        self._db_path: Path = config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Indexing ────────────────────────────────────────────────────────

    def rebuild(self, full: bool = False) -> IndexStats:
        """Re-scan all roots. If ``full`` truncates first."""
        t0 = time.monotonic()
        stats = IndexStats()
        if full:
            with self._conn:
                self._conn.execute("DELETE FROM symbols")
                self._conn.execute("DELETE FROM files")

        cur = self._conn.cursor()
        existing: dict[str, float] = {
            r["path"]: r["mtime"] for r in cur.execute("SELECT path, mtime FROM files")
        }
        seen_paths: set[str] = set()

        for scanned in iter_source_files(self._cfg):
            stats.scanned += 1
            pkey = str(scanned.path)
            seen_paths.add(pkey)
            prev_mtime = existing.get(pkey)
            if prev_mtime is not None and abs(prev_mtime - scanned.mtime) < 1e-3:
                continue   # unchanged

            try:
                source = scanned.path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("read failed for %s: %s", scanned.path, exc)
                continue
            syms = extract(source, scanned.language)
            stats.symbols += len(syms)
            with self._conn:
                self._conn.execute(
                    "INSERT INTO files (path, language, size, mtime, indexed_at_unix) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "language=excluded.language, size=excluded.size, mtime=excluded.mtime, "
                    "indexed_at_unix=excluded.indexed_at_unix",
                    (pkey, scanned.language, scanned.size, scanned.mtime, time.time()),
                )
                self._conn.execute("DELETE FROM symbols WHERE path = ?", (pkey,))
                if syms:
                    self._conn.executemany(
                        "INSERT INTO symbols (path, name, kind, line, end_line, signature, parent) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(pkey, s.name, s.kind, s.line, s.end_line, s.signature, s.parent) for s in syms],
                    )
            if prev_mtime is None:
                stats.inserted += 1
            else:
                stats.updated += 1

        # Prune deleted files.
        for path in list(existing.keys()):
            if path not in seen_paths:
                with self._conn:
                    self._conn.execute("DELETE FROM symbols WHERE path = ?", (path,))
                    self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
                stats.pruned += 1

        stats.elapsed_seconds = round(time.monotonic() - t0, 3)
        return stats

    # ── Queries ─────────────────────────────────────────────────────────

    def find_symbol(
        self,
        name: str,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Exact-name lookup. Case-sensitive (matches the source)."""
        sql = "SELECT * FROM symbols WHERE name = ?"
        params: list = [name]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY path, line LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params)]

    def search_symbols(self, like: str, limit: int = 50) -> list[dict]:
        """Substring (LIKE) search over symbol names."""
        params = [f"%{like}%", int(limit)]
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? "
                "ORDER BY path, line LIMIT ?",
                params,
            )
        ]

    def list_files(
        self,
        language: Optional[str] = None,
        path_substring: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM files WHERE 1=1"
        params: list = []
        if language:
            sql += " AND language = ?"
            params.append(language)
        if path_substring:
            sql += " AND path LIKE ?"
            params.append(f"%{path_substring}%")
        sql += " ORDER BY path LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params)]

    def stats(self) -> dict:
        files = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        symbols = self._conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        langs = [
            dict(r) for r in self._conn.execute(
                "SELECT language, COUNT(*) AS files FROM files GROUP BY language ORDER BY files DESC"
            )
        ]
        return {"files": files, "symbols": symbols, "languages": langs}
