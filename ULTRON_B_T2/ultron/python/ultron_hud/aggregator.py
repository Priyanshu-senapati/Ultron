"""Aggregator — pulls a compact status snapshot from each read-side
service via the WS bridge.

Each section gets its own *_query_request → *_query_result round-trip.
If the target service is slow or offline, that section reports
``available: false`` and the others still come through.

This module deliberately does *not* import any other ULTRON service
package: in the running stack each service lives in its own process,
so in-process `get_service()` would always return None.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("ultron.hud.aggregator")


# Type aliases for the round-trip helper the service injects.
RequestResponseFn = Callable[[str, dict[str, Any], str, float], Awaitable[Optional[dict[str, Any]]]]


class HudAggregator:
    """Stateless aggregator. The owning service wires in ``rr`` —
    a coroutine ``rr(req_kind, payload, resp_kind, timeout) -> result``
    that publishes ``req_kind`` with ``payload`` and returns the next
    ``resp_kind`` event payload, or None on timeout."""

    def __init__(self, *, request_response: Optional[RequestResponseFn] = None,
                 per_section_timeout: float = 2.0) -> None:
        self._rr = request_response
        self._timeout = per_section_timeout

    def set_request_response(self, rr: RequestResponseFn) -> None:
        self._rr = rr

    async def snapshot(self) -> dict[str, Any]:
        if self._rr is None:
            # Aggregator not yet wired (used in tests or stand-alone) —
            # return the empty shape so callers stay simple.
            return {k: {"available": False} for k in
                    ("dopamine", "wellness", "money", "planner", "code", "kg")}
        sections = await asyncio.gather(
            self._dopamine(), self._wellness(), self._money(),
            self._planner(), self._code(), self._kg(),
            return_exceptions=True,
        )
        keys = ("dopamine", "wellness", "money", "planner", "code", "kg")
        out: dict[str, Any] = {}
        for k, v in zip(keys, sections):
            if isinstance(v, Exception):
                logger.exception("section %s failed: %s", k, v)
                out[k] = {"available": False}
            else:
                out[k] = v
        return out

    # ── Section fetchers ───────────────────────────────────────────────

    async def _dopamine(self) -> dict[str, Any]:
        assert self._rr is not None
        r = await self._rr(
            "dopamine_query_request", {"kind": "current_score"},
            "dopamine_query_result", self._timeout,
        )
        if not r:
            return {"available": False}
        return {"available": True, "score": r.get("score", 0.0)}

    async def _wellness(self) -> dict[str, Any]:
        assert self._rr is not None
        streaks = await self._rr(
            "wellness_query_request", {"kind": "all_streaks"},
            "wellness_query_result", self._timeout,
        )
        if not streaks:
            return {"available": False}
        latest = await self._rr(
            "wellness_query_request", {"kind": "latest_metrics"},
            "wellness_query_result", self._timeout,
        )
        return {
            "available": True,
            "streaks": streaks.get("streaks", []),
            "latest": (latest or {}).get("metrics", {}),
        }

    async def _money(self) -> dict[str, Any]:
        assert self._rr is not None
        summary = await self._rr(
            "money_query_request", {"kind": "monthly_summary"},
            "money_query_result", self._timeout,
        )
        if not summary:
            return {"available": False}
        check = await self._rr(
            "money_query_request", {"kind": "budget_check"},
            "money_query_result", self._timeout,
        )
        alerts = []
        if check:
            alerts = [r for r in (check.get("rows") or []) if r.get("status") != "ok"]
        return {
            "available": True,
            "summary": summary.get("summary", {}),
            "budget_alerts": alerts,
        }

    async def _planner(self) -> dict[str, Any]:
        assert self._rr is not None
        r = await self._rr(
            "plan_query_request", {"kind": "today_summary"},
            "plan_query_result", self._timeout,
        )
        if not r:
            return {"available": False}
        summary = r.get("summary") or {}
        return {
            "available": True,
            "next_blocks": (summary.get("blocks") or [])[:3],
            "next_events": (summary.get("events") or [])[:3],
        }

    async def _code(self) -> dict[str, Any]:
        assert self._rr is not None
        r = await self._rr(
            "code_query_request", {"kind": "stats"},
            "code_query_result", max(self._timeout, 30.0),
        )
        if not r:
            return {"available": False}
        return {"available": True, "stats": r.get("stats", {})}

    async def _kg(self) -> dict[str, Any]:
        assert self._rr is not None
        r = await self._rr(
            "kg_query_request", {"kind": "stats"},
            "kg_query_result", self._timeout,
        )
        if not r:
            return {"available": False}
        return {"available": True, "stats": r.get("stats", {})}
