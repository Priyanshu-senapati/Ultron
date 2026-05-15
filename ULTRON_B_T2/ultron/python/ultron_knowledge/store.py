"""SQLite-backed knowledge store with in-memory cosine search.

Schema:
    chunks(
        id INTEGER PRIMARY KEY,
        file_path TEXT,
        file_mtime REAL,
        chunk_idx INTEGER,
        chunk_text TEXT,
        heading_path TEXT,
        embedding BLOB
    )

Embeddings are stored as raw float32 bytes (numpy `.tobytes()`). At
search time the whole table is loaded into a numpy matrix once;
subsequent queries are a single matmul against ~hundreds of rows
(microseconds). Re-load when the DB mtime changes.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .embedder import EMBEDDING_DIM

logger = logging.getLogger("ultron.knowledge.store")


@dataclass
class StoredChunk:
    id: int
    file_path: str
    chunk_idx: int
    chunk_text: str
    heading_path: str


class KnowledgeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # In-memory cache for search.
        self._matrix: Optional[np.ndarray] = None  # (n, dim)
        self._chunks: list[StoredChunk] = []
        self._last_loaded_ts: float = 0.0

    # ── Schema ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_mtime REAL NOT NULL,
                    chunk_idx INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    heading_path TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    UNIQUE(file_path, chunk_idx)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)"
            )
            conn.commit()

    # ── Indexer-side mutation ─────────────────────────────────────────────

    def file_mtime(self, file_path: str) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_mtime FROM chunks WHERE file_path = ? LIMIT 1",
                (file_path,),
            ).fetchone()
        return row[0] if row else None

    def replace_file(
        self,
        file_path: str,
        file_mtime: float,
        chunks: list[tuple[str, str, np.ndarray]],
    ) -> None:
        """Delete any prior chunks for this file, then insert fresh ones."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
            for idx, (text, heading, emb) in enumerate(chunks):
                if emb.dtype != np.float32:
                    emb = emb.astype(np.float32)
                conn.execute(
                    "INSERT INTO chunks (file_path, file_mtime, chunk_idx, chunk_text, heading_path, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_path, file_mtime, idx, text, heading, emb.tobytes()),
                )
            conn.commit()
        # Invalidate in-memory cache so next query reloads.
        self._matrix = None

    def delete_file(self, file_path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
            conn.commit()
        self._matrix = None

    def all_indexed_files(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT file_path FROM chunks").fetchall()
        return {r[0] for r in rows}

    # ── Retrieval ─────────────────────────────────────────────────────────

    def _load_cache(self) -> None:
        """Load all rows into memory for fast cosine search."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, file_path, chunk_idx, chunk_text, heading_path, embedding FROM chunks"
            ).fetchall()
        if not rows:
            self._matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
            self._chunks = []
        else:
            embeddings: list[np.ndarray] = []
            chunks: list[StoredChunk] = []
            for row in rows:
                emb = np.frombuffer(row[5], dtype=np.float32)
                if emb.size != EMBEDDING_DIM:
                    continue
                embeddings.append(emb)
                chunks.append(
                    StoredChunk(
                        id=row[0],
                        file_path=row[1],
                        chunk_idx=row[2],
                        chunk_text=row[3],
                        heading_path=row[4],
                    )
                )
            self._matrix = np.stack(embeddings, axis=0)
            self._chunks = chunks
        self._last_loaded_ts = time.monotonic()
        logger.info("knowledge store loaded: %d chunks", len(self._chunks))

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 3,
        min_score: float = 0.25,
    ) -> list[tuple[StoredChunk, float]]:
        """Return top_k (chunk, cosine_score) pairs above min_score."""
        if self._matrix is None:
            self._load_cache()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        # Both query and matrix rows are L2-normalised, so dot = cosine.
        q = query_embedding.astype(np.float32)
        if q.ndim == 2:
            q = q[0]
        scores = self._matrix @ q
        # Top-k indices, descending.
        if top_k >= len(scores):
            order = np.argsort(-scores)
        else:
            top_idx = np.argpartition(-scores, top_k)[:top_k]
            order = top_idx[np.argsort(-scores[top_idx])]
        results: list[tuple[StoredChunk, float]] = []
        for idx in order:
            score = float(scores[idx])
            if score < min_score:
                break
            results.append((self._chunks[idx], score))
        return results

    def reload(self) -> None:
        """Force re-load from disk on next search."""
        self._matrix = None
