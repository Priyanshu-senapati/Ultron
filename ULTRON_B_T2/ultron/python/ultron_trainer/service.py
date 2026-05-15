"""TrainerService — WS-facing owner of the trainer ledger.

Subscribes:
  - ``workout_record_request``   — payload: Workout dict
  - ``sleep_record_request``     — payload: SleepLog dict
  - ``metric_record_request``    — payload: BodyMetric dict
  - ``wellness_query_request``   — payload: ``{kind, ...}``

Publishes:
  - ``workout_recorded`` / ``sleep_recorded`` / ``metric_recorded``
  - ``wellness_query_result``
  - ``wellness_nudge`` — emitted heuristically (e.g., streak alive)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .analytics import TrainerAnalytics
from .config import TrainerConfig
from .models import BodyMetric, SleepLog, Workout
from .store import TrainerStore

logger = logging.getLogger("ultron.trainer.service")


class TrainerService:
    def __init__(self, config: TrainerConfig) -> None:
        self._cfg = config
        self._store = TrainerStore(config)
        self._analytics = TrainerAnalytics(self._store, config)
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()

    @property
    def store(self) -> TrainerStore:
        return self._store

    @property
    def analytics(self) -> TrainerAnalytics:
        return self._analytics

    # ── Public Python API ──────────────────────────────────────────────

    async def record_workout(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            w = Workout(
                ts=float(payload.get("ts") or time.time()),
                exercise=str(payload["exercise"]),
                sets=int(payload.get("sets") or 1),
                reps=int(payload.get("reps") or 0),
                weight_kg=float(payload.get("weight_kg") or 0.0),
                duration_secs=int(payload.get("duration_secs") or 0),
                note=str(payload.get("note") or ""),
            )
            wid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.record_workout(w)
            )
            w.id = wid
            result = {"id": wid, "workout": w.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("workout_recorded", result)
            streak = self._analytics.streak("workout")
            if streak["current"] in (3, 7, 14, 30, 60, 100):
                await self._bridge.publish("wellness_nudge", {
                    "kind": "streak_milestone",
                    "habit": "workout",
                    "current": streak["current"],
                })
        return result

    async def record_sleep(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            s = SleepLog(
                date=str(payload["date"]),
                bedtime_ts=float(payload["bedtime_ts"]),
                wake_ts=float(payload["wake_ts"]),
                quality=int(payload.get("quality") or 3),
                note=str(payload.get("note") or ""),
            )
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.record_sleep(s)
            )
            result = {"sleep": s.as_dict(), "hours": round(s.hours(), 2)}
        if self._bridge is not None:
            await self._bridge.publish("sleep_recorded", result)
            if s.hours() < self._cfg.sleep_target_hours - 1.5:
                await self._bridge.publish("wellness_nudge", {
                    "kind": "low_sleep",
                    "date": s.date,
                    "hours": round(s.hours(), 2),
                    "target": self._cfg.sleep_target_hours,
                })
        return result

    async def record_metric(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            m = BodyMetric(
                ts=float(payload.get("ts") or time.time()),
                weight_kg=payload.get("weight_kg"),
                mood=payload.get("mood"),
                energy=payload.get("energy"),
                note=str(payload.get("note") or ""),
            )
            # Normalise numeric optional fields.
            if m.weight_kg is not None:
                m.weight_kg = float(m.weight_kg)
            if m.mood is not None:
                m.mood = int(m.mood)
            if m.energy is not None:
                m.energy = int(m.energy)
            mid = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.record_metric(m)
            )
            m.id = mid
            result = {"id": mid, "metric": m.as_dict()}
        if self._bridge is not None:
            await self._bridge.publish("metric_recorded", result)
        return result

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "today"))
        loop = asyncio.get_running_loop()
        if kind == "streak":
            result = {"kind": kind, "streak": await loop.run_in_executor(
                None, lambda: self._analytics.streak(
                    str(payload.get("habit", "workout")),
                    as_of=payload.get("as_of"),
                )
            )}
        elif kind == "all_streaks":
            result = {"kind": kind, "streaks": await loop.run_in_executor(
                None, lambda: self._analytics.all_streaks(payload.get("as_of"))
            )}
        elif kind == "weekly_workout_summary":
            result = {"kind": kind, "summary": await loop.run_in_executor(
                None, lambda: self._analytics.weekly_workout_summary(
                    weeks=int(payload.get("weeks", 1))
                )
            )}
        elif kind == "weekly_sleep_summary":
            result = {"kind": kind, "summary": await loop.run_in_executor(
                None, lambda: self._analytics.weekly_sleep_summary(
                    weeks=int(payload.get("weeks", 1))
                )
            )}
        elif kind == "latest_metrics":
            result = {"kind": kind, "metrics": await loop.run_in_executor(
                None, lambda: self._analytics.latest_metrics()
            )}
        elif kind == "weight_trend":
            result = {"kind": kind, "trend": await loop.run_in_executor(
                None, lambda: self._analytics.weight_trend(
                    days=int(payload.get("days", 30))
                )
            )}
        elif kind == "list_workouts":
            rows = await loop.run_in_executor(None, lambda: self._store.list_workouts(
                since_ts=payload.get("since_ts"),
                until_ts=payload.get("until_ts"),
                exercise=payload.get("exercise"),
                limit=int(payload.get("limit", 100)),
            ))
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "list_sleep":
            rows = await loop.run_in_executor(None, lambda: self._store.list_sleep(
                since_date=payload.get("since_date"),
                until_date=payload.get("until_date"),
                limit=int(payload.get("limit", 100)),
            ))
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "list_metrics":
            rows = await loop.run_in_executor(None, lambda: self._store.list_metrics(
                since_ts=payload.get("since_ts"),
                until_ts=payload.get("until_ts"),
                limit=int(payload.get("limit", 100)),
            ))
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("wellness_query_result", result)
        return result

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start trainer service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "workout_record_request",
                "sleep_record_request",
                "metric_record_request",
                "wellness_query_request",
            ],
            role="trainer-twin",
        )
        logger.info("TrainerService starting — db=%s", self._cfg.db_path)
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "workout_record_request":
                await self.record_workout(payload)
            elif kind == "sleep_record_request":
                await self.record_sleep(payload)
            elif kind == "metric_record_request":
                await self.record_metric(payload)
            elif kind == "wellness_query_request":
                await self.query(payload)
        except Exception:  # noqa: BLE001
            logger.exception("handler failed for kind=%s", kind)
