"""Unified Recall — long-term semantic memory for ULTRON.

Phase 1 (shipped): every conversation turn (user + assistant) is
embedded with sentence-transformers and stored in a SQLite + numpy
vector index. ``recall_query_request {kind: search, query: "..."}``
returns the most-relevant past turns regardless of how long ago they
happened. Module C / Agent Mesh / any tool can call it to break out of
the deque's last-N-turns window.

Phase 2 (later): a periodic LLM extractor reads recent turns, emits
durable (subject, predicate, object) facts, and a daily / session
reflector writes ~300-word summaries — both stored in tables this
service already provisioned.

Public entry::

    from ultron_recall import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import RecallConfig, load_recall_config
from .retriever import (
    FactHit,
    RecallBundle,
    RecallRetriever,
    ReflectionHit,
    TurnHit,
)
from .service import RecallService
from .store import RecallStore, StoredReflection, StoredTurn

_service: Optional[RecallService] = None


def init(config: Optional[RecallConfig] = None) -> RecallService:
    global _service
    if _service is None:
        cfg = config or load_recall_config()
        _service = RecallService(cfg)
    return _service


def get_service() -> Optional[RecallService]:
    return _service


__all__ = [
    "FactHit",
    "RecallBundle",
    "RecallConfig",
    "RecallRetriever",
    "RecallService",
    "RecallStore",
    "ReflectionHit",
    "StoredReflection",
    "StoredTurn",
    "TurnHit",
    "get_service",
    "init",
    "load_recall_config",
]
