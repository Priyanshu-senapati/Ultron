"""SQLite store for the Recall service.

Tables:
  - ``turns``        : every user / assistant utterance with its embedding.
  - ``reflections``  : LLM-generated session / day summaries (Phase 2).
  - ``facts``        : durable (subject, predicate, object) triples
                       extracted from turns (Phase 2).

Schemas are all created up-front so Phase 2 can start writing without
a migration step. Phase 1 only writes ``turns``.

Embeddings are raw float32 bytes via ``numpy.tobytes()``. Search loads
the matrix into memory on first query and reuses it; mutations
invalidate the cache so the next query reloads.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("ultron.recall.store")

EMBEDDING_DIM = 384  # mirror ultron_knowledge.embedder.EMBEDDING_DIM


_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    conv_id TEXT NOT NULL,
    embedding BLOB NOT NULL,
    indexed_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS ix_turns_conv ON turns(conv_id);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start_ts REAL NOT NULL,
    period_end_ts REAL NOT NULL,
    period_kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reflections_period ON reflections(period_kind, period_start_ts);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    source_turn_id INTEGER,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_ts REAL NOT NULL,
    UNIQUE(subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS ix_facts_subject ON facts(subject);
"""


@dataclass
class StoredTurn:
    id: int
    ts: float
    role: str
    content: str
    conv_id: str


@dataclass
class StoredReflection:
    id: int
    period_start_ts: float
    period_end_ts: float
    period_kind: str
    summary: str


class RecallStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        # In-memory caches.
        self._turn_matrix: Optional[np.ndarray] = None
        self._turns: list[StoredTurn] = []
        self._refl_matrix: Optional[np.ndarray] = None
        self._reflections: list[StoredReflection] = []

    # ── Schema ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ── Turns: write side ─────────────────────────────────────────────

    def insert_turn(self, *, ts: float, role: str, content: str,
                    conv_id: str, embedding: np.ndarray) -> int:
        if embedding.dtype != np.float32:
            embedding = embedding.astype(np.float32)
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO turns(ts,role,content,conv_id,embedding,indexed_ts) "
                "VALUES(?,?,?,?,?,?)",
                (ts, role, content, conv_id, embedding.tobytes(), now),
            )
            conn.commit()
        self._turn_matrix = None  # invalidate cache
        return int(cur.lastrowid or 0)

    def insert_turns_bulk(self, rows: list[dict[str, Any]]) -> list[int]:
        """Batch insert. Each row: {ts, role, content, conv_id, embedding(np.ndarray)}."""
        if not rows:
            return []
        now = time.time()
        ids: list[int] = []
        with self._connect() as conn:
            for r in rows:
                emb: np.ndarray = r["embedding"]
                if emb.dtype != np.float32:
                    emb = emb.astype(np.float32)
                cur = conn.execute(
                    "INSERT INTO turns(ts,role,content,conv_id,embedding,indexed_ts) "
                    "VALUES(?,?,?,?,?,?)",
                    (r["ts"], r["role"], r["content"], r["conv_id"],
                     emb.tobytes(), now),
                )
                ids.append(int(cur.lastrowid or 0))
            conn.commit()
        self._turn_matrix = None
        return ids

    # ── Reflections (Phase 2) ─────────────────────────────────────────

    def insert_reflection(self, *, period_start_ts: float, period_end_ts: float,
                          period_kind: str, summary: str,
                          embedding: np.ndarray) -> int:
        if embedding.dtype != np.float32:
            embedding = embedding.astype(np.float32)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reflections"
                "(period_start_ts,period_end_ts,period_kind,summary,embedding,created_ts) "
                "VALUES(?,?,?,?,?,?)",
                (period_start_ts, period_end_ts, period_kind, summary,
                 embedding.tobytes(), time.time()),
            )
            conn.commit()
        self._refl_matrix = None
        return int(cur.lastrowid or 0)

    # ── Facts (Phase 2) ───────────────────────────────────────────────

    def insert_fact(self, *, subject: str, predicate: str, object_: str,
                    source_turn_id: Optional[int] = None,
                    confidence: float = 1.0) -> Optional[int]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO facts(subject,predicate,object,source_turn_id,confidence,created_ts) "
                    "VALUES(?,?,?,?,?,?)",
                    (subject.strip(), predicate.strip(), object_.strip(),
                     source_turn_id, float(confidence), time.time()),
                )
                conn.commit()
                return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            # Duplicate (subject,predicate,object) — silently skip.
            return None

    def all_facts(self, *, subject: Optional[str] = None,
                  limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        with self._connect() as conn:
            if subject:
                rows = conn.execute(
                    "SELECT * FROM facts WHERE subject = ? "
                    "ORDER BY created_ts DESC LIMIT ?",
                    (subject, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM facts ORDER BY created_ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── Read side ─────────────────────────────────────────────────────

    def _load_turn_cache(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,ts,role,content,conv_id,embedding FROM turns"
            ).fetchall()
        if not rows:
            self._turn_matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
            self._turns = []
            return
        mats: list[np.ndarray] = []
        items: list[StoredTurn] = []
        for r in rows:
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            if emb.size != EMBEDDING_DIM:
                continue
            mats.append(emb)
            items.append(StoredTurn(
                id=int(r["id"]), ts=float(r["ts"]), role=str(r["role"]),
                content=str(r["content"]), conv_id=str(r["conv_id"]),
            ))
        self._turn_matrix = np.stack(mats, axis=0) if mats else np.zeros(
            (0, EMBEDDING_DIM), dtype=np.float32,
        )
        self._turns = items
        logger.info("recall turn cache loaded: %d turns", len(self._turns))

    def _load_reflection_cache(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,period_start_ts,period_end_ts,period_kind,summary,embedding"
                " FROM reflections"
            ).fetchall()
        if not rows:
            self._refl_matrix = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
            self._reflections = []
            return
        mats: list[np.ndarray] = []
        items: list[StoredReflection] = []
        for r in rows:
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            if emb.size != EMBEDDING_DIM:
                continue
            mats.append(emb)
            items.append(StoredReflection(
                id=int(r["id"]),
                period_start_ts=float(r["period_start_ts"]),
                period_end_ts=float(r["period_end_ts"]),
                period_kind=str(r["period_kind"]),
                summary=str(r["summary"]),
            ))
        self._refl_matrix = np.stack(mats, axis=0) if mats else np.zeros(
            (0, EMBEDDING_DIM), dtype=np.float32,
        )
        self._reflections = items

    def search_turns(self, query_emb: np.ndarray, *, top_k: int,
                     min_score: float) -> list[tuple[StoredTurn, float]]:
        if self._turn_matrix is None:
            self._load_turn_cache()
        if self._turn_matrix is None or self._turn_matrix.shape[0] == 0:
            return []
        q = query_emb.astype(np.float32)
        if q.ndim == 2:
            q = q[0]
        scores = self._turn_matrix @ q
        if top_k >= len(scores):
            order = np.argsort(-scores)
        else:
            idx = np.argpartition(-scores, top_k)[:top_k]
            order = idx[np.argsort(-scores[idx])]
        out: list[tuple[StoredTurn, float]] = []
        for i in order:
            sc = float(scores[i])
            if sc < min_score:
                break
            out.append((self._turns[i], sc))
        return out

    def search_reflections(self, query_emb: np.ndarray, *, top_k: int,
                           min_score: float) -> list[tuple[StoredReflection, float]]:
        if self._refl_matrix is None:
            self._load_reflection_cache()
        if self._refl_matrix is None or self._refl_matrix.shape[0] == 0:
            return []
        q = query_emb.astype(np.float32)
        if q.ndim == 2:
            q = q[0]
        scores = self._refl_matrix @ q
        if top_k >= len(scores):
            order = np.argsort(-scores)
        else:
            idx = np.argpartition(-scores, top_k)[:top_k]
            order = idx[np.argsort(-scores[idx])]
        out: list[tuple[StoredReflection, float]] = []
        for i in order:
            sc = float(scores[i])
            if sc < min_score:
                break
            out.append((self._reflections[i], sc))
        return out

    def turns_around(self, turn_id: int, window: int) -> list[StoredTurn]:
        """Return turns from the same conversation neighbouring ``turn_id``.

        Used by the retriever to give the LLM enough context to interpret
        a hit. ``window`` is the number of turns on EACH side.
        """
        if window <= 0:
            return []
        with self._connect() as conn:
            ref = conn.execute(
                "SELECT conv_id,ts FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if not ref:
                return []
            conv_id = ref["conv_id"]
            ts = float(ref["ts"])
            rows_before = conn.execute(
                "SELECT id,ts,role,content,conv_id FROM turns "
                "WHERE conv_id = ? AND ts < ? ORDER BY ts DESC LIMIT ?",
                (conv_id, ts, window),
            ).fetchall()
            rows_after = conn.execute(
                "SELECT id,ts,role,content,conv_id FROM turns "
                "WHERE conv_id = ? AND ts > ? ORDER BY ts ASC LIMIT ?",
                (conv_id, ts, window),
            ).fetchall()
        before = [StoredTurn(id=int(r["id"]), ts=float(r["ts"]),
                             role=str(r["role"]), content=str(r["content"]),
                             conv_id=str(r["conv_id"])) for r in rows_before]
        after = [StoredTurn(id=int(r["id"]), ts=float(r["ts"]),
                            role=str(r["role"]), content=str(r["content"]),
                            conv_id=str(r["conv_id"])) for r in rows_after]
        # before is descending by ts — reverse so we return chronological.
        before.reverse()
        return before + after

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            t = conn.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"]
            r = conn.execute("SELECT COUNT(*) AS c FROM reflections").fetchone()["c"]
            f = conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"]
        return {"turns": int(t), "reflections": int(r), "facts": int(f)}

    def recent_turns(self, *, limit: int = 50,
                     conv_id: Optional[str] = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._connect() as conn:
            if conv_id:
                rows = conn.execute(
                    "SELECT id,ts,role,content,conv_id FROM turns "
                    "WHERE conv_id = ? ORDER BY ts DESC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,ts,role,content,conv_id FROM turns "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def reload(self) -> None:
        self._turn_matrix = None
        self._refl_matrix = None
