"""KnowledgeRetriever — query → top-k chunks for prompt injection.

Module C's ContextAssembler calls `retrieve()` on every user turn and
splices the result into a `[RELEVANT KNOWLEDGE]` block.

Failure mode: if embeddings library isn't available or the index is
empty, `retrieve()` returns an empty list — the LLM just sees the
prompt without retrieved knowledge. Never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .embedder import Embedder
from .store import KnowledgeStore

logger = logging.getLogger("ultron.knowledge.retriever")


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    heading_path: str
    file_path: str
    score: float


class KnowledgeRetriever:
    def __init__(
        self,
        db_path: Path,
        embedder: Optional[Embedder] = None,
        top_k: int = 3,
        min_score: float = 0.30,
    ) -> None:
        self.db_path = db_path
        self.top_k = top_k
        self.min_score = min_score
        self._embedder = embedder or Embedder()
        self._store: Optional[KnowledgeStore] = None
        # Track DB mtime so we auto-reload after the indexer writes.
        self._last_db_mtime: float = 0.0

    def _ensure_store(self) -> Optional[KnowledgeStore]:
        if not self.db_path.exists():
            return None
        if self._store is None:
            self._store = KnowledgeStore(self.db_path)
            self._last_db_mtime = self.db_path.stat().st_mtime
            return self._store
        # Auto-reload if the DB has been touched.
        try:
            mtime = self.db_path.stat().st_mtime
            if mtime > self._last_db_mtime + 0.5:
                self._store.reload()
                self._last_db_mtime = mtime
        except OSError:
            pass
        return self._store

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        store = self._ensure_store()
        if store is None or not query.strip():
            return []
        try:
            q_emb = self._embedder.encode_one(query)
        except RuntimeError as exc:
            logger.debug("embedder unavailable: %s", exc)
            return []
        try:
            hits = store.search(q_emb, top_k=self.top_k, min_score=self.min_score)
        except Exception as exc:  # noqa: BLE001
            logger.debug("retrieval failed: %s", exc)
            return []
        out: list[RetrievedChunk] = []
        for chunk, score in hits:
            out.append(
                RetrievedChunk(
                    text=chunk.chunk_text,
                    heading_path=chunk.heading_path,
                    file_path=chunk.file_path,
                    score=score,
                )
            )
        return out
