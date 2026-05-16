"""SysInfoService — publishes ``system_info`` on a steady tick.

Subscribes:
  - ``system_info_request`` → emits a fresh ``system_info`` immediately.

Publishes:
  - ``system_info`` — payload: time/battery/wifi/bluetooth/heard at the
                     time of the tick. Heavy fields (wifi SSID, bluetooth
                     device count) update on ``heavy_tick_seconds`` only;
                     between heavy ticks they carry the last-known value.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .collector import (
    collect_battery, collect_bluetooth, collect_time, collect_wifi,
)
from .config import SysInfoConfig

logger = logging.getLogger("ultron.sysinfo.service")


class SysInfoService:
    def __init__(self, config: SysInfoConfig) -> None:
        self._cfg = config
        self._bridge: Optional[UltronBridge] = None
        self._stop = asyncio.Event()
        self._tick_task: Optional[asyncio.Task[None]] = None
        # Cache for heavy fields so we don't re-shell every tick.
        self._wifi: dict[str, Any] = {"available": False}
        self._bluetooth: dict[str, Any] = {"available": False}
        self._last_heavy_at: float = 0.0

    async def snapshot(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        time_info, battery = await asyncio.gather(
            loop.run_in_executor(None, lambda: collect_time(self._cfg.timezone)),
            loop.run_in_executor(None, collect_battery),
        )
        now_mono = time.monotonic()
        if now_mono - self._last_heavy_at >= self._cfg.heavy_tick_seconds:
            self._wifi, self._bluetooth = await asyncio.gather(
                loop.run_in_executor(None, collect_wifi),
                loop.run_in_executor(None, collect_bluetooth),
            )
            self._last_heavy_at = now_mono
        return {
            "ts": time.time(),
            "time": time_info,
            "battery": battery,
            "wifi": self._wifi,
            "bluetooth": self._bluetooth,
        }

    async def _publish_tick(self) -> None:
        if self._bridge is None:
            return
        snap = await self.snapshot()
        await self._bridge.publish("system_info", snap)

    async def _tick_loop(self) -> None:
        logger.info("sysinfo tick=%ss heavy=%ss",
                    self._cfg.tick_seconds, self._cfg.heavy_tick_seconds)
        while not self._stop.is_set():
            try:
                await self._publish_tick()
            except Exception:  # noqa: BLE001
                logger.exception("sysinfo tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start sysinfo")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["system_info_request"],
            role="sysinfo",
        )
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("SysInfoService starting")
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

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("kind") == "system_info_request":
            asyncio.create_task(self._publish_tick())
