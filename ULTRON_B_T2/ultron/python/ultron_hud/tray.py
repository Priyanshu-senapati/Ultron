"""Optional Windows tray icon — gracefully no-ops if pystray missing.

The tray runs in a *separate thread* (pystray uses a blocking loop) and
communicates with the asyncio service via a thread-safe callable. We
keep the surface tiny: a couple of menu items, a tooltip text, and the
ability to swap that tooltip on every aggregator tick.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("ultron.hud.tray")


class TrayIcon:
    """Wraps a pystray icon when available; otherwise no-op."""

    def __init__(
        self,
        *,
        title: str = "ULTRON",
        on_open_chat: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        self._title = title
        self._on_open_chat = on_open_chat or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._icon: Any = None
        self._thread: Optional[threading.Thread] = None
        self._available = self._probe()

    @property
    def available(self) -> bool:
        return self._available

    def _probe(self) -> bool:
        try:
            import pystray  # type: ignore[import]  # noqa: F401
            from PIL import Image, ImageDraw  # type: ignore[import]  # noqa: F401
            return True
        except ImportError:
            logger.info("pystray/Pillow not installed — tray icon disabled")
            return False

    def _make_icon_image(self) -> Any:
        from PIL import Image, ImageDraw  # type: ignore[import]
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # Filled circle in ULTRON cyan; bold border.
        d.ellipse((4, 4, size - 4, size - 4), fill=(0, 240, 255, 255),
                  outline=(0, 0, 0, 255), width=3)
        d.text((size // 2 - 8, size // 2 - 10), "U", fill=(0, 0, 0, 255))
        return img

    def start(self) -> None:
        if not self._available:
            return
        try:
            import pystray  # type: ignore[import]
            image = self._make_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("ULTRON", lambda icon, item: None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open chat", self._open_chat),
                pystray.MenuItem("Quit HUD", self._quit),
            )
            self._icon = pystray.Icon("ultron", image, self._title, menu)
            self._thread = threading.Thread(
                target=self._icon.run, name="ultron-tray", daemon=True,
            )
            self._thread.start()
            logger.info("tray icon started")
        except Exception:  # noqa: BLE001
            logger.exception("failed to start tray icon — continuing without")
            self._available = False

    def set_title(self, title: str) -> None:
        if not self._available or self._icon is None:
            return
        try:
            self._icon.title = title[:127]
        except Exception:  # noqa: BLE001
            logger.exception("failed to set tray title")

    def stop(self) -> None:
        if not self._available or self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            logger.exception("failed to stop tray icon")

    # ── Menu callbacks (called in tray thread) ─────────────────────────

    def _open_chat(self, icon: Any, item: Any) -> None:
        try:
            self._on_open_chat()
        except Exception:  # noqa: BLE001
            logger.exception("on_open_chat failed")

    def _quit(self, icon: Any, item: Any) -> None:
        try:
            self._on_quit()
        except Exception:  # noqa: BLE001
            logger.exception("on_quit failed")
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
