"""recall tool — semantic search over ULTRON's long-term memory.

Exposes the recall service via the standard tool interface so the LLM
(Module C) can call it during a conversation. Kinds:

  - ``search``  — top-K relevant past turns + reflections + facts for a
                  natural-language query. Returns a ready-to-inject
                  ``prompt_block`` plus the raw bundle.
  - ``counts``  — how many turns / reflections / facts are stored.
  - ``recent``  — last N turns (optionally filtered by conv_id).

Sets ``confirm_required=False`` because recall is read-only.
"""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "search"))
        if kind not in ("search", "counts", "recent"):
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        if kind == "search":
            q = str(args.get("query") or "").strip()
            if not q:
                return {"ok": False, "reason": "empty query"}
            payload["query"] = q
            for k in ("top_k", "include_reflections", "include_facts",
                      "since_ts"):
                if k in args:
                    payload[k] = args[k]
        elif kind == "recent":
            for k in ("limit", "conv_id"):
                if k in args:
                    payload[k] = args[k]
        result = await bridge_rpc.request_response(
            "recall_query_request", payload, "recall_query_result",
            timeout=6.0,
        )
        if result is None:
            return {"ok": False, "reason": "recall service did not respond"}
        result["ok"] = True
        return result

    return Tool(
        name="recall",
        description=(
            "Semantic search over the assistant's long-term memory of past "
            "conversations, facts about the user, and session reflections. "
            "Use when the user references past events the current window "
            "doesn't cover ('the thing we discussed last week', 'my dog's "
            "name', 'what did you say about X'). Kinds: search (top-K "
            "relevant hits), counts (corpus size), recent (last N turns)."
        ),
        category="awareness",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                          "enum": ["search", "counts", "recent"]},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 30},
                "include_reflections": {"type": "boolean"},
                "include_facts": {"type": "boolean"},
                "since_ts": {"type": "number"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "conv_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
