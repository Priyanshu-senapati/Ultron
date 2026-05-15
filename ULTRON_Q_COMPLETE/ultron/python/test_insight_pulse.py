"""
Integration tests for the Module-O Python sidecar.

Run with::

    pip install -r python/requirements.txt
    pytest python/

What's covered
--------------

1. ``test_bridge_reconnects_on_drop`` — boots a mock WS server, lets the
   bridge connect, kills the server, asserts the bridge reconnects when
   the server comes back. Real ``websockets.serve``, real backoff path.

2. ``test_llava_result_posted_back`` — boots a mock WS server, injects a
   fake ``screenshot_captured`` event pointing at a real PNG on disk,
   mocks Ollama via ``httpx.MockTransport``, asserts the sidecar POSTs to
   Ollama and publishes a cleaned ``visual_label`` back through the
   bridge.

3. ``test_rate_limit_skips_close_screenshots`` — fires two
   ``screenshot_captured`` events 3 s apart (synthetic clock), asserts
   only one Ollama call is made.

Why this combination
--------------------

LLaVA we mock because Ollama isn't available in CI. The WS bridge we
*don't* mock — running a real local ``websockets`` server takes
milliseconds and exercises the actual handshake + subscribe flow the
production bridge depends on. The only piece we don't run is the
``ultron-core`` daemon itself; we play its role.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
import pytest_asyncio
import websockets

# `insight_pulse` lives in the same dir; pytest sets `python/` as
# rootdir via `conftest.py` (see below).
from insight_pulse import (
    DEFAULT_MIN_INTERVAL_SECS,
    InsightPulseSidecar,
    LlavaClient,
    clean_label,
)
from ultron_bridge import UltronBridge

logging.getLogger("ultron.bridge").setLevel(logging.WARNING)
logging.getLogger("ultron.insight_pulse.llava").setLevel(logging.WARNING)

# pytest-asyncio is in auto mode (see conftest.py), so async def tests
# are picked up automatically. No module-level pytestmark needed.


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


# A genuine 1x1 transparent PNG. Saved verbatim so the sidecar can open
# it with PIL/imghdr/whatever happens to be installed; we only need the
# bytes to be valid enough for `Path.read_bytes()` to return them.
TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100618c14e10000000049454e"
    "44ae426082"
)


class MockUltronServer:
    """Minimal stand-in for ``ultron-core``'s WS bridge.

    Accepts the hello handshake (any non-empty token), replies welcome,
    accepts optional subscribe, then waits. Tests push events via
    ``send_event`` and observe client-originated publishes via
    ``published``.
    """

    def __init__(self) -> None:
        self.server: Optional[websockets.WebSocketServer] = None
        self.port: int = 0
        self.connections: list[websockets.WebSocketServerProtocol] = []
        self.published: list[dict] = []
        self._connection_events: list[asyncio.Event] = []

    async def start(self) -> None:
        # port=0 → OS picks a free one; we read it back from `.sockets`.
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        sock = next(iter(self.server.sockets))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _handler(self, ws: websockets.WebSocketServerProtocol) -> None:
        # 1. Hello.
        try:
            hello_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            return
        hello = json.loads(hello_raw)
        if hello.get("op") != "hello" or not hello.get("token"):
            await ws.send(
                json.dumps(
                    {"op": "error", "code": "bad_hello", "message": "no hello"}
                )
            )
            return

        # 2. Welcome.
        await ws.send(
            json.dumps(
                {
                    "op": "welcome",
                    "server_version": "test",
                    "session_id": "test-session",
                }
            )
        )

        # 3. Pump anything the client sends. Subscribe / publish / etc.
        self.connections.append(ws)
        # Notify anyone waiting for "a connection arrived".
        for ev in self._connection_events:
            ev.set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = msg.get("op")
                if op == "subscribe":
                    continue
                if op == "publish":
                    self.published.append(msg)
                if op == "ping":
                    await ws.send(json.dumps({"op": "pong"}))
        except websockets.ConnectionClosed:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)

    async def send_event(self, kind: str, payload: dict) -> None:
        """Push an `op:event` to every live client."""
        frame = json.dumps(
            {
                "op": "event",
                "kind": kind,
                "ts": "2026-05-11T00:00:00Z",
                "payload": payload,
            }
        )
        # Snapshot list to avoid mutation-during-iteration.
        for ws in list(self.connections):
            try:
                await ws.send(frame)
            except websockets.ConnectionClosed:
                pass

    def new_connection_event(self) -> asyncio.Event:
        """Returns an Event that fires when the next client connects."""
        ev = asyncio.Event()
        self._connection_events.append(ev)
        return ev


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    """Poll until ``predicate()`` is truthy, or raise TimeoutError."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if loop.time() > deadline:
            raise asyncio.TimeoutError(
                f"predicate not satisfied within {timeout}s"
            )
        await asyncio.sleep(interval)


@pytest_asyncio.fixture
async def server():
    s = MockUltronServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


# ---------------------------------------------------------------------------
# Sanity: clean_label is well-defined
# ---------------------------------------------------------------------------


def test_clean_label_lowercases_and_strips_punct():
    assert clean_label("Writing Python Code.") == "writing python code"
    assert clean_label("The user is reading docs!") == "reading docs"
    assert clean_label("'terminal with error output'") == "terminal with error output"
    assert clean_label("") == ""
    assert clean_label("    ") == ""


def test_clean_label_truncates():
    label = clean_label("x" * 200, max_chars=20)
    assert len(label) <= 20


# ---------------------------------------------------------------------------
# Test 1 — bridge reconnect
# ---------------------------------------------------------------------------


async def test_bridge_reconnects_on_drop(server: MockUltronServer):
    """After the WS server drops the connection, the bridge should
    reconnect within the first backoff window."""
    # Tighten the backoff schedule so the test finishes in ~2 s rather
    # than 30+ s. We do this by patching the class constant on a *copy*
    # via subclassing — keeps prod defaults untouched.
    class FastBridge(UltronBridge):
        BACKOFF_SCHEDULE = (0.1, 0.2, 0.4)

    received: list[dict] = []

    async def on_event(ev: dict) -> None:
        received.append(ev)

    first_conn = server.new_connection_event()
    bridge = FastBridge(
        url=server.url,
        token="dummy",
        on_event=on_event,
        subscribe_to=[],
        role="test",
    )
    runner = asyncio.create_task(bridge.run_forever())
    try:
        # Wait for first connection.
        await asyncio.wait_for(first_conn.wait(), timeout=5.0)
        # Deliver one event and make sure the wiring works end-to-end.
        await server.send_event("heartbeat", {"tension": 0.1, "uptime_secs": 1})
        await _wait_for(lambda: len(received) >= 1)

        # Kill the server side of the connection. The bridge should
        # detect the drop and back off + reconnect.
        second_conn = server.new_connection_event()
        for ws in list(server.connections):
            await ws.close()

        await asyncio.wait_for(second_conn.wait(), timeout=5.0)

        # Verify the reconnected session is usable: send another event,
        # ensure it lands.
        await server.send_event("heartbeat", {"tension": 0.2, "uptime_secs": 2})
        await _wait_for(lambda: len(received) >= 2, timeout=3.0)
        assert received[-1]["payload"]["tension"] == 0.2
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Test 2 — LLaVA result posted back through the bridge
# ---------------------------------------------------------------------------


async def test_llava_result_posted_back(server: MockUltronServer, tmp_path: Path):
    """Inject a ``screenshot_captured`` event; assert the sidecar POSTs
    to Ollama once and publishes a ``visual_label`` back via WS."""
    # Write a real PNG to disk where the sidecar can read it.
    png_path = tmp_path / "shot.png"
    png_path.write_bytes(TINY_PNG)

    ollama_calls: list[httpx.Request] = []

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        ollama_calls.append(request)
        # Echo a noisy response so the cleaner has work to do.
        return httpx.Response(
            status_code=200,
            json={
                "model": "llava:7b",
                "response": "The user is writing Python code!",
                "done": True,
            },
        )

    mock_transport = httpx.MockTransport(ollama_handler)
    mock_client = httpx.AsyncClient(transport=mock_transport)
    llava = LlavaClient(
        url="http://mocked.invalid/api/generate",
        model="llava:test",
        client=mock_client,
    )

    # Build a sidecar manually so we can use our small min_interval.
    class FastBridge(UltronBridge):
        BACKOFF_SCHEDULE = (0.1, 0.2, 0.4)

    handler_slot: dict = {"fn": None}

    async def _on_event(ev: dict) -> None:
        fn = handler_slot["fn"]
        if fn is not None:
            await fn(ev)

    bridge = FastBridge(
        url=server.url,
        token="dummy",
        on_event=_on_event,
        subscribe_to=["screenshot_captured"],
        role="test-llava",
    )
    sidecar = InsightPulseSidecar(
        bridge=bridge,
        llava=llava,
        min_interval_secs=0.1,  # tight so the test doesn't drag
    )
    handler_slot["fn"] = sidecar.handle_event

    conn = server.new_connection_event()
    runner = asyncio.create_task(bridge.run_forever())
    try:
        await asyncio.wait_for(conn.wait(), timeout=5.0)
        # Give the subscribe message a moment to settle on the server.
        await asyncio.sleep(0.1)

        await server.send_event(
            "screenshot_captured",
            {
                "path": str(png_path),
                "width": 1,
                "height": 1,
                "reason": "periodic",
                "ts_unix_ms": 1_700_000_000_000,
            },
        )

        # Wait until the server has seen a publish from the sidecar.
        await _wait_for(
            lambda: any(p.get("kind") == "visual_label" for p in server.published),
            timeout=5.0,
        )
        assert len(ollama_calls) == 1
        published = [p for p in server.published if p.get("kind") == "visual_label"]
        assert len(published) == 1
        payload = published[0]["payload"]
        # Cleaner should have stripped "The user is " + "!".
        assert payload["label"] == "writing python code"
        assert payload["screenshot_ts"] == 1_700_000_000_000
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        await llava.aclose()


# ---------------------------------------------------------------------------
# Test 3 — rate limit suppresses the second of two close screenshots
# ---------------------------------------------------------------------------


async def test_rate_limit_skips_close_screenshots(
    server: MockUltronServer, tmp_path: Path
):
    """Fire two ``screenshot_captured`` events with a synthetic clock
    showing 3 s between them; with a 10 s min interval, only one
    inference must run."""
    png_path = tmp_path / "shot.png"
    png_path.write_bytes(TINY_PNG)

    ollama_calls: list[httpx.Request] = []

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        ollama_calls.append(request)
        return httpx.Response(
            status_code=200,
            json={"response": "terminal output", "done": True},
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(ollama_handler))
    llava = LlavaClient(
        url="http://mocked.invalid/api/generate",
        model="llava:test",
        client=mock_client,
    )

    # Injectable monotonic clock so we don't have to actually wait 3 s.
    clock = {"now": 0.0}

    class FastBridge(UltronBridge):
        BACKOFF_SCHEDULE = (0.1, 0.2, 0.4)

    handler_slot: dict = {"fn": None}

    async def _on_event(ev: dict) -> None:
        fn = handler_slot["fn"]
        if fn is not None:
            await fn(ev)

    bridge = FastBridge(
        url=server.url,
        token="dummy",
        on_event=_on_event,
        subscribe_to=["screenshot_captured"],
        role="test-ratelimit",
    )
    sidecar = InsightPulseSidecar(
        bridge=bridge,
        llava=llava,
        min_interval_secs=DEFAULT_MIN_INTERVAL_SECS,  # 10 s
        time_source=lambda: clock["now"],
    )
    handler_slot["fn"] = sidecar.handle_event

    conn = server.new_connection_event()
    runner = asyncio.create_task(bridge.run_forever())
    try:
        await asyncio.wait_for(conn.wait(), timeout=5.0)
        await asyncio.sleep(0.1)

        # First event at t=0 — must trigger.
        clock["now"] = 0.0
        await server.send_event(
            "screenshot_captured",
            {"path": str(png_path), "width": 1, "height": 1,
             "reason": "periodic", "ts_unix_ms": 1},
        )
        await _wait_for(lambda: len(ollama_calls) == 1, timeout=5.0)

        # Second event at t=3 — under the 10 s threshold, must be skipped.
        clock["now"] = 3.0
        await server.send_event(
            "screenshot_captured",
            {"path": str(png_path), "width": 1, "height": 1,
             "reason": "periodic", "ts_unix_ms": 2},
        )
        # Give the handler time to process and (correctly) decide to skip.
        await asyncio.sleep(0.5)
        assert len(ollama_calls) == 1, (
            f"expected 1 ollama call after rate-limit skip, saw {len(ollama_calls)}"
        )

        # Third event past the threshold — must trigger again.
        clock["now"] = 11.0
        await server.send_event(
            "screenshot_captured",
            {"path": str(png_path), "width": 1, "height": 1,
             "reason": "periodic", "ts_unix_ms": 3},
        )
        await _wait_for(lambda: len(ollama_calls) == 2, timeout=5.0)
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        await llava.aclose()
