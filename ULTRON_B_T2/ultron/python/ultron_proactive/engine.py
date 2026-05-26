"""Proactive suggestion engine.

Every tick_secs, evaluates a set of rules against current time, day,
and accumulated state. When a rule fires, publishes a
``proactive_suggestion`` event on the bus. The voice engine or toast
bridge can choose to surface it.

Rules are intentionally simple and deterministic -- no ML, no
creepiness. Each rule checks time-of-day + day-of-week and suggests
an action the user might want.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import ProactiveConfig

logger = logging.getLogger("ultron.proactive")
IST = ZoneInfo("Asia/Kolkata")


class ProactiveEngine:
    def __init__(self, cfg: ProactiveConfig, publish) -> None:
        self._cfg = cfg
        self._publish = publish
        self._task: Optional[asyncio.Task] = None
        self._last_fired: dict[str, float] = {}
        self._boot_time = time.monotonic()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="proactive")
        logger.info("proactive engine started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _in_quiet_hours(self, hour: int) -> bool:
        start = self._cfg.quiet_hours_start
        end = self._cfg.quiet_hours_end
        if start > end:
            return hour >= start or hour < end
        return start <= hour < end

    def _can_fire(self, rule_id: str, now_mono: float) -> bool:
        last = self._last_fired.get(rule_id, 0.0)
        if now_mono - last < self._cfg.cooldown_secs:
            return False
        return True

    async def _fire(self, rule_id: str, suggestion: str,
                    action: Optional[dict] = None) -> None:
        now = time.monotonic()
        if not self._can_fire(rule_id, now):
            return
        self._last_fired[rule_id] = now
        payload: dict[str, Any] = {
            "rule": rule_id,
            "suggestion": suggestion,
            "ts": time.time(),
        }
        if action:
            payload["action"] = action
        await self._publish("proactive_suggestion", payload)
        logger.info("proactive: fired %s -- %s", rule_id, suggestion)

    async def _evaluate(self) -> None:
        now = datetime.now(IST)
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Monday
        is_weekend = weekday >= 5

        if self._in_quiet_hours(hour):
            return

        # Morning check-in (8-9 AM weekdays)
        if not is_weekend and 8 <= hour < 9 and minute < 15:
            await self._fire(
                "morning_checkin",
                "Good morning, sir. Want me to run your morning routine?",
                {"macro": "morning_routine"},
            )

        # Hydration reminder (every 2 hours during work hours)
        if 10 <= hour < 20 and hour % 2 == 0 and minute < 10:
            await self._fire(
                "hydration",
                "Quick reminder to grab some water, sir.",
            )

        # Stretch break (after 2 hours of continuous use)
        if 10 <= hour < 18 and minute >= 45 and minute < 55:
            await self._fire(
                "stretch_break",
                "You've been at it a while. Might be a good time for a quick stretch.",
            )

        # End-of-day wind down (6-7 PM weekdays)
        if not is_weekend and 18 <= hour < 19 and minute < 10:
            await self._fire(
                "wind_down",
                "Evening, sir. Consider saving your work and switching to something lighter.",
            )

        # Weekend morning (10 AM weekends)
        if is_weekend and 10 <= hour < 11 and minute < 15:
            await self._fire(
                "weekend_morning",
                "Good morning, sir. It's the weekend. Take it easy today.",
            )

        # Late night warning (11 PM)
        if hour == 23 and minute < 15:
            await self._fire(
                "late_night",
                "It's getting late, sir. Consider wrapping up for tonight.",
            )

        # Eye rest (every 45 min during screen time hours)
        if 9 <= hour < 22 and minute in (20, 21):
            await self._fire(
                "eye_rest",
                "20-20-20 rule: look at something 20 feet away for 20 seconds.",
            )

    async def _loop(self) -> None:
        await asyncio.sleep(self._cfg.boot_delay_secs)
        while True:
            try:
                if self._cfg.enabled:
                    await self._evaluate()
            except Exception as exc:
                logger.error("proactive tick failed: %s", exc)
            await asyncio.sleep(self._cfg.tick_secs)
