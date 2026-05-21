"""RecallService — long-term memory across conversations.

Phase 1 (this file):
  - Indexes every ``voice_transcript`` (user) and ``llm_response``
    (assistant) into a SQLite + numpy vector store.
  - Answers ``recall_query_request`` with semantic top-K matches over
    all past turns + (when present) reflections + (substring-matched)
    facts.

Phase 2 (later): fact extractor + reflection composer write into the
already-provisioned reflections / facts tables.

Subscribes:
  - ``voice_transcript``       → index as role='user'
  - ``llm_response``           → index as role='assistant'
  - ``recall_query_request``   → answer with a RecallBundle
  - ``recall_index_request``   → manual index (used by tests / tools)

Publishes:
  - ``recall_indexed``         → fires per batch with the new turn ids
  - ``recall_query_result``    → answer to a query
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ultron_bridge import UltronBridge
from ultron_knowledge.embedder import Embedder

from .config import RecallConfig
from .extractor import FactExtractor
from .reflector import Reflector
from .retriever import RecallRetriever
from .store import RecallStore

logger = logging.getLogger("ultron.recall.service")


@dataclass
class _PendingTurn:
    ts: float
    role: str
    content: str
    conv_id: str


class RecallService:
    def __init__(self, config: RecallConfig) -> None:
        self._cfg = config
        self._store = RecallStore(config.db_path)
        self._embedder = Embedder(model_name=config.embedding_model)
        self._retriever = RecallRetriever(self._store, config)
        self._bridge: Optional[UltronBridge] = None
        # Each service boot opens a new conversation id; every turn this
        # session is tagged with it.
        self._conv_id: str = uuid.uuid4().hex[:12]
        self._pending: list[_PendingTurn] = []
        self._lock = asyncio.Lock()
        self._flusher_task: Optional[asyncio.Task] = None
        # Phase 2 background workers. Constructed lazily on first need
        # so unit tests that don't touch Ollama don't pay the import.
        self._extractor: Optional[FactExtractor] = None
        self._reflector: Optional[Reflector] = None
        self._extractor_task: Optional[asyncio.Task] = None
        self._reflector_task: Optional[asyncio.Task] = None

    @property
    def store(self) -> RecallStore:
        return self._store

    @property
    def conv_id(self) -> str:
        return self._conv_id

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ── Indexing ──────────────────────────────────────────────────────

    def _normalise(self, content: str) -> str:
        # Strip control chars and collapse whitespace.
        text = " ".join(content.split())
        if len(text) > self._cfg.max_indexed_chars:
            text = text[: self._cfg.max_indexed_chars].rstrip() + "…"
        return text

    def queue_turn(self, *, role: str, content: str,
                   ts: Optional[float] = None,
                   conv_id: Optional[str] = None) -> bool:
        text = self._normalise(content)
        if len(text) < self._cfg.min_content_chars:
            return False
        ts = ts if ts is not None else time.time()
        self._pending.append(_PendingTurn(
            ts=ts, role=role, content=text,
            conv_id=conv_id or self._conv_id,
        ))
        return True

    async def flush_pending(self) -> list[int]:
        """Embed + persist any queued turns. Returns the inserted ids."""
        if not self._pending:
            return []
        async with self._lock:
            batch, self._pending = self._pending[:], []
        # Embed off the loop — sentence-transformers is CPU-bound.
        loop = asyncio.get_running_loop()
        texts = [t.content for t in batch]
        try:
            embeddings: np.ndarray = await loop.run_in_executor(
                None, lambda: self._embedder.encode(texts)
            )
        except Exception:  # noqa: BLE001
            logger.exception("embedding failed; re-queueing batch")
            async with self._lock:
                self._pending = batch + self._pending
            return []
        rows: list[dict[str, Any]] = []
        for t, emb in zip(batch, embeddings):
            rows.append({
                "ts": t.ts, "role": t.role, "content": t.content,
                "conv_id": t.conv_id, "embedding": emb,
            })
        ids = await loop.run_in_executor(
            None, lambda: self._store.insert_turns_bulk(rows)
        )
        if self._bridge is not None and ids:
            try:
                await self._bridge.publish("recall_indexed", {
                    "ids": ids,
                    "count": len(ids),
                    "conv_id": self._conv_id,
                    "ts": time.time(),
                })
            except Exception:  # noqa: BLE001
                logger.exception("recall_indexed publish failed")
        logger.info("recall: indexed %d turns (conv=%s, pending=%d)",
                    len(ids), self._conv_id, len(self._pending))
        return ids

    # ── Query ─────────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "search"))
        loop = asyncio.get_running_loop()
        if kind == "search":
            q = str(payload.get("query") or "").strip()
            if not q:
                result = {"kind": kind, "error": "empty query"}
            else:
                top_k = payload.get("top_k")
                top_k = int(top_k) if top_k is not None else None
                include_reflections = bool(payload.get("include_reflections", True))
                include_facts = bool(payload.get("include_facts", True))
                since_ts = payload.get("since_ts")
                since_ts = float(since_ts) if since_ts is not None else None
                # Flush so a just-spoken turn is included.
                await self.flush_pending()
                emb = await loop.run_in_executor(
                    None, lambda: self._embedder.encode_one(q)
                )
                bundle = self._retriever.search(
                    q, emb, top_k=top_k,
                    include_reflections=include_reflections,
                    include_facts=include_facts,
                    since_ts=since_ts,
                )
                prompt_block = self._retriever.format_for_prompt(bundle)
                result = {
                    "kind": kind,
                    "bundle": bundle.as_dict(),
                    "prompt_block": prompt_block,
                }
        elif kind == "counts":
            counts = await loop.run_in_executor(None, lambda: self._store.counts())
            result = {"kind": kind, "counts": counts,
                      "conv_id": self._conv_id,
                      "pending": self.pending_count}
        elif kind == "recent":
            limit = int(payload.get("limit", 50))
            conv_id = payload.get("conv_id")
            rows = await loop.run_in_executor(
                None, lambda: self._store.recent_turns(limit=limit, conv_id=conv_id)
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "flush":
            ids = await self.flush_pending()
            result = {"kind": kind, "indexed_ids": ids, "count": len(ids)}
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            try:
                await self._bridge.publish("recall_query_result", result)
            except Exception:  # noqa: BLE001
                logger.exception("recall_query_result publish failed")
        return result

    # ── Event handler ─────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "voice_transcript":
                text = str(payload.get("text") or "").strip()
                if text:
                    self.queue_turn(role="user", content=text)
            elif kind == "llm_response":
                text = str(payload.get("text") or "").strip()
                if text and not payload.get("error"):
                    self.queue_turn(role="assistant", content=text)
            elif kind == "recall_query_request":
                asyncio.create_task(self.query(payload))
            elif kind == "recall_index_request":
                role = str(payload.get("role") or "user")
                text = str(payload.get("content") or "")
                if text and self.queue_turn(role=role, content=text):
                    await self.flush_pending()
            elif kind == "recall_extract_request":
                asyncio.create_task(self._run_extract_pass())
            elif kind == "recall_reflect_request":
                period = str(payload.get("period") or "session")
                asyncio.create_task(self._run_reflect(period=period,
                                                      payload=payload))
            elif kind == "voice_shutdown_initiated":
                # Reflect the current session before the stack tears down.
                # Best-effort: if the LLM is slow we may miss it but the
                # heartbeat reflector will eventually catch up.
                if self._cfg.enable_reflections:
                    asyncio.create_task(self._run_reflect(period="session",
                                                          payload={}))
        except Exception:  # noqa: BLE001
            logger.exception("recall handler failed for kind=%s", kind)

    # ── Phase 2: extractor + reflector orchestration ──────────────────

    def _ollama_url(self) -> str:
        return os.environ.get("OLLAMA_URL", "http://localhost:11434")

    def _ollama_model(self) -> str:
        # Read the LLM module's config so we use the same model the rest
        # of the stack is using. Fall back to the small fast default.
        try:
            from ultron_llm.config import load_config as _load_llm
            return _load_llm().ollama_model
        except Exception:  # noqa: BLE001
            return os.environ.get("ULTRON_OLLAMA_MODEL", "llama3.2:3b")

    def _ensure_extractor(self) -> FactExtractor:
        if self._extractor is None:
            self._extractor = FactExtractor(
                self._store,
                ollama_url=self._ollama_url(),
                ollama_model=self._ollama_model(),
            )
        return self._extractor

    def _ensure_reflector(self) -> Reflector:
        if self._reflector is None:
            self._reflector = Reflector(
                self._store,
                ollama_url=self._ollama_url(),
                ollama_model=self._ollama_model(),
                embedder=self._embedder,
                max_chars=self._cfg.reflection_chars,
            )
        return self._reflector

    async def _run_extract_pass(self) -> dict[str, Any]:
        if not self._cfg.enable_fact_extraction:
            return {"skipped": "disabled"}
        await self.flush_pending()
        result = await self._ensure_extractor().extract_pass()
        if self._bridge is not None:
            try:
                await self._bridge.publish("facts_extracted", result)
            except Exception:  # noqa: BLE001
                logger.exception("facts_extracted publish failed")
        return result

    async def _run_reflect(self, *, period: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.enable_reflections:
            return {"skipped": "disabled"}
        await self.flush_pending()
        ref = self._ensure_reflector()
        if period == "day":
            now = time.time()
            day_end = float(payload.get("day_end_ts") or now)
            day_start = float(payload.get("day_start_ts")
                              or (day_end - 86400.0))
            result = await ref.reflect_day(day_start_ts=day_start,
                                            day_end_ts=day_end)
        else:
            conv_id = str(payload.get("conv_id") or self._conv_id)
            result = await ref.reflect_session(conv_id=conv_id)
        if self._bridge is not None:
            try:
                await self._bridge.publish("reflection_written", result)
            except Exception:  # noqa: BLE001
                logger.exception("reflection_written publish failed")
        return result

    async def _extract_loop(self) -> None:
        """Periodic background extraction pass."""
        if not self._cfg.enable_fact_extraction:
            return
        interval = max(60.0, self._cfg.extract_interval_secs)
        # Wait for some content to accumulate before the first pass.
        await asyncio.sleep(self._cfg.extract_first_delay_secs)
        while True:
            try:
                await self._run_extract_pass()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("extract loop tick failed")
                await asyncio.sleep(interval)

    # ── Background flusher ────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        interval = max(1.0, self._cfg.embed_flush_interval_secs)
        while True:
            try:
                await asyncio.sleep(interval)
                if (len(self._pending) >= self._cfg.embed_batch_size
                        or (self._pending and time.time() % 1 < interval)):
                    await self.flush_pending()
                elif self._pending:
                    # Drain stragglers each tick so a single pending
                    # turn doesn't sit forever.
                    await self.flush_pending()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("recall flush loop tick failed")

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start recall service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "voice_transcript",
                "llm_response",
                "recall_query_request",
                "recall_index_request",
                "recall_extract_request",
                "recall_reflect_request",
                "voice_shutdown_initiated",
            ],
            role="recall",
        )
        logger.info("RecallService starting — db=%s model=%s conv=%s "
                    "extract=%s reflect=%s",
                    self._cfg.db_path, self._cfg.embedding_model, self._conv_id,
                    self._cfg.enable_fact_extraction,
                    self._cfg.enable_reflections)
        self._flusher_task = asyncio.create_task(self._flush_loop())
        if self._cfg.enable_fact_extraction:
            self._extractor_task = asyncio.create_task(self._extract_loop())
        try:
            await self._bridge.run_forever()
        finally:
            for t in (self._flusher_task, self._extractor_task,
                      self._reflector_task):
                if t is not None:
                    t.cancel()
            try:
                await self.flush_pending()
            except Exception:  # noqa: BLE001
                pass
