"""EmotionService — bridges the detector + tracker to the bus.

Subscribes:
  - ``voice_transcript``      → analyze user utterance
  - ``llm_response``          → optional: mirror our own tone back
                                 (kept off by default — only the user's
                                 emotional state shapes the response)
  - ``insight_snapshot``      → keep tension cached for cross-ref
  - ``emotion_query_request`` → read-only query

Publishes:
  - ``emotion_state_changed`` → on any non-trivial change. Payload is
                                the tracker snapshot (valence, arousal,
                                frustration, confidence, mood_label,
                                source, matched, last_text_preview).
  - ``emotion_query_result``  → answer to a query.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import EmotionConfig
from .detector import EmotionSignal, analyze
from .state import EmotionTracker

logger = logging.getLogger("ultron.emotion.service")


class EmotionService:
    def __init__(self, config: EmotionConfig) -> None:
        self._cfg = config
        self._tracker = EmotionTracker(half_life_secs=config.half_life_secs)
        self._bridge: Optional[UltronBridge] = None
        self._latest_tension: Optional[float] = None
        self._latest_load: Optional[float] = None
        self._last_published: dict = {}
        self._last_publish_ts: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def tracker(self) -> EmotionTracker:
        return self._tracker

    # ── Core analysis path ────────────────────────────────────────────

    async def ingest_text(self, text: str, *, role: str = "user",
                          ts: Optional[float] = None) -> None:
        """Score the text + update the tracker + maybe publish."""
        ts = ts if ts is not None else time.time()
        sig = analyze(
            text, tension=self._latest_tension,
            cognitive_load=self._latest_load,
            cfg=self._cfg, ts=ts,
        )
        async with self._lock:
            self._tracker.apply(sig)
            snap = self._tracker.snapshot()
        # Decide whether to publish.
        should_publish = self._tracker.is_significant_change(
            self._last_published, self._cfg.min_change_for_publish,
        )
        # Strong frustration always publishes immediately even if change
        # is small — the consumer should react fast.
        if sig.frustration >= self._cfg.immediate_publish_frustration:
            should_publish = True
        # Cool-down: never spam the bus faster than min_publish_interval.
        elapsed = ts - self._last_publish_ts
        if should_publish and elapsed < self._cfg.min_publish_interval_secs:
            should_publish = False
        if should_publish and self._bridge is not None:
            try:
                await self._bridge.publish("emotion_state_changed", snap)
                self._last_published = dict(snap)
                self._last_publish_ts = ts
                logger.info(
                    "emotion: %s  v=%.2f a=%.2f f=%.2f conf=%.2f  "
                    "src=%s  matched=%s",
                    snap.get("mood_label"),
                    snap["valence"], snap["arousal"], snap["frustration"],
                    snap["confidence"], snap["source"],
                    snap["last_matched"],
                )
            except Exception:  # noqa: BLE001
                logger.exception("emotion_state_changed publish failed")

    # ── Query API ─────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "current"))
        if kind == "current":
            async with self._lock:
                snap = self._tracker.snapshot()
            result = {"kind": kind, "state": snap}
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            try:
                await self._bridge.publish("emotion_query_result", result)
            except Exception:  # noqa: BLE001
                logger.exception("emotion_query_result publish failed")
        return result

    # ── Event handler ─────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "voice_transcript":
                text = str(payload.get("text") or "").strip()
                if text:
                    await self.ingest_text(text, role="user")
            elif kind == "insight_snapshot":
                t = payload.get("tension")
                if t is not None:
                    self._latest_tension = float(t)
                cl = payload.get("cognitive_load")
                if cl is not None:
                    self._latest_load = float(cl)
            elif kind == "emotion_query_request":
                asyncio.create_task(self.query(payload))
        except Exception:  # noqa: BLE001
            logger.exception("emotion handler failed for kind=%s", kind)

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start emotion service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "voice_transcript",
                "insight_snapshot",
                "emotion_query_request",
            ],
            role="emotion",
        )
        logger.info("EmotionService starting — half_life=%.0fs",
                    self._cfg.half_life_secs)
        await self._bridge.run_forever()
