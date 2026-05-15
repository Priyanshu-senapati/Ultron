"""dopamine_query tool — read-only access to Module Y over the WS bridge."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "current_score"))
        valid = {"current_score", "list_patterns", "list_marks", "rollup"}
        if kind not in valid:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for key in ("since_ts", "until_ts", "mark_kind", "limit"):
            if key in args:
                payload[key] = args[key]
        result = await bridge_rpc.request_response(
            "dopamine_query_request", payload, "dopamine_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "dopamine service did not respond"}
        return result

    return Tool(
        name="dopamine_query",
        description=(
            "Query the ULTRON dopamine marker (read-only). Kinds: "
            "current_score, list_patterns, list_marks, rollup."
        ),
        category="awareness",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["current_score", "list_patterns", "list_marks", "rollup"],
                },
                "since_ts": {"type": "number"},
                "until_ts": {"type": "number"},
                "mark_kind": {"type": "string", "enum": ["rewarding", "wasteful", "neutral"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
