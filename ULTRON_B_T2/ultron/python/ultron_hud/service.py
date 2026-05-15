"""HudService — aggregator + optional tray.

Subscribes:
  - ``hud_status_request``  → publishes a single hud_status_tick
  - all the ``*_query_result`` topics it needs to await round-trips

Publishes (every tick_seconds):
  - ``hud_status_tick`` — payload is the aggregator snapshot
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .aggregator import HudAggregator
from .config import HudConfig
from .tray import TrayIcon

logger = logging.getLogger("ultron.hud.service")


# Result kinds we await as part of aggregator round-trips. We need to
# subscribe to all of them up front.
_RESULT_KINDS = (
    "dopamine_query_result",
    "wellness_query_result",
    "money_query_result",
    "plan_query_result",
    "code_query_result",
    "kg_query_result",
)


class HudService:
    def __init__(self, config: HudConfig) -> None:
        self._cfg = config
        self._aggregator = HudAggregator()
        self._bridge: Optional[UltronBridge] = None
        self._tick_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._tray: Optional[TrayIcon] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # In-flight round-trip waiters: list per result-kind. The first
        # waiter on each list claims the next event of that kind.
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {
            k: [] for k in _RESULT_KINDS
        }
        self._aggregator.set_request_response(self._request_response)

    @property
    def aggregator(self) -> HudAggregator:
        return self._aggregator

    async def snapshot(self) -> dict[str, Any]:
        snap = await self._aggregator.snapshot()
        snap["ts"] = time.time()
        return snap

    # ── Round-trip helper (used by the aggregator) ─────────────────────

    async def _request_response(
        self,
        request_kind: str,
        payload: dict[str, Any],
        response_kind: str,
        timeout: float,
    ) -> Optional[dict[str, Any]]:
        if self._bridge is None or response_kind not in self._waiters:
            return None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiters[response_kind].append(fut)
        try:
            await self._bridge.publish(request_kind, payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return None
        finally:
            # Best-effort cleanup if we exited before it was resolved.
            try:
                self._waiters[response_kind].remove(fut)
            except ValueError:
                pass

    async def _publish_tick(self) -> None:
        if self._bridge is None:
            return
        snap = await self.snapshot()
        await self._bridge.publish("hud_status_tick", snap)
        if self._tray is not None and self._tray.available and self._cfg.show_score_in_title:
            self._update_tray_title(snap)

    def _update_tray_title(self, snap: dict[str, Any]) -> None:
        try:
            dop = snap.get("dopamine") or {}
            score = dop.get("score")
            score_s = f"{score:+.1f}" if isinstance(score, (int, float)) else "—"
            wellness = (snap.get("wellness") or {}).get("streaks") or []
            workout = next((s for s in wellness if s.get("kind") == "workout"), {})
            wk = workout.get("current", 0)
            title = f"ULTRON  score {score_s}  workout {wk}d"
            if self._tray is not None:
                self._tray.set_title(title)
        except Exception:  # noqa: BLE001
            logger.exception("tray title update failed")

    async def _tick_loop(self) -> None:
        logger.info("hud tick=%ss", self._cfg.tick_seconds)
        while not self._stop.is_set():
            try:
                await self._publish_tick()
            except Exception:  # noqa: BLE001
                logger.exception("hud tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.tick_seconds)
            except asyncio.TimeoutError:
                pass

    # ── Tray menu callbacks (run in tray thread) ───────────────────────

    def _menu_open_chat(self) -> None:
        if self._bridge is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._bridge.publish("hud_request_open_chat", {}),
            self._loop,
        )

    def _menu_quit(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._stop.set)

    # ── WS lifecycle ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start hud service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["hud_status_request", *_RESULT_KINDS],
            role="hud",
        )
        self._loop = asyncio.get_running_loop()
        if self._cfg.enable_tray:
            self._tray = TrayIcon(
                on_open_chat=self._menu_open_chat,
                on_quit=self._menu_quit,
            )
            self._tray.start()
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("HudService starting — tray=%s",
                    bool(self._tray and self._tray.available))
        try:
            await self._bridge.run_forever()
        finally:
            self._stop.set()
            if self._tick_task:
                self._tick_task.cancel()
                try:
                    await self._tick_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._tray is not None:
                self._tray.stop()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        if kind == "hud_status_request":
            # Fan out so the publish-then-await chain doesn't deadlock
            # this very bridge's recv loop.
            asyncio.create_task(self._publish_tick_safely())
            return
        # Dispatch result events to the next pending round-trip waiter.
        if kind in self._waiters:
            queue = self._waiters[kind]
            payload = event.get("payload") or {}
            while queue:
                fut = queue.pop(0)
                if not fut.done():
                    fut.set_result(payload)
                    return

    async def _publish_tick_safely(self) -> None:
        try:
            await self._publish_tick()
        except Exception:  # noqa: BLE001
            logger.exception("on-demand hud tick failed")
