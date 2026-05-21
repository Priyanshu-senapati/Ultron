"""ReentryService — bridges the detector + context buffer to the WS bus.

Subscribes:
  - ``input_metrics_updated`` — drives the presence detector (idle_secs).
  - ``insight_snapshot``      — focus_app, focus_category.
  - ``visual_label``          — LLaVA labels.
  - ``llm_response``          — last C reply for the quote.
  - ``voice_transcript``      — last thing the user said.
  - ``git_activity``          — commits to count during absence.
  - ``reentry_query_request`` — read-only state query (current/last_brief).

Publishes:
  - ``reentry_brief``         — fires on AWAY → RETURNING when the away
                                duration meets ``min_away_minutes_for_brief``
                                and we're past cooldown. Payload:
                                ``{text, away_seconds, away_minutes,
                                   away_started_ts, ts, snapshot}``.
  - ``presence_state_changed`` — every detector transition, regardless
                                 of whether a brief was spoken. Useful
                                 for HUD indicators.
  - ``reentry_query_result``  — answer to ``reentry_query_request``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .composer import compose_brief
from .config import ReentryConfig
from .context import ReentryContext
from .detector import PresenceState, PresenceTransition, ReentryDetector

logger = logging.getLogger("ultron.reentry.service")


class ReentryService:
    def __init__(self, config: ReentryConfig) -> None:
        self._cfg = config
        self._detector = ReentryDetector(config)
        self._context = ReentryContext(config.recent_lookback_secs)
        self._bridge: Optional[UltronBridge] = None
        self._last_brief_ts: float = 0.0
        self._last_brief: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def detector(self) -> ReentryDetector:
        return self._detector

    @property
    def context(self) -> ReentryContext:
        return self._context

    @property
    def last_brief(self) -> dict[str, Any]:
        return dict(self._last_brief)

    # ── Query API ──────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "current"))
        if kind == "current":
            result = {
                "kind": kind,
                "state": self._detector.state.value,
                "away_started_ts": self._detector.away_started_ts,
                "last_idle_secs": self._detector.last_idle_secs,
                "last_brief_ts": self._last_brief_ts,
            }
        elif kind == "last_brief":
            result = {"kind": kind, "brief": dict(self._last_brief)}
        elif kind == "snapshot":
            result = {"kind": kind, "snapshot": asdict(self._context.snapshot())}
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            try:
                await self._bridge.publish("reentry_query_result", result)
            except Exception:  # noqa: BLE001
                logger.exception("reentry_query_result publish failed")
        return result

    # ── Transition handling ────────────────────────────────────────────

    async def _on_transition(self, trans: PresenceTransition) -> None:
        if self._bridge is None:
            return
        # Always announce the state change for HUD subscribers.
        try:
            await self._bridge.publish("presence_state_changed", {
                "state": trans.to_state.value,
                "prev_state": trans.from_state.value,
                "ts": trans.ts,
                "away_started_ts": trans.away_started_ts,
                "away_duration_seconds": round(trans.away_duration_seconds, 1),
            })
        except Exception:  # noqa: BLE001
            logger.exception("presence_state_changed publish failed")

        # PRESENT → AWAY: reset the away-window counters in context.
        if trans.from_state == PresenceState.PRESENT and trans.to_state == PresenceState.AWAY:
            self._context.mark_away(trans.away_started_ts or trans.ts)
            logger.info("presence: away (idle reached threshold at ts=%.0f)", trans.ts)
            return

        # AWAY → RETURNING: maybe fire the brief.
        if trans.to_state == PresenceState.RETURNING:
            self._context.mark_return()
            await self._maybe_brief(trans)

    async def _maybe_brief(self, trans: PresenceTransition) -> None:
        if not self._cfg.speak_brief:
            return
        away_secs = trans.away_duration_seconds
        min_secs = self._cfg.min_away_minutes_for_brief * 60.0
        if away_secs < min_secs:
            logger.info("reentry: skipped, away %.0fs < %.0fs threshold",
                        away_secs, min_secs)
            return
        if trans.ts - self._last_brief_ts < self._cfg.cooldown_secs:
            logger.info("reentry: skipped, within cooldown (last brief %.0fs ago)",
                        trans.ts - self._last_brief_ts)
            return

        snap = self._context.snapshot(now=trans.ts)
        text = compose_brief(snap, away_secs, self._cfg)
        if not text.strip():
            logger.info("reentry: composer returned empty brief, skipping")
            return

        self._last_brief_ts = trans.ts
        self._last_brief = {
            "text": text,
            "away_seconds": round(away_secs, 1),
            "away_minutes": round(away_secs / 60.0, 1),
            "away_started_ts": trans.away_started_ts,
            "ts": trans.ts,
            "snapshot": asdict(snap),
        }
        try:
            await self._bridge.publish("reentry_brief", self._last_brief)
            logger.info("reentry brief published (%d chars, away %.1f min)",
                        len(text), away_secs / 60.0)
        except Exception:  # noqa: BLE001
            logger.exception("reentry_brief publish failed")

    # ── Event handler ──────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        now = time.time()
        try:
            if kind == "input_metrics_updated":
                idle = float(payload.get("idle_secs") or 0.0)
                async with self._lock:
                    trans = self._detector.feed_idle(idle, ts=now)
                if trans is not None:
                    await self._on_transition(trans)
            elif kind == "insight_snapshot":
                self._context.on_insight_snapshot(payload, ts=now)
            elif kind == "visual_label":
                self._context.on_visual_label(payload, ts=now)
            elif kind == "llm_response":
                self._context.on_llm_response(payload, ts=now)
            elif kind == "voice_transcript":
                self._context.on_voice_transcript(payload, ts=now)
            elif kind == "git_activity":
                self._context.on_git_activity(payload, ts=now)
            elif kind == "reentry_query_request":
                asyncio.create_task(self.query(payload))
        except Exception:  # noqa: BLE001
            logger.exception("reentry handler failed for kind=%s", kind)

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start reentry service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "input_metrics_updated",
                "insight_snapshot",
                "visual_label",
                "llm_response",
                "voice_transcript",
                "git_activity",
                "reentry_query_request",
            ],
            role="reentry-protocol",
        )
        logger.info("ReentryService starting — away_threshold=%.0fs min_brief=%.1fmin",
                    self._cfg.away_threshold_secs, self._cfg.min_away_minutes_for_brief)
        await self._bridge.run_forever()
