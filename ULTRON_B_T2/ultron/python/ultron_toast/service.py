"""ToastService — subscribes to a curated set of bus events and surfaces
significant ones as Windows toasts.

Subscribes:
  - ``wellness_nudge``
  - ``flow_state_changed``      (ACTIVE → BROKEN with substantial duration)
  - ``tuning_suggestion``       (one per cool-down window)
  - ``self_reflection_written``
  - ``readiness_score_update``  (on bucket transitions only)
  - ``voice_shutdown_initiated``

Does NOT publish anything — it's a pure consumer. The router decides
whether each event warrants a toast and the service launches it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import ToastConfig
from .notifier import show
from .router import ToastRouter, ToastSpec

logger = logging.getLogger("ultron.toast.service")


class ToastService:
    def __init__(self, config: ToastConfig) -> None:
        self._cfg = config
        self._router = ToastRouter(config)
        self._bridge: Optional[UltronBridge] = None

    @property
    def router(self) -> ToastRouter:
        return self._router

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        try:
            spec: Optional[ToastSpec] = self._router.route(kind, payload)
        except Exception:  # noqa: BLE001
            logger.exception("toast router failed for kind=%s", kind)
            return
        if spec is None:
            return
        # Run subprocess in a thread so a slow PowerShell start doesn't
        # block the event loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: show(spec.title, spec.body, spec.footer),
        )

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start toast service")
        if not self._cfg.enabled:
            logger.info("toast service disabled in config; idling")
            # Stay connected so a config edit + restart works without
            # the launcher thinking the process crashed.
            await asyncio.Event().wait()
            return
        self._bridge = UltronBridge(
            url=self._cfg.ws_url, token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=[
                "wellness_nudge",
                "flow_state_changed",
                "tuning_suggestion",
                "self_reflection_written",
                "readiness_score_update",
                "voice_shutdown_initiated",
            ],
            role="toast",
        )
        logger.info("ToastService starting")
        await self._bridge.run_forever()
