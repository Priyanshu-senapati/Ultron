"""DopamineService — listens to activity events, scores them, alerts.

Subscribes:
  - ``focus_app``        — payload may have {app, title} or just a string
  - ``visual_label``     — payload has {label} or {summary}
  - ``voice_user_said``  — payload has {text}
  - ``insight_snapshot`` — payload has {focus_app, ...}
  - ``dopamine_pattern_set_request``  — payload: pattern dict
  - ``dopamine_query_request``        — payload: {kind, ...}

Publishes:
  - ``dopamine_mark``           — every matched pattern hit
  - ``dopamine_score_update``   — every score change
  - ``dopamine_drift_alert``    — score crossed drift floor
  - ``dopamine_flow_state``     — score crossed flow ceiling
  - ``dopamine_query_result``
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import DopamineConfig
from .scorer import DopamineScorer
from .store import DopamineStore

logger = logging.getLogger("ultron.dopamine.service")


class DopamineService:
    def __init__(self, config: DopamineConfig) -> None:
        self._cfg = config
        self._store = DopamineStore(config)
        self._scorer = DopamineScorer(config)
        self._bridge: Optional[UltronBridge] = None
        self._lock = asyncio.Lock()
        self._patterns: list[dict[str, Any]] = []
        self._patterns_loaded_at: float = 0.0
        # Cooldown trackers per alert kind.
        self._last_alert: dict[str, float] = {}

    @property
    def store(self) -> DopamineStore:
        return self._store

    @property
    def scorer(self) -> DopamineScorer:
        return self._scorer

    def _patterns_fresh(self) -> list[dict[str, Any]]:
        # Refresh from store at most once every 30 seconds — pattern
        # edits are rare and we don't want to hit SQLite per event.
        if not self._patterns or (time.monotonic() - self._patterns_loaded_at) > 30:
            self._patterns = self._store.list_patterns()
            self._patterns_loaded_at = time.monotonic()
        return self._patterns

    # ── Public Python API ──────────────────────────────────────────────

    async def ingest_text(self, text: str, *, source: str = "") -> dict[str, Any]:
        """Match ``text`` against current patterns, persist hits, update
        score, emit relevant events. Returns a small status object."""
        if not text:
            return {"text": text, "matches": 0, "score": self._scorer.score}
        patterns = await asyncio.get_running_loop().run_in_executor(
            None, self._patterns_fresh
        )
        matches = self._scorer.match(text, patterns)
        if not matches:
            return {"text": text, "matches": 0, "score": self._scorer.score}
        now = time.time()
        async with self._lock:
            for m in matches:
                self._store.record_mark(
                    ts=now, pattern=m.pattern, weight=m.weight,
                    kind=m.kind, source=source, context=text[:512],
                )
                if self._bridge is not None:
                    await self._bridge.publish("dopamine_mark", {
                        "ts": now, "pattern": m.pattern,
                        "weight": m.weight, "kind": m.kind,
                        "source": source,
                    })
            new_score = self._scorer.apply(matches)
        if self._bridge is not None:
            await self._bridge.publish("dopamine_score_update", {
                "ts": now, "score": round(new_score, 3),
                "matches": len(matches),
            })
            await self._maybe_alert(new_score, now)
        return {"text": text, "matches": len(matches), "score": new_score}

    async def _maybe_alert(self, score: float, now: float) -> None:
        if self._bridge is None:
            return
        cooldown = self._cfg.alert_cooldown_seconds
        if score <= self._cfg.drift_floor:
            last = self._last_alert.get("drift", 0.0)
            if now - last >= cooldown:
                self._last_alert["drift"] = now
                await self._bridge.publish("dopamine_drift_alert", {
                    "ts": now,
                    "score": round(score, 3),
                    "floor": self._cfg.drift_floor,
                })
        elif score >= self._cfg.flow_ceiling:
            last = self._last_alert.get("flow", 0.0)
            if now - last >= cooldown:
                self._last_alert["flow"] = now
                await self._bridge.publish("dopamine_flow_state", {
                    "ts": now,
                    "score": round(score, 3),
                    "ceiling": self._cfg.flow_ceiling,
                })

    async def set_pattern(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._store.upsert_pattern(
                    name=str(payload["name"]),
                    substring=str(payload["substring"]),
                    weight=int(payload["weight"]),
                    kind=str(payload.get("kind") or "neutral"),
                )
            )
            # Bust cache so the new pattern is used immediately.
            self._patterns_loaded_at = 0.0
        return {"pattern": payload}

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "current_score"))
        loop = asyncio.get_running_loop()
        if kind == "current_score":
            result = {"kind": kind, "score": round(self._scorer.score, 3)}
        elif kind == "list_patterns":
            rows = await loop.run_in_executor(None, lambda: self._store.list_patterns())
            result = {"kind": kind, "rows": rows}
        elif kind == "list_marks":
            rows = await loop.run_in_executor(None, lambda: self._store.list_marks(
                since_ts=payload.get("since_ts"),
                until_ts=payload.get("until_ts"),
                kind=payload.get("mark_kind"),
                limit=int(payload.get("limit", 100)),
            ))
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "rollup":
            since_ts = float(payload.get("since_ts") or (time.time() - 86400))
            rows = await loop.run_in_executor(None, lambda: self._store.rollup_by_pattern(
                since_ts=since_ts, limit=int(payload.get("limit", 50)),
            ))
            result = {"kind": kind, "since_ts": since_ts, "rows": rows}
        else:
            result = {"kind": kind, "rows": [], "error": f"unknown query kind {kind!r}"}
        if self._bridge is not None:
            await self._bridge.publish("dopamine_query_result", result)
        return result

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start dopamine service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "focus_app",
                "visual_label",
                "voice_user_said",
                "insight_snapshot",
                "dopamine_pattern_set_request",
                "dopamine_query_request",
            ],
            role="dopamine-marker",
        )
        logger.info("DopamineService starting — db=%s", self._cfg.db_path)
        await self._bridge.run_forever()

    @staticmethod
    def _text_from_event(kind: str, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return str(payload or "")
        if kind == "focus_app":
            parts = [str(payload.get("app", "")), str(payload.get("title", ""))]
            return " ".join(p for p in parts if p)
        if kind == "visual_label":
            return str(payload.get("label") or payload.get("summary") or "")
        if kind == "voice_user_said":
            return str(payload.get("text") or "")
        if kind == "insight_snapshot":
            return str(payload.get("focus_app", ""))
        return ""

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "dopamine_pattern_set_request":
                await self.set_pattern(payload)
                return
            if kind == "dopamine_query_request":
                await self.query(payload)
                return
            text = self._text_from_event(kind, payload)
            if text:
                await self.ingest_text(text, source=kind)
        except Exception:  # noqa: BLE001
            logger.exception("handler failed for kind=%s", kind)
