"""InterruptService — log focus interruptions + pair them with recovery times.

Subscribes:
  - ``flow_state_changed``        — flow ACTIVE → BROKEN is an interrupt;
                                     IDLE/ENTERING → ACTIVE is the
                                     recovery marker for pending entries.
  - ``voice_transcript``          — wake-word interactions while present
                                     are self-interrupts.
  - ``wellness_nudge``            — system-initiated interrupt.
  - ``presence_state_changed``    — (opt-in) re-entry events.
  - ``insight_snapshot``          — track the latest focus_app for
                                     attribution on the next interrupt.
  - ``interrupt_query_request``   — read-only state / stats query.

Publishes:
  - ``interrupt_logged``          — fires when a new interrupt is
                                     recorded with the full record.
  - ``interrupt_recovered``       — fires when a previously-open
                                     interrupt gets paired with a
                                     flow-ACTIVE recovery marker.
  - ``interrupt_query_result``    — answer to a query.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import InterruptConfig
from .store import Interrupt, InterruptStore

logger = logging.getLogger("ultron.interrupts.service")


class InterruptService:
    def __init__(self, config: InterruptConfig) -> None:
        self._cfg = config
        self._store = InterruptStore(config)
        self._bridge: Optional[UltronBridge] = None
        self._pending: deque[Interrupt] = deque()
        self._last_focus_app: str = ""
        self._presence_state: str = "present"
        self._lock = asyncio.Lock()

    @property
    def store(self) -> InterruptStore:
        return self._store

    @property
    def presence_state(self) -> str:
        return self._presence_state

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ── Recording ─────────────────────────────────────────────────────

    async def _record(self, source: str, detail: str, ts: Optional[float] = None,
                      focus_app: Optional[str] = None) -> Interrupt:
        ts = ts if ts is not None else time.time()
        intr = Interrupt(
            ts=ts,
            source=source,
            detail=detail,
            focus_app=focus_app if focus_app is not None else self._last_focus_app,
        )
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._store.record(intr)
        )
        self._pending.append(intr)
        # Cap the pending set so a flood doesn't bloat memory.
        while len(self._pending) > self._cfg.max_pending_interrupts:
            self._pending.popleft()
        if self._bridge is not None:
            try:
                await self._bridge.publish("interrupt_logged", intr.as_dict())
            except Exception:  # noqa: BLE001
                logger.exception("interrupt_logged publish failed")
        logger.info("interrupt logged: source=%s app=%s detail=%s",
                    intr.source, intr.focus_app, intr.detail)
        return intr

    async def _pair_recoveries(self, recovery_ts: float) -> None:
        """Pair every pending interrupt within the recovery window with
        ``recovery_ts``. Older interrupts beyond the window are dropped
        without a recovery time."""
        if not self._pending:
            return
        cutoff = recovery_ts - self._cfg.recovery_window_secs
        # Walk forward, splitting the deque into recovered (>= cutoff)
        # and stale (< cutoff). Stale entries are forgotten.
        keep: deque[Interrupt] = deque()
        recovered: list[Interrupt] = []
        while self._pending:
            intr = self._pending.popleft()
            if intr.ts < cutoff:
                # Too old — leave it persisted without recovery.
                continue
            if intr.recovery_secs is not None:
                keep.append(intr)
                continue
            # Pair it.
            rec_secs = max(0.0, recovery_ts - intr.ts)
            intr.recovery_secs = round(rec_secs, 1)
            intr.recovery_ts = recovery_ts
            await asyncio.get_running_loop().run_in_executor(
                None, lambda i=intr: self._store.update_recovery(
                    i.id, i.recovery_secs or 0.0, i.recovery_ts or 0.0,
                ),
            )
            recovered.append(intr)
        self._pending = keep
        if self._bridge is None:
            return
        for intr in recovered:
            try:
                await self._bridge.publish("interrupt_recovered", intr.as_dict())
            except Exception:  # noqa: BLE001
                logger.exception("interrupt_recovered publish failed")
        if recovered:
            logger.info("interrupts recovered: %d (recovery_window=%.0fs)",
                        len(recovered), self._cfg.recovery_window_secs)

    # ── Query API ─────────────────────────────────────────────────────

    async def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "today"))
        loop = asyncio.get_running_loop()
        if kind == "today":
            stats = await loop.run_in_executor(None, lambda: self._store.today())
            result = {"kind": kind, "stats": stats,
                      "pending": [i.as_dict() for i in self._pending]}
        elif kind == "stats":
            stats = await loop.run_in_executor(
                None, lambda: self._store.stats(since_ts=payload.get("since_ts"))
            )
            result = {"kind": kind, "stats": stats}
        elif kind == "recent":
            rows = await loop.run_in_executor(
                None, lambda: self._store.recent(limit=int(payload.get("limit", 50)))
            )
            result = {"kind": kind, "rows": rows, "count": len(rows)}
        elif kind == "pending":
            result = {"kind": kind,
                      "rows": [i.as_dict() for i in self._pending],
                      "count": len(self._pending)}
        else:
            result = {"kind": kind, "error": f"unknown kind {kind!r}"}
        if self._bridge is not None:
            try:
                await self._bridge.publish("interrupt_query_result", result)
            except Exception:  # noqa: BLE001
                logger.exception("interrupt_query_result publish failed")
        return result

    # ── Event handler ─────────────────────────────────────────────────

    async def _on_flow_state(self, payload: dict[str, Any]) -> None:
        prev = str(payload.get("prev_state") or "")
        state = str(payload.get("state") or "")
        ts = float(payload.get("ts") or time.time())

        # Flow break = interrupt (if duration crosses the floor).
        if (self._cfg.record_flow_break and prev == "active" and state == "broken"):
            dur = float(payload.get("duration_seconds") or 0.0)
            if dur >= self._cfg.min_flow_break_duration_secs:
                reason = (payload.get("reason") or "").strip() or "unknown"
                app = str(payload.get("last_focus_app") or self._last_focus_app)
                detail = (f"broke flow after {int(round(dur / 60.0))}m "
                          f"({reason})")
                await self._record("flow_break", detail, ts=ts, focus_app=app)

        # Recovery: pair pending interrupts on the next ACTIVE entry.
        if state == "active" and prev in ("entering", "idle"):
            await self._pair_recoveries(recovery_ts=ts)

    async def _on_voice_transcript(self, payload: dict[str, Any]) -> None:
        if not self._cfg.record_wake_word:
            return
        # Only record voice-initiated commands as self-interrupts when
        # the user was PRESENT — coming back from AWAY is a re-entry,
        # not an interrupt.
        if self._presence_state != "present":
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        # Truncate the spoken text to keep detail tidy.
        snippet = (text[:80] + "…") if len(text) > 80 else text
        await self._record("wake_word", f"voice command: '{snippet}'")

    async def _on_wellness_nudge(self, payload: dict[str, Any]) -> None:
        if not self._cfg.record_wellness_nudge:
            return
        kind = str(payload.get("kind") or "")
        detail = f"wellness nudge: {kind}" if kind else "wellness nudge"
        await self._record("wellness_nudge", detail)

    async def _on_presence(self, payload: dict[str, Any]) -> None:
        new_state = str(payload.get("state") or "")
        if new_state in ("present", "away", "returning"):
            self._presence_state = new_state
        if self._cfg.record_reentry and new_state == "returning":
            mins = float(payload.get("away_duration_seconds") or 0.0) / 60.0
            await self._record("reentry", f"returned after {mins:.0f}m away")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            if kind == "flow_state_changed":
                await self._on_flow_state(payload)
            elif kind == "voice_transcript":
                await self._on_voice_transcript(payload)
            elif kind == "wellness_nudge":
                await self._on_wellness_nudge(payload)
            elif kind == "presence_state_changed":
                await self._on_presence(payload)
            elif kind == "insight_snapshot":
                app = str(payload.get("focus_app") or "").strip()
                if app:
                    self._last_focus_app = app
            elif kind == "interrupt_query_request":
                asyncio.create_task(self.query(payload))
        except Exception:  # noqa: BLE001
            logger.exception("interrupt handler failed for kind=%s", kind)

    # ── WS lifecycle ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start interrupt service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "flow_state_changed",
                "voice_transcript",
                "wellness_nudge",
                "presence_state_changed",
                "insight_snapshot",
                "interrupt_query_request",
            ],
            role="interrupt-ledger",
        )
        logger.info("InterruptService starting — db=%s recovery_window=%.0fs",
                    self._cfg.db_path, self._cfg.recovery_window_secs)
        await self._bridge.run_forever()
