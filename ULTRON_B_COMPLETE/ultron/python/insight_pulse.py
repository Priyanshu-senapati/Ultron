"""
insight_pulse.py — Module O Python sidecar (LLaVA visual context inference).

Subscribes to ``screenshot_captured`` events on the ULTRON WS bridge. For
each new screenshot:

1. Reads the PNG from disk (the path is in the event payload).
2. Base64-encodes the bytes.
3. POSTs to Ollama's ``/api/generate`` with the LLaVA model and a tight
   one-phrase prompt.
4. Cleans the response (lowercase, strip punctuation, trim length).
5. Publishes the label back over the bridge as a custom event with
   ``kind = "visual_label"``. The Rust sidecar
   (``ultron-insight-pulse``) consumes this and folds it into the next
   ``InsightSnapshot``.

Behavioural rules
-----------------

- **Rate-limit:** at most one inference per ``min_interval_secs`` (default
  10 s). Events arriving inside the window are silently skipped — the
  Rust sidecar's staleness logic (drop label after 120 s) is the source
  of truth for "is this still relevant", so we'd rather skip than queue.
- **Never crash.** Ollama unreachable, file missing, model timing out,
  malformed JSON, decoding errors — all logged and swallowed. The
  process loops forever until you Ctrl-C it.
- **Privacy:** the daemon already gates screenshot capture behind config.
  We don't open any other files or send anywhere besides ``localhost``.

Configuration is via environment variables (see ``__main__`` block).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from ultron_bridge import UltronBridge

logger = logging.getLogger("ultron.insight_pulse.llava")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_LLAVA_MODEL = "llava:7b"
DEFAULT_MIN_INTERVAL_SECS = 10.0
DEFAULT_HTTP_TIMEOUT_SECS = 60.0
DEFAULT_LABEL_MAX_CHARS = 80

PROMPT = (
    "Describe what the user is currently doing on screen in one short phrase. "
    "Focus on the task, not UI details. Examples: "
    "'writing python code', 'reading documentation', 'browsing news', "
    "'terminal with error output', 'video call', 'spreadsheet work'. "
    "Reply with ONLY the phrase, no punctuation, no explanation."
)

# Pattern for cleaning the model's response.
_PUNCT_RE = re.compile(r"[^\w\s\-]+")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Label cleaner — exposed so tests can pin its semantics
# ---------------------------------------------------------------------------


def clean_label(raw: str, max_chars: int = DEFAULT_LABEL_MAX_CHARS) -> str:
    """Normalise a model response to a short, predictable label.

    Lowercases, strips punctuation (keeping hyphens for compound words),
    collapses whitespace, truncates to ``max_chars``. Returns an empty
    string for empty input — callers decide whether to publish.
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    # Drop common leading boilerplate the model sometimes emits despite
    # the prompt ("the user is...", "this shows...").
    for prefix in ("the user is ", "the user ", "this shows ", "i see "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if len(s) > max_chars:
        s = s[:max_chars].rstrip()
    return s


# ---------------------------------------------------------------------------
# LLaVA client — async; tests inject their own httpx.AsyncClient
# ---------------------------------------------------------------------------


class LlavaClient:
    """Thin async wrapper around Ollama's ``/api/generate`` endpoint.

    Holds an ``httpx.AsyncClient`` so connection pooling works across
    consecutive calls. Use ``await client.aclose()`` at shutdown — the
    sidecar does this in its ``finally`` block.
    """

    def __init__(
        self,
        url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_LLAVA_MODEL,
        timeout_secs: float = DEFAULT_HTTP_TIMEOUT_SECS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout_secs = timeout_secs
        # Tests pass in a pre-built client with a MockTransport. Production
        # gets a default one with a sane timeout.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_secs)
        )
        self._owns_client = client is None

    async def describe(self, image_bytes: bytes) -> Optional[str]:
        """Return a one-phrase description or ``None`` on any failure.

        Cleaning is applied here — callers receive a ready-to-publish
        label or ``None``.
        """
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "prompt": PROMPT,
            "images": [b64],
            "stream": False,
        }
        try:
            resp = await self._client.post(self.url, json=body)
        except httpx.HTTPError as exc:
            logger.warning("ollama request failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "ollama returned %d: %s", resp.status_code, resp.text[:200]
            )
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("ollama returned non-json: %s", exc)
            return None
        raw = data.get("response")
        if not isinstance(raw, str):
            logger.warning("ollama response missing 'response' string: %r", data)
            return None
        label = clean_label(raw)
        if not label:
            logger.debug("ollama returned empty/unusable label: %r", raw)
            return None
        return label

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Sidecar — ties bridge + rate limiter + LLaVA client together
# ---------------------------------------------------------------------------


class InsightPulseSidecar:
    """Glue: WS bridge handler → screenshot event → LLaVA → publish label."""

    def __init__(
        self,
        bridge: UltronBridge,
        llava: LlavaClient,
        min_interval_secs: float = DEFAULT_MIN_INTERVAL_SECS,
        time_source: Optional[callable] = None,
    ) -> None:
        self.bridge = bridge
        self.llava = llava
        self.min_interval_secs = min_interval_secs
        # Tests inject a frozen clock here; production uses time.monotonic.
        self._now = time_source or time.monotonic
        # -inf so the very first event always passes the rate-limit gate
        # regardless of what clock the caller injected. `0.0` would break
        # tests that pin their synthetic clock to 0.0 for the first event.
        self._last_inference_at: float = float("-inf")
        self._lock = asyncio.Lock()

    async def handle_event(self, event: dict) -> None:
        """Dispatch for one parsed WS event frame. The bridge calls this
        for every ``op:event`` frame it receives."""
        if event.get("kind") != "screenshot_captured":
            return
        payload = event.get("payload") or {}
        path = payload.get("path")
        ts_ms = payload.get("ts_unix_ms")
        if not isinstance(path, str) or not path:
            logger.warning("screenshot_captured missing path: %r", payload)
            return

        # Rate-limit gate. Holding the lock means two events that fire
        # simultaneously can't both pass the check.
        async with self._lock:
            now = self._now()
            elapsed = now - self._last_inference_at
            if elapsed < self.min_interval_secs:
                logger.debug(
                    "rate-limit skip: %.2fs since last (min=%.1fs) path=%s",
                    elapsed,
                    self.min_interval_secs,
                    path,
                )
                return
            self._last_inference_at = now

        await self._infer_and_publish(path, ts_ms)

    async def _infer_and_publish(self, path: str, ts_ms: Optional[int]) -> None:
        try:
            image_bytes = await asyncio.to_thread(_read_file_bytes, path)
        except FileNotFoundError:
            logger.warning("screenshot file missing: %s", path)
            return
        except OSError as exc:
            logger.warning("could not read %s: %s", path, exc)
            return

        if not image_bytes:
            logger.warning("empty screenshot file: %s", path)
            return

        label = await self.llava.describe(image_bytes)
        if not label:
            return

        payload = {"label": label}
        if isinstance(ts_ms, int):
            payload["screenshot_ts"] = ts_ms
        ok = await self.bridge.publish("visual_label", payload)
        if ok:
            logger.info("visual_label: %s", label)
        else:
            logger.warning("visual_label publish dropped: %s", label)


def _read_file_bytes(path: str) -> bytes:
    """Sync helper, run on a thread via ``asyncio.to_thread``."""
    return Path(path).read_bytes()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_from_env() -> tuple[UltronBridge, InsightPulseSidecar]:
    """Read environment + construct the wiring. Exposed for tests."""
    url = os.environ.get("ULTRON_WS_URL", "ws://127.0.0.1:9420/ws")
    token = os.environ.get("ULTRON_TOKEN")
    if not token:
        sys.exit(
            "ULTRON_TOKEN not set — read it from %APPDATA%/ULTRON/config.toml"
        )
    model = os.environ.get("ULTRON_LLAVA_MODEL", DEFAULT_LLAVA_MODEL)
    ollama_url = os.environ.get("ULTRON_OLLAMA_URL", DEFAULT_OLLAMA_URL)
    min_interval = float(
        os.environ.get("ULTRON_LLAVA_MIN_INTERVAL_SECS", DEFAULT_MIN_INTERVAL_SECS)
    )

    llava = LlavaClient(url=ollama_url, model=model)
    # The bridge needs a handler; we wire it via a forward reference that
    # the sidecar fills in once constructed.
    handler_slot: dict = {"fn": None}

    async def _on_event(ev: dict) -> None:
        fn = handler_slot["fn"]
        if fn is not None:
            await fn(ev)

    bridge = UltronBridge(
        url=url,
        token=token,
        on_event=_on_event,
        subscribe_to=["screenshot_captured"],
        role="insight-pulse-llava",
    )
    sidecar = InsightPulseSidecar(
        bridge=bridge,
        llava=llava,
        min_interval_secs=min_interval,
    )
    handler_slot["fn"] = sidecar.handle_event
    return bridge, sidecar


async def _main() -> None:
    bridge, sidecar = _build_from_env()
    logger.info(
        "insight_pulse llava sidecar starting — model=%s min_interval=%.1fs",
        sidecar.llava.model,
        sidecar.min_interval_secs,
    )
    try:
        await bridge.run_forever()
    finally:
        await sidecar.llava.aclose()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nbye.")
