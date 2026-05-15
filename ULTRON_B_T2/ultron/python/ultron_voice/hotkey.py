"""Global hotkey listener.

Uses the `keyboard` library, which on Windows hooks into the low-level
keyboard input chain via WH_KEYBOARD_LL (same mechanism as ULTRON-core's
input monitor). On Linux it requires root or input-group membership —
on macOS it needs accessibility permissions. For Priyanshu's Windows
target this Just Works once the process has the right privileges.

Threading model
---------------

The keyboard library runs its own daemon thread that pumps callbacks
synchronously. We bridge those callbacks back into asyncio via
``asyncio.run_coroutine_threadsafe`` — the caller supplies the loop
explicitly so we don't get tangled with the wrong one.

Press vs. release
-----------------

Both are exposed because the spec supports either "hold to talk" or
"tap to start, tap to stop" UX. The orchestrator decides which to wire.
Currently:

- ``on_press`` → start recording
- ``on_release`` → end recording early (in addition to VAD silence)

Permission failures
-------------------

On Windows we shouldn't hit any. On other platforms, ``keyboard.add_hotkey``
may raise ``ImportError`` or ``RuntimeError`` if the user can't hook the
input device. The orchestrator's graceful-degradation rule kicks in: log
the error and continue with clap-only activation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("ultron.voice.hotkey")


# Coroutine factories the listener invokes — they return coroutines that
# get scheduled on the asyncio loop.
PressCallback = Callable[[], Awaitable[None]]


class HotkeyListener:
    """Wraps the `keyboard` library and dispatches press/release into asyncio.

    Construct on the asyncio thread, call ``start()`` from anywhere.
    The internal thread is a daemon — process exit kills it cleanly.
    """

    def __init__(
        self,
        hotkey: str,
        on_press: PressCallback,
        on_release: PressCallback,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        escape_key: str = "esc",
        on_escape: Optional[PressCallback] = None,
    ) -> None:
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self.escape_key = escape_key
        self.on_escape = on_escape
        # If `loop` is not passed, we resolve it at start() time — by
        # then the orchestrator will have one running.
        self._loop = loop
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._hotkey_handle = None
        self._escape_handle = None

    def start(self) -> None:
        """Begin listening. Idempotent — second call is a no-op."""
        if self._thread is not None:
            return
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="HotkeyListener", daemon=True
        )
        self._thread.start()
        logger.info(
            "hotkey listener started: hotkey=%r escape=%r",
            self.hotkey,
            self.escape_key,
        )

    def stop(self) -> None:
        """Remove the hooks and wait for the worker thread to exit.

        Called during graceful shutdown. Safe to call multiple times.
        """
        if self._thread is None:
            return
        self._stop.set()
        try:
            import keyboard  # type: ignore[import-not-found]

            if self._hotkey_handle is not None:
                keyboard.remove_hotkey(self._hotkey_handle)
            if self._escape_handle is not None:
                keyboard.remove_hotkey(self._escape_handle)
        except Exception as exc:
            # Stopping is best-effort — if the library is gone we just exit.
            logger.debug("hotkey teardown error (ignored): %s", exc)
        # The keyboard library uses a daemon thread of its own; we don't
        # control its lifecycle. Our worker thread is a no-op loop;
        # signalling stop is enough.
        self._thread = None

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            import keyboard  # type: ignore[import-not-found]
        except Exception as exc:
            logger.error(
                "keyboard library import failed (%s) — voice activation "
                "by hotkey will not work; fall back to clap mode.",
                exc,
            )
            return

        try:
            self._hotkey_handle = keyboard.add_hotkey(
                self.hotkey,
                lambda: self._dispatch(self.on_press),
                trigger_on_release=False,
                suppress=False,
            )
            # The keyboard library's hotkey API doesn't expose a clean
            # release callback alongside press — but it does expose
            # per-key events via `on_release_key`. We register one for
            # the *last* key in the combo so "Ctrl+Shift+Space release"
            # fires when Space comes back up.
            last_key = self.hotkey.split("+")[-1].strip()
            keyboard.on_release_key(
                last_key,
                lambda _event: self._dispatch(self.on_release),
                suppress=False,
            )
            if self.on_escape is not None:
                self._escape_handle = keyboard.add_hotkey(
                    self.escape_key,
                    lambda: self._dispatch(self.on_escape),  # type: ignore[arg-type]
                    suppress=False,
                )
        except Exception as exc:
            logger.error(
                "failed to register hotkey %r: %s — clap-only mode.",
                self.hotkey,
                exc,
            )
            return

        # Idle wait. The keyboard library's hooks run on its own thread;
        # ours only exists to hold the registration alive and to give
        # `stop()` a thread to join on.
        while not self._stop.is_set():
            self._stop.wait(timeout=0.5)

    def _dispatch(self, fn: PressCallback) -> None:
        """Schedule the coroutine on the asyncio loop from this thread."""
        if self._loop is None or not self._loop.is_running():
            logger.debug("loop not running; dropping hotkey event")
            return
        try:
            asyncio.run_coroutine_threadsafe(fn(), self._loop)
        except Exception as exc:
            # Never let a callback failure kill the listener.
            logger.error("hotkey dispatch failed: %s", exc)
