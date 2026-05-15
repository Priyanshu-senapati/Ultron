"""Module K — Knowledge Graph.

A small, durable graph of entities the user cares about (people,
projects, concepts, places, decisions, events) with typed relations
between them. NetworkX provides graph algorithms; SQLite provides
durability. Read-only queries reach agents via ``kg_query``.

This is distinct from the markdown knowledge-base (Module D's text
search) — the graph stores *relations*, not prose.

Public entry::

    from ultron_kg import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import KnowledgeGraphConfig, load_kg_config
from .graph import KnowledgeGraph
from .service import KGService
from .store import KGStore

_service: Optional[KGService] = None


def init(config: Optional[KnowledgeGraphConfig] = None) -> KGService:
    global _service
    if _service is None:
        cfg = config or load_kg_config()
        _service = KGService(cfg)
    return _service


def get_service() -> Optional[KGService]:
    return _service


__all__ = [
    "KGService",
    "KGStore",
    "KnowledgeGraph",
    "KnowledgeGraphConfig",
    "get_service",
    "init",
    "load_kg_config",
]
