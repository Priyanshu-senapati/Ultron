"""web_search tool — DuckDuckGo web search via ``ddgs``.

Wraps the existing ``ultron_llm.web_search`` helper so we only have one
DDG client in the codebase.
"""
from __future__ import annotations

from typing import Any

from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        max_results = int(args.get("max_results", config.web_search_max_results))
        max_results = max(1, min(max_results, 10))

        # Local import: ultron_llm.web_search hits ``ddgs`` only on first call
        # and we don't want this tool's registration to drag the dep in
        # at import time.
        from ultron_llm.web_search import search as _ddg_search  # type: ignore[import]

        results = await _ddg_search(query, max_results=max_results)
        return {
            "query": query,
            "count": len(results),
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
        }

    return Tool(
        name="web_search",
        description="DuckDuckGo web search. Returns up to 10 title/url/snippet rows.",
        category="internet",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )
