"""Derived views over goals / outcomes / blocks / events.

Pure read-side. The service uses these to answer ``plan_query_request``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import PlannerConfig
from .store import PlannerStore

logger = logging.getLogger("ultron.planner")


class Planner:
    def __init__(self, store: PlannerStore, config: PlannerConfig) -> None:
        self._store = store
        self._cfg = config

    # ── Today / upcoming views ─────────────────────────────────────────

    def upcoming_blocks(self, *, horizon_seconds: int = 24 * 3600, limit: int = 20) -> list[dict[str, Any]]:
        now = time.time()
        return self._store.list_blocks(
            since_ts=now, until_ts=now + max(1, horizon_seconds), limit=limit,
        )

    def upcoming_events(self, *, horizon_seconds: int = 24 * 3600, limit: int = 20) -> list[dict[str, Any]]:
        now = time.time()
        rows = self._store.list_events(
            since_ts=now, until_ts=now + max(1, horizon_seconds),
            only_pending=True, limit=limit,
        )
        return rows

    def today_summary(self) -> dict[str, Any]:
        """Blocks + pending events from now through midnight (local UTC)."""
        now_dt = datetime.now(timezone.utc)
        # End of UTC day. Good enough for a 24 h horizon view; HUD can
        # localise display later.
        end_of_day = datetime(
            now_dt.year, now_dt.month, now_dt.day, 23, 59, 59, tzinfo=timezone.utc,
        ).timestamp()
        now = now_dt.timestamp()
        blocks = self._store.list_blocks(since_ts=now, until_ts=end_of_day, limit=50)
        events = self._store.list_events(
            since_ts=now, until_ts=end_of_day, only_pending=True, limit=50,
        )
        return {
            "now_ts": now,
            "end_of_day_ts": end_of_day,
            "blocks": blocks,
            "events": events,
        }

    # ── Goal progress ──────────────────────────────────────────────────

    def goal_progress(self, goal_id: int) -> dict[str, Any]:
        goal = self._store.get_goal(goal_id)
        if not goal:
            return {"goal_id": goal_id, "found": False}
        outcomes = self._store.list_outcomes(goal_id=goal_id)
        total_w = sum(float(o["weight"]) for o in outcomes) or 1.0
        done_w = sum(float(o["weight"]) for o in outcomes if o["status"] == "done")
        in_prog = sum(1 for o in outcomes if o["status"] == "in_progress")
        return {
            "goal_id": goal_id,
            "found": True,
            "title": goal["title"],
            "status": goal["status"],
            "outcomes_total": len(outcomes),
            "outcomes_done": sum(1 for o in outcomes if o["status"] == "done"),
            "outcomes_in_progress": in_prog,
            "progress": round(done_w / total_w, 3),
        }

    def all_goal_progress(self) -> list[dict[str, Any]]:
        return [
            self.goal_progress(int(g["id"]))
            for g in self._store.list_goals(status="active")
        ]

    # ── Outcome heat (time invested via blocks) ────────────────────────

    def outcome_time_spent(self, outcome_id: int, *, days: int = 30) -> dict[str, Any]:
        cutoff = time.time() - max(1, days) * 86400
        rows = self._store.list_blocks(
            since_ts=cutoff, outcome_id=outcome_id, limit=self._cfg.max_query_rows,
        )
        minutes = sum(
            max(0.0, (r["ts_end"] - r["ts_start"]) / 60.0) for r in rows
        )
        return {
            "outcome_id": outcome_id,
            "days": days,
            "block_count": len(rows),
            "minutes": round(minutes, 1),
        }
