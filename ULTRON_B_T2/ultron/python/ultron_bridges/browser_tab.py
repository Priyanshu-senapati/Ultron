"""Browser-tab bridge.

The "what page is the user on?" signal is hard to get from the OS — the
Chrome window title is truncated, and the OS only knows `Chrome.exe`.
Cleanest fix: a tiny Chrome/Edge extension running as a service worker
that POSTs `{url, title}` to a local HTTP endpoint every time the
focused tab changes. This bridge runs that endpoint and republishes the
data onto the WS bus as `browser_tab` events.

Why a raw asyncio HTTP server instead of aiohttp: keeps the dep tree
small. The protocol we need to speak is ~50 lines of code.

Browser extension lives in `tools/browser_extension/` — see its README
for install instructions (Load Unpacked in chrome://extensions).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from .base import Bridge, BridgePublishFn
from .config import BrowserTabConfig

logger = logging.getLogger("ultron.bridges.browser_tab")

# Cap each incoming request — extensions only send tiny JSON blobs.
MAX_BODY_BYTES = 32 * 1024


class BrowserTabBridge(Bridge):
    name = "browser_tab"

    def __init__(self, publish: BridgePublishFn | None, cfg: BrowserTabConfig) -> None:
        super().__init__(publish or (lambda k, p: _noop(k, p)))  # type: ignore[arg-type]
        self.cfg = cfg
        self._server: Optional[asyncio.base_events.Server] = None
        self._last_received: float = 0.0
        self._last_signature: Optional[str] = None

    async def run(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host=self.cfg.bind_host, port=self.cfg.bind_port
        )
        self.log.info(
            "browser-tab receiver listening on %s:%d",
            self.cfg.bind_host, self.cfg.bind_port,
        )
        try:
            # The server lives until stop is signalled.
            await self._stop_event.wait()
        finally:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve_one(reader, writer)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("connection handler error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return

        # Parse minimal HTTP.
        head_text = head.decode("latin-1", "ignore")
        lines = head_text.split("\r\n")
        if not lines:
            return
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            return

        headers: dict[str, str] = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                headers[k.strip().lower()] = v.strip()

        # CORS preflight — Chrome extension service workers send these
        # when posting JSON to a different origin.
        if method == "OPTIONS":
            await self._write(writer, 204, body=b"")
            return

        if method == "GET" and path.startswith("/ping"):
            await self._write(writer, 200, body=b'{"ok":true}', content_type="application/json")
            return

        if method != "POST" or not path.startswith("/ingest"):
            await self._write(writer, 404, body=b"not found")
            return

        try:
            length = min(int(headers.get("content-length", "0")), MAX_BODY_BYTES)
        except ValueError:
            length = 0

        body = b""
        if length > 0:
            try:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                await self._write(writer, 400, body=b"truncated")
                return

        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            await self._write(writer, 400, body=b"bad json")
            return

        url = str(data.get("url", "") or "")
        title = str(data.get("title", "") or "")
        active_since_ms = int(data.get("active_since_ms", 0) or 0)
        # Some pages have no title (about:blank); accept and pass through.
        signature = f"{url}|{title}"
        now = time.time()
        if signature != self._last_signature:
            self._last_signature = signature
            self._last_received = now
            await self.publish(
                "browser_tab",
                {
                    "url": url,
                    "title": title,
                    "active_since_ms": active_since_ms,
                    "ts_unix_ms": int(now * 1000),
                },
            )
        else:
            self._last_received = now

        await self._write(writer, 200, body=b'{"ok":true}', content_type="application/json")

    async def _write(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
            + f"Content-Type: {content_type}\r\n".encode("ascii")
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Access-Control-Allow-Origin: *\r\n"
            + b"Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n"
            + b"Access-Control-Allow-Headers: Content-Type\r\n"
            + b"Connection: close\r\n\r\n"
            + body
        )
        try:
            await writer.drain()
        except ConnectionResetError:
            pass


async def _noop(kind: str, payload: dict[str, Any]) -> bool:
    return False
