"""
ultron_bridge.py — Shared WebSocket client for all ULTRON Python modules.

Every Python sidecar (Module O's LLaVA inference, future B/C/D modules)
imports this same class instead of rolling its own connection logic. That
way there's exactly one place to fix when the WS handshake evolves.

Usage
-----

    from ultron_bridge import UltronBridge

    async def my_handler(event: dict) -> None:
        if event["kind"] == "screenshot_captured":
            ...

    bridge = UltronBridge(
        url="ws://127.0.0.1:9420/ws",
        token="<from %APPDATA%/ULTRON/config.toml>",
        on_event=my_handler,
        subscribe_to=["screenshot_captured"],   # None / [] = all events
        role="insight-pulse-llava",
    )
    asyncio.run(bridge.run_forever())

Behavioural notes
-----------------

- The daemon's WS protocol uses the field name ``op`` on every frame —
  see ``crates/ultron-types/src/messages.rs``. Older drafts of this
  bridge used ``type``; that's wrong. We always send ``op``.
- Reconnect is exponential: 1, 2, 4, 8, 16, 30, 30, 30… seconds.
- The handshake must complete within ``handshake_timeout`` seconds
  (default 10) or we treat it as a connection failure and back off.
- ``on_event`` is awaited for **every** delivered event. If it raises,
  we log and continue — one bad handler must not kill the bridge.
- ``send`` is safe to call from any task; if there's no live connection
  the message is dropped (with a warning) rather than queued.
- Compatible with Python 3.11+ (uses ``asyncio.timeout``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

logger = logging.getLogger("ultron.bridge")

EventHandler = Callable[[dict], Awaitable[None]]


class UltronBridge:
    """Reconnecting WS client for ULTRON Python modules.

    Lifetime is owned by the caller via ``run_forever``. Cancel that task
    to stop. The bridge does **not** install signal handlers — your
    sidecar's ``main`` decides what KeyboardInterrupt should do.
    """

    # Exponential backoff schedule, in seconds. Capped at the last value.
    BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

    def __init__(
        self,
        url: str,
        token: str,
        on_event: EventHandler,
        subscribe_to: Optional[list[str]] = None,
        role: str = "python-bridge",
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        handshake_timeout: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("url is required")
        if not token:
            raise ValueError("token is required")
        if not callable(on_event):
            raise TypeError("on_event must be an async callable")
        self.url = url
        self.token = token
        self.on_event = on_event
        self.subscribe_to: list[str] = list(subscribe_to or [])
        self.role = role
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.handshake_timeout = handshake_timeout

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ---- public API ------------------------------------------------------

    async def run_forever(self) -> None:
        """Connect, handle events, reconnect on drop. Returns only on
        cancellation."""
        attempt = 0
        while not self._closed:
            try:
                await self._connect_and_run()
                attempt = 0  # successful session → reset backoff
            except asyncio.CancelledError:
                logger.info("ultron_bridge cancelled — exiting")
                self._closed = True
                raise
            except Exception as exc:  # noqa: BLE001  (explicit catch, logs+continues)
                delay = self._backoff(attempt)
                logger.warning(
                    "ws connection failed (%s) — reconnecting in %.1fs",
                    exc,
                    delay,
                )
                attempt += 1
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    self._closed = True
                    raise

    async def send(self, msg: dict) -> bool:
        """Send a JSON message to the daemon. Returns True if dispatched,
        False if there was no live connection."""
        ws = self._ws
        if ws is None:
            logger.warning("send dropped — no live connection: %r", msg.get("op"))
            return False
        try:
            async with self._send_lock:
                await ws.send(json.dumps(msg))
            return True
        except ConnectionClosed:
            logger.warning("send dropped — connection closed mid-flight")
            return False

    async def publish(self, kind: str, payload: dict) -> bool:
        """Convenience helper for the most common send: a custom event."""
        return await self.send({"op": "publish", "kind": kind, "payload": payload})

    def close(self) -> None:
        """Cooperative shutdown — the next reconnect attempt will exit."""
        self._closed = True

    # ---- internals -------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        idx = min(attempt, len(self.BACKOFF_SCHEDULE) - 1)
        return self.BACKOFF_SCHEDULE[idx]

    async def _connect_and_run(self) -> None:
        logger.info("connecting to %s as role=%s", self.url, self.role)
        async with websockets.connect(
            self.url,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=8 * 1024 * 1024,  # 8 MiB — large enough for screenshot paths + metadata
        ) as ws:
            self._ws = ws
            try:
                await self._handshake(ws)
                if self.subscribe_to:
                    await ws.send(
                        json.dumps(
                            {"op": "subscribe", "kinds": list(self.subscribe_to)}
                        )
                    )
                await self._receive_loop(ws)
            finally:
                self._ws = None

    async def _handshake(self, ws: websockets.WebSocketClientProtocol) -> None:
        await ws.send(
            json.dumps({"op": "hello", "token": self.token, "role": self.role})
        )
        try:
            async with asyncio.timeout(self.handshake_timeout):
                raw = await ws.recv()
        except asyncio.TimeoutError as e:
            raise InvalidHandshake(
                f"no welcome within {self.handshake_timeout}s"
            ) from e
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InvalidHandshake(f"non-JSON welcome: {raw!r}") from e
        if msg.get("op") != "welcome":
            raise InvalidHandshake(f"expected welcome, got {msg!r}")
        logger.info(
            "ws connected — server=%s session=%s",
            msg.get("server_version"),
            msg.get("session_id"),
        )

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("non-JSON frame: %r", raw[:200])
                continue
            op = msg.get("op")
            if op == "event":
                try:
                    await self.on_event(msg)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  (explicit catch, logs+continues)
                    logger.exception("on_event handler raised — continuing")
            elif op == "pong":
                continue
            elif op == "error":
                logger.warning(
                    "server error: code=%s msg=%s",
                    msg.get("code"),
                    msg.get("message"),
                )
            elif op == "ack":
                logger.debug("ack: %r", msg)
            else:
                logger.debug("unhandled frame op=%r", op)


# Minimal standalone smoke test — confirms wiring is sane.
#
#   ULTRON_WS_URL=ws://127.0.0.1:9420/ws \
#   ULTRON_TOKEN=$(cat ...config.toml... | grep token) \
#       python python/ultron_bridge.py
#
# Will print one line per event for 30s then exit.
if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    url = os.environ.get("ULTRON_WS_URL", "ws://127.0.0.1:9420/ws")
    token = os.environ.get("ULTRON_TOKEN")
    if not token:
        sys.exit("set ULTRON_TOKEN (read it from %APPDATA%/ULTRON/config.toml)")

    counter = {"n": 0}

    async def _print(ev: dict) -> None:
        counter["n"] += 1
        print(f"  [{counter['n']:>4}] {ev.get('kind'):<24} {str(ev.get('payload'))[:80]}")

    bridge = UltronBridge(url=url, token=token, on_event=_print, role="bridge-smoketest")

    async def _run() -> None:
        task = asyncio.create_task(bridge.run_forever())
        try:
            await asyncio.sleep(30)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run())
        print(f"\n{counter['n']} events received in 30s.")
    except KeyboardInterrupt:
        pass
