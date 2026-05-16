"""flow_query tool — read-only access to the flow detector."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "current"))
        if kind not in ("current", "recent", "stats"):
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for k in ("limit", "since_ts"):
            if k in args:
                payload[k] = args[k]
        result = await bridge_rpc.request_response(
            "flow_query_request", payload, "flow_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "flow service did not respond"}
        return result

    return Tool(
        name="flow_query",
        description=(
            "Query the flow state protector (read-only). Kinds: "
            "current (live state + duration), recent (last N sessions), "
            "stats (rollup over last 7 days by default)."
        ),
        category="awareness",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["current", "recent", "stats"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "since_ts": {"type": "number"},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
