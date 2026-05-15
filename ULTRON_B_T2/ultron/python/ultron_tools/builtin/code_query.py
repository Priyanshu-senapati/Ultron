"""code_query tool — queries Module G's code index over the WS bridge.

Runs in the tool-service process; Module G runs in a different process.
We publish ``code_query_request`` and await ``code_query_result``.
"""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "find_symbol"))
        payload: dict[str, Any] = {"kind": kind}
        if kind == "find_symbol":
            payload.update({
                "name": str(args.get("name", "")),
                "symbol_kind": args.get("symbol_kind"),
                "limit": int(args.get("limit", 25)),
            })
        elif kind == "search_symbols":
            payload.update({
                "like": str(args.get("like", "")),
                "limit": int(args.get("limit", 25)),
            })
        elif kind == "list_files":
            payload.update({
                "language": args.get("language"),
                "path_substring": args.get("path_substring"),
                "limit": int(args.get("limit", 100)),
            })
        elif kind == "stats":
            pass
        else:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}

        # Code index may still be rebuilding on first boot — use a wide
        # timeout so the LLM doesn't get a spurious miss.
        result = await bridge_rpc.request_response(
            "code_query_request", payload, "code_query_result", timeout=30.0,
        )
        if result is None:
            return {"ok": False, "reason": "code service did not respond"}
        return result

    return Tool(
        name="code_query",
        description="Query the ULTRON code index. kinds: find_symbol, search_symbols, list_files, stats.",
        category="code",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["find_symbol", "search_symbols", "list_files", "stats"],
                },
                "name": {"type": "string", "maxLength": 256},
                "like": {"type": "string", "maxLength": 256},
                "symbol_kind": {"type": "string", "maxLength": 64},
                "language": {"type": "string", "maxLength": 64},
                "path_substring": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
