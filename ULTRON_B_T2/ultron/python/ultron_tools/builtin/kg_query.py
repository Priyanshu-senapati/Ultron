"""kg_query tool — read-only access to Module K over the WS bridge."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "stats"))
        valid = {
            "stats", "search_entities", "list_entities", "find_entity",
            "neighbors", "egonet", "shortest_path", "top_entities",
        }
        if kind not in valid:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for key in ("like", "entity_kind", "name", "entity_id",
                    "direction", "radius", "src", "dst", "limit"):
            if key in args:
                payload[key] = args[key]
        result = await bridge_rpc.request_response(
            "kg_query_request", payload, "kg_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "kg service did not respond"}
        return result

    return Tool(
        name="kg_query",
        description=(
            "Query the ULTRON knowledge graph (read-only). Kinds: "
            "stats, search_entities, list_entities, find_entity, "
            "neighbors, egonet, shortest_path, top_entities."
        ),
        category="knowledge",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "stats", "search_entities", "list_entities", "find_entity",
                        "neighbors", "egonet", "shortest_path", "top_entities",
                    ],
                },
                "like": {"type": "string", "maxLength": 128},
                "entity_kind": {"type": "string", "maxLength": 32},
                "name": {"type": "string", "maxLength": 128},
                "entity_id": {"type": "integer", "minimum": 1},
                "direction": {"type": "string", "enum": ["in", "out", "both"]},
                "radius": {"type": "integer", "minimum": 1, "maximum": 5},
                "src": {"type": "integer", "minimum": 1},
                "dst": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
