"""plan_query tool — read-only access to Module S+J over the WS bridge."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "today_summary"))
        valid = {
            "today_summary", "upcoming_blocks", "upcoming_events",
            "list_goals", "list_outcomes", "goal_progress",
            "all_goal_progress", "outcome_time_spent",
            "list_blocks", "list_events",
        }
        if kind not in valid:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for key in ("horizon_seconds", "status", "goal_id", "outcome_id",
                    "days", "since_ts", "until_ts", "only_pending", "limit"):
            if key in args:
                payload[key] = args[key]
        result = await bridge_rpc.request_response(
            "plan_query_request", payload, "plan_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "planner service did not respond"}
        return result

    return Tool(
        name="plan_query",
        description=(
            "Query the ULTRON planner (read-only). Kinds: "
            "today_summary, upcoming_blocks, upcoming_events, "
            "list_goals, list_outcomes, goal_progress, all_goal_progress, "
            "outcome_time_spent, list_blocks, list_events."
        ),
        category="planner",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "today_summary", "upcoming_blocks", "upcoming_events",
                        "list_goals", "list_outcomes", "goal_progress",
                        "all_goal_progress", "outcome_time_spent",
                        "list_blocks", "list_events",
                    ],
                },
                "horizon_seconds": {"type": "integer", "minimum": 60, "maximum": 31_536_000},
                "status": {"type": "string", "maxLength": 32},
                "goal_id": {"type": "integer", "minimum": 1},
                "outcome_id": {"type": "integer", "minimum": 1},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "since_ts": {"type": "number"},
                "until_ts": {"type": "number"},
                "only_pending": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
