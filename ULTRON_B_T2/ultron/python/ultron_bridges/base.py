"""Bridge ABC + the publish callback type.

A bridge represents one external service. The supervisor (`BridgesService`)
calls `start()` once at boot; the bridge owns its own asyncio task and is
expected to loop forever until `stop()` is called.

Why an ABC instead of a dataclass: each integration has wildly different
auth (OAuth PKCE, fine-grained PAT, OAuth2 with refresh, none), polling
cadence (5s for music, 60s for mail), and event shape. A common shape
would constrain more than it'd help; this base just pins lifecycle.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

# Bridges publish back to the WS bus via this callback (a thin wrapper
# around `UltronBridge.publish`). Passed in by the supervisor so bridges
# don't import the WS client directly — easier to mock in tests.
BridgePublishFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


class Bridge(abc.ABC):
    """Base class for one external-service integration."""

    # Subclasses must set this. Used in logs and in the per-bridge config
    # section name: `[bridges.<name>]` in config.toml.
    name: str = "unnamed"

    # Bus event kinds the bridge wants to consume. The supervisor unions
    # all subscriptions across bridges and routes inbound events to the
    # right bridge via `on_event`. Default: outbound-only.
    subscribed_kinds: tuple[str, ...] = ()

    def __init__(self, publish: BridgePublishFn) -> None:
        self._publish = publish
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self.log = logging.getLogger(f"ultron.bridges.{self.name}")

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Spawn the poll loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._safe_loop(), name=f"bridge:{self.name}")
        self.log.info("bridge started")

    async def stop(self) -> None:
        """Signal the loop to exit and await it."""
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            self._task = None
            self.log.info("bridge stopped")

    # ---- subclass hook --------------------------------------------------

    @abc.abstractmethod
    async def run(self) -> None:
        """Long-running poll loop. Subclasses implement.

        Should respect ``self._stop_event`` — either by checking it
        between sleeps or by racing it with ``asyncio.wait``.
        """
        raise NotImplementedError

    async def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Called by the supervisor when an event the bridge subscribed
        to arrives. Default: no-op. Bridges that want bidirectional
        control (e.g. Spotify play/pause/next via bus) override this and
        also set ``subscribed_kinds``."""
        return

    # ---- helpers --------------------------------------------------------

    async def publish(self, kind: str, payload: dict[str, Any]) -> bool:
        """Publish a typed event onto the WS bus."""
        return await self._publish(kind, payload)

    async def sleep(self, seconds: float) -> bool:
        """Sleep until either ``seconds`` elapse or stop is requested.

        Returns True if the sleep completed normally, False if stop fired
        — bridges should `return` on False to exit cleanly.
        """
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    # ---- internal -------------------------------------------------------

    async def _safe_loop(self) -> None:
        """Wrap subclass `run()` so a crash is logged, not propagated."""
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.log.exception("bridge crashed — supervisor will not restart it")
