"""BridgesService — supervises every enabled Bridge.

Owns the shared `UltronBridge` WS client and a list of `Bridge` instances.
Each bridge runs in its own asyncio task; if one crashes, the supervisor
logs it and lets the others continue (independent failure domains, same
invariant the rest of ULTRON's sidecars maintain).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ultron_bridge import UltronBridge

from .base import Bridge
from .config import BridgesConfig

logger = logging.getLogger("ultron.bridges.service")


class BridgesService:
    def __init__(self, cfg: BridgesConfig) -> None:
        self._cfg = cfg
        self._bridges: list[Bridge] = []
        self._ws: UltronBridge | None = None

    def register(self, bridge: Bridge) -> None:
        """Add a bridge to the supervised set. Call before `run()`."""
        self._bridges.append(bridge)

    async def _publish(self, kind: str, payload: dict[str, Any]) -> bool:
        """Bridge publish callback — adapts to the underlying WS client."""
        ws = self._ws
        if ws is None:
            logger.debug("publish %r dropped — no live ws connection", kind)
            return False
        return await ws.publish(kind, payload)

    async def _on_event(self, event: dict[str, Any]) -> None:
        # The bridges sidecar publishes outward; for now it does not
        # consume events. Bridges that need to react to other events
        # (e.g. browser_tab triggering a refresh) can override.
        return

    async def run(self) -> None:
        """Connect WS, spawn every registered bridge, wait forever."""
        if not self._cfg.ws_token:
            raise RuntimeError(
                "bridge.token not set in config.toml — cannot connect to WS"
            )

        self._ws = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._on_event,
            subscribe_to=[],
            role="ultron-bridges",
        )

        for b in self._bridges:
            # Inject the publish callback now that the WS client exists.
            b._publish = self._publish  # type: ignore[attr-defined]

        ws_task = asyncio.create_task(self._ws.run_forever(), name="bridges:ws")

        # Start each bridge concurrently. start() is idempotent and returns
        # quickly — the actual loops run in detached tasks owned by each
        # Bridge instance.
        for b in self._bridges:
            try:
                await b.start()
            except Exception:  # noqa: BLE001
                logger.exception("failed to start bridge %s", b.name)

        try:
            await ws_task
        finally:
            for b in self._bridges:
                try:
                    await b.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("error stopping bridge %s", b.name)
