"""knowledge_search tool — semantic search over the curated KG.

Wraps ``ultron_knowledge.KnowledgeRetriever`` (built earlier). Used by
the LLM when grounding answers in indexed docs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    appdata = config.audit_log_path.parent  # …/ULTRON/data
    default_db = appdata / "knowledge.db"

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        top_k = int(args.get("top_k", 5))
        top_k = max(1, min(top_k, 20))

        if not default_db.exists():
            return {"query": query, "hits": [], "note": "knowledge.db not present"}

        try:
            from ultron_knowledge import KnowledgeRetriever  # type: ignore[import]
        except ImportError as exc:
            return {"query": query, "hits": [], "note": f"retriever unavailable: {exc}"}

        retriever = KnowledgeRetriever(db_path=default_db)
        hits = retriever.search(query, top_k=top_k)
        return {
            "query": query,
            "count": len(hits),
            "hits": [
                {
                    "score": float(getattr(h, "score", 0.0)),
                    "title": getattr(h, "title", ""),
                    "path": getattr(h, "path", ""),
                    "snippet": getattr(h, "snippet", "")[:600],
                }
                for h in hits
            ],
        }

    return Tool(
        name="knowledge_search",
        description="Semantic search over the curated knowledge graph (KG).",
        category="memory",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )
