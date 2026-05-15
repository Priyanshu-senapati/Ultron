"""wellness_query tool — read-only access to Module TT over the WS bridge."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "all_streaks"))
        valid = {
            "streak", "all_streaks", "weekly_workout_summary",
            "weekly_sleep_summary", "latest_metrics", "weight_trend",
            "list_workouts", "list_sleep", "list_metrics",
        }
        if kind not in valid:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for key in ("habit", "as_of", "weeks", "days",
                    "since_ts", "until_ts", "since_date", "until_date",
                    "exercise", "limit"):
            if key in args:
                payload[key] = args[key]
        result = await bridge_rpc.request_response(
            "wellness_query_request", payload, "wellness_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "trainer service did not respond"}
        return result

    return Tool(
        name="wellness_query",
        description=(
            "Query the ULTRON trainer ledger (read-only). Kinds: "
            "streak, all_streaks, weekly_workout_summary, weekly_sleep_summary, "
            "latest_metrics, weight_trend, list_workouts, list_sleep, list_metrics."
        ),
        category="wellness",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "streak", "all_streaks", "weekly_workout_summary",
                        "weekly_sleep_summary", "latest_metrics", "weight_trend",
                        "list_workouts", "list_sleep", "list_metrics",
                    ],
                },
                "habit": {"type": "string", "maxLength": 32},
                "as_of": {"type": "string", "maxLength": 16},
                "weeks": {"type": "integer", "minimum": 1, "maximum": 52},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "exercise": {"type": "string", "maxLength": 64},
                "since_ts": {"type": "number"},
                "until_ts": {"type": "number"},
                "since_date": {"type": "string", "maxLength": 16},
                "until_date": {"type": "string", "maxLength": 16},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
