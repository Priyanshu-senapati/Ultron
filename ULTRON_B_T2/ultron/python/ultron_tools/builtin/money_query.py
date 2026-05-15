"""money_query tool — read-only access to Module P over the WS bridge."""
from __future__ import annotations

from typing import Any

from .. import bridge_rpc
from ..config import ToolsConfig
from ..registry import Tool


def build(config: ToolsConfig) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "monthly_summary"))
        valid = {
            "monthly_summary", "category_rollup", "top_merchants",
            "budget_check", "account_balances", "list_transactions",
            "list_budgets", "list_categories", "list_accounts",
        }
        if kind not in valid:
            return {"ok": False, "reason": f"unknown kind {kind!r}"}
        payload: dict[str, Any] = {"kind": kind}
        for key in ("month", "category", "account", "tx_kind",
                    "since_ts", "until_ts", "limit"):
            if key in args:
                payload[key] = args[key]
        result = await bridge_rpc.request_response(
            "money_query_request", payload, "money_query_result", timeout=5.0,
        )
        if result is None:
            return {"ok": False, "reason": "money service did not respond"}
        return result

    return Tool(
        name="money_query",
        description=(
            "Query the ULTRON money ledger (read-only). Kinds: "
            "monthly_summary, category_rollup, top_merchants, "
            "budget_check, account_balances, list_transactions, "
            "list_budgets, list_categories, list_accounts."
        ),
        category="money",
        confirm_required=False,
        args_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "monthly_summary", "category_rollup", "top_merchants",
                        "budget_check", "account_balances", "list_transactions",
                        "list_budgets", "list_categories", "list_accounts",
                    ],
                },
                "month": {"type": "string", "maxLength": 16},
                "category": {"type": "string", "maxLength": 64},
                "account": {"type": "string", "maxLength": 64},
                "tx_kind": {"type": "string", "maxLength": 16},
                "since_ts": {"type": "number"},
                "until_ts": {"type": "number"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=handler,
    )
