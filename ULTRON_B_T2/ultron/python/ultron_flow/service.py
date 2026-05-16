"""FlowService — bridges the detector to the WS bus.

Subscribes:
  - ``insight_snapshot``        — cognitive_load, tension, cadence_band,
                                  focus_app, focus_category, fatigue_flag.
                                  Emitted by insight-pulse every ~5 s.
  - ``input_metrics_updated``   — app_switch_per_min, backspace_rate_per_min,
                                  idle_secs. Also from insight-pulse.
  - ``flow_query_request``      — read-only state / stats query.

Publishes:
  - ``flow_state_changed``      — fires on every state transition with
                                  ``{state, prev_state, duration_seconds,
                                    reason, last_focus_app, ts}``.
                                  Voice engine, HUD, and any future
                                  consumer subscribe to this and adapt.
  - ``flow_query_result``       — answer to ``flow_query_request``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import FlowConfig
from .detector import FlowDetector, FlowSample, FlowState, StateTransition
from .store import FlowStore

logger = logging.getLogger("ultron.flow.service")


class FlowService:
    def __init__(self, config: FlowConfig) -> None:
        self._cfg = config
        self._detector = FlowDetector(config)
        self._store = FlowStore(config)
        self._bridge: Optional[UltronBridge] = None
        self._latest_input: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def detector(self) -> FlowDetector:
        return self._detector

    @property
    def store(self) -> FlowStore:
        return self._store

    # ── Query API ──────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "current"))
        loop = asyncio.get_running_loop()
        if kind == "current":
            start = self._detector.session_start_ts
            duration = (time.time() - start) if start else 0.0
            result = {
                "kind": kind,
                "state": self._detector.state.value,
                "session_start_ts": start,
                "session_duration_seconds": round(duration, 1),
                "silence_voice": (self._cfg.silence_non_urgent_voice
                                  and self._detector.state == FlowState.ACTIVE),
                "dim_hud": (self._cfg.dim_hud
                            and self._detector.state == FlowState.ACTIVE),
            }
        elif kind == "recent":
            rows = await loop.run_in_executor(
                None, lambda: self._store.recent(limit=int(payload.get("limit", 20)))
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "stats":
            stats = await loop.run_in_executor(
                None, lambda: self._store.stats(since_ts=payload.get("since_ts"))
            )
            result = {"kind": kind, "stats": stats}
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("flow_query_result", result)
        return result

    # ── Sample handling ────────────────────────────────────────────────

    def _build_sample(self, snap: dict[str, Any]) -> FlowSample:
        """Merge the latest insight_snapshot with the last input metrics."""
        m = self._latest_input
        return FlowSample(
            ts=time.time(),
            cognitive_load=float(snap.get("cognitive_load") or 0.0),
            tension=float(snap.get("tension") or 0.0),
            cadence_band=str(snap.get("cadence_band") or ""),
            focus_category=str(snap.get("focus_category") or ""),
            app_switch_per_min=float(m.get("app_switch_per_min") or 0.0),
            backspace_per_min=float(m.get("backspace_rate_per_min") or 0.0),
            idle_secs=float(m.get("idle_secs") or 0.0),
            focus_app=str(snap.get("focus_app") or ""),
        )

    async def _on_snapshot(self, snapshot_payload: dict[str, Any]) -> None:
        async with self._lock:
            sample = self._build_sample(snapshot_payload)
            trans = self._detector.feed(sample)
        if trans is not None:
            await self._on_transition(trans)

    async def _on_transition(self, trans: StateTransition) -> None:
        if self._bridge is None:
            return
        payload: dict[str, Any] = {
            "state": trans.to_state.value,
            "prev_state": trans.from_state.value,
            "ts": trans.ts,
        }
        if trans.duration_seconds > 0:
            payload["duration_seconds"] = round(trans.duration_seconds, 1)
            payload["duration_minutes"] = round(trans.duration_seconds / 60.0, 1)
        if trans.reason:
            payload["reason"] = trans.reason
        if trans.last_focus_app:
            payload["last_focus_app"] = trans.last_focus_app
        # While ACTIVE, broadcast the reactions every consumer should
        # apply. Keeps the contract explicit on the wire — voice engine
        # doesn't have to remember which state means quiet.
        if trans.to_state == FlowState.ACTIVE:
            payload["silence_voice"] = self._cfg.silence_non_urgent_voice
            payload["dim_hud"] = self._cfg.dim_hud
        try:
            await self._bridge.publish("flow_state_changed", payload)
        except Exception:  # noqa: BLE001
            logger.exception("flow_state_changed publish failed")

        # Persist completed sessions.
        if trans.from_state == FlowState.ACTIVE and trans.to_state == FlowState.IDLE:
            # Sometimes we go ACTIVE → BROKEN → IDLE in a single tick;
            # the BROKEN branch handles persistence below.
            pass
        if (trans.from_state == FlowState.ACTIVE
                and trans.duration_seconds > 0):
            try:
                start_ts = trans.ts - trans.duration_seconds
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._store.record_session(
                        start_ts=start_ts, end_ts=trans.ts,
                        broken_by=trans.reason,
                        last_focus_app=trans.last_focus_app,
                    ),
                )
                logger.info(
                    "flow session logged: %.1f min, broken_by=%s, app=%s",
                    trans.duration_seconds / 60.0,
                    trans.reason, trans.last_focus_app,
                )
            except Exception:  # noqa: BLE001
                logger.exception("flow session log failed")

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start flow service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "insight_snapshot",
                "input_metrics_updated",
                "flow_query_request",
            ],
            role="flow-protector",
        )
        logger.info("FlowService starting — db=%s", self._cfg.db_path)
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "insight_snapshot":
                await self._on_snapshot(payload)
            elif kind == "input_metrics_updated":
                # Cache for the next snapshot merge.
                self._latest_input = payload
            elif kind == "flow_query_request":
                asyncio.create_task(self.query(payload))
        except Exception:  # noqa: BLE001
            logger.exception("flow handler failed for kind=%s", kind)
