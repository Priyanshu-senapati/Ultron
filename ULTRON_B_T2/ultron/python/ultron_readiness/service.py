"""ReadinessService — listens to wellness + flow + insight events,
publishes a periodic 0-100 readiness score.

Subscribes:
  - ``sleep_recorded``           — updates the sleep signal.
  - ``workout_recorded``         — updates the activity signal.
  - ``insight_snapshot``         — updates the tension EWMA.
  - ``flow_state_changed``       — when ACTIVE→BROKEN/IDLE with
                                    duration_seconds > 0, log the flow
                                    session into the trailing-24h pool.
  - ``readiness_query_request``  — read-only query.

Publishes:
  - ``readiness_score_update``   — full score payload on (a) startup,
                                   (b) every recompute_interval_secs,
                                   (c) any sleep/workout event.
  - ``readiness_query_result``   — answer to a query request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .calculator import ReadinessScore, compute_score
from .config import ReadinessConfig
from .state import ReadinessState
from .store import ReadinessStore

logger = logging.getLogger("ultron.readiness.service")


class ReadinessService:
    def __init__(self, config: ReadinessConfig) -> None:
        self._cfg = config
        self._state = ReadinessState(config.calm_ewma_half_life_secs)
        self._store = ReadinessStore(config)
        self._bridge: Optional[UltronBridge] = None
        self._latest: Optional[ReadinessScore] = None
        self._lock = asyncio.Lock()
        self._recompute_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> ReadinessState:
        return self._state

    @property
    def store(self) -> ReadinessStore:
        return self._store

    @property
    def latest(self) -> Optional[ReadinessScore]:
        return self._latest

    # ── Score computation ─────────────────────────────────────────────

    def compute_now(self, now: Optional[float] = None) -> ReadinessScore:
        now = now if now is not None else time.time()
        score = compute_score(
            sleep_hours=self._state.last_sleep_hours,
            flow_minutes_yesterday=self._state.flow_minutes_in_last_24h(now=now),
            avg_tension=self._state.tension_ewma,
            last_workout_ts=self._state.last_workout_ts,
            now=now,
            cfg=self._cfg,
        )
        self._latest = score
        return score

    async def _publish_and_record(self, score: ReadinessScore) -> None:
        if self._bridge is None:
            return
        try:
            await self._bridge.publish("readiness_score_update", score.as_dict())
        except Exception:  # noqa: BLE001
            logger.exception("readiness_score_update publish failed")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.record(score)
            )
        except Exception:  # noqa: BLE001
            logger.exception("readiness store record failed")

    async def _recompute_and_publish(self) -> ReadinessScore:
        async with self._lock:
            score = self.compute_now()
        await self._publish_and_record(score)
        return score

    # ── Query API ─────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "current"))
        loop = asyncio.get_running_loop()
        if kind == "current":
            score = self._latest or self.compute_now()
            result = {"kind": kind, "score": score.as_dict()}
        elif kind == "recompute":
            score = await self._recompute_and_publish()
            result = {"kind": kind, "score": score.as_dict()}
        elif kind == "recent":
            rows = await loop.run_in_executor(
                None, lambda: self._store.recent(limit=int(payload.get("limit", 30)))
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "explain":
            score = self._latest or self.compute_now()
            result = {
                "kind": kind,
                "score": score.as_dict(),
                "explanation": [c.as_dict() for c in score.components],
            }
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            try:
                await self._bridge.publish("readiness_query_result", result)
            except Exception:  # noqa: BLE001
                logger.exception("readiness_query_result publish failed")
        return result

    # ── Event handlers ────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "sleep_recorded":
                hours = payload.get("hours")
                if hours is None and "sleep" in payload:
                    # Some publishers nest under "sleep".
                    hours = (payload["sleep"] or {}).get("hours")
                if hours is not None:
                    self._state.update_sleep(float(hours))
                    asyncio.create_task(self._recompute_and_publish())
            elif kind == "workout_recorded":
                w = payload.get("workout") or payload
                ts = float(w.get("ts") or time.time())
                self._state.update_workout(
                    ts, duration_secs=int(w.get("duration_secs") or 0),
                )
                asyncio.create_task(self._recompute_and_publish())
            elif kind == "insight_snapshot":
                t = payload.get("tension")
                if t is not None:
                    self._state.update_tension(float(t))
            elif kind == "flow_state_changed":
                # Capture completed sessions for the 24h pool. The flow
                # service emits ACTIVE→BROKEN with duration_seconds.
                prev = str(payload.get("prev_state") or "")
                state = str(payload.get("state") or "")
                dur = float(payload.get("duration_seconds") or 0.0)
                if prev == "active" and state in ("broken", "idle") and dur > 0:
                    ts = float(payload.get("ts") or time.time())
                    self._state.update_flow_session(end_ts=ts, duration_secs=dur)
            elif kind == "readiness_query_request":
                asyncio.create_task(self.query(payload))
        except Exception:  # noqa: BLE001
            logger.exception("readiness handler failed for kind=%s", kind)

    # ── Background loop ───────────────────────────────────────────────

    async def _recompute_loop(self) -> None:
        await asyncio.sleep(self._cfg.boot_delay_secs)
        # Initial publish so subscribers see something right away.
        await self._recompute_and_publish()
        while True:
            try:
                await asyncio.sleep(self._cfg.recompute_interval_secs)
                await self._recompute_and_publish()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("readiness recompute loop tick failed")

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start readiness service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "sleep_recorded",
                "workout_recorded",
                "insight_snapshot",
                "flow_state_changed",
                "readiness_query_request",
            ],
            role="readiness-score",
        )
        logger.info(
            "ReadinessService starting — sleep_target=%.1fh flow_target=%.0fmin db=%s",
            self._cfg.sleep_target_hours, self._cfg.flow_target_minutes,
            self._cfg.db_path,
        )
        self._recompute_task = asyncio.create_task(self._recompute_loop())
        try:
            await self._bridge.run_forever()
        finally:
            if self._recompute_task is not None:
                self._recompute_task.cancel()
