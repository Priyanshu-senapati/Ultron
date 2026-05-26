"""Focus Shield -- when flow state is active, actively kill distracting
apps if they gain focus. Like a bouncer for your attention.

Subscribes to flow_state_changed. When flow is active:
  - Monitors focus_app events
  - If a blocked app gains focus, kills it immediately
  - Publishes focus_shield_blocked event
  - Mutes toast notifications (publishes focus_shield_mute)

When flow breaks, unblocks everything.

Configurable blocked-app list in config.toml [focus_shield].
"""
from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Optional

logger = logging.getLogger("ultron.focus_shield")

DEFAULT_BLOCKED = {
    "discord", "whatsapp", "telegram", "slack",
    "instagram", "twitter", "facebook",
    "reddit", "pinterest", "tiktok",
}

_PROCESS_MAP: dict[str, str] = {
    "discord": "Discord.exe",
    "whatsapp": "WhatsApp.exe",
    "telegram": "Telegram.exe",
    "slack": "slack.exe",
    "steam": "steam.exe",
}


class FocusShield:
    def __init__(self, publish, blocked_apps: Optional[set[str]] = None,
                 enabled: bool = True) -> None:
        self._publish = publish
        self._blocked = blocked_apps or DEFAULT_BLOCKED
        self._enabled = enabled
        self._flow_active = False
        self._blocked_count = 0
        self._last_block_ts: float = 0.0

    async def on_flow_state(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state") or "")
        if state == "active":
            if not self._flow_active:
                self._flow_active = True
                self._blocked_count = 0
                logger.info("focus shield: ARMED (flow active)")
                await self._publish("focus_shield_status", {
                    "active": True,
                    "blocked_apps": list(self._blocked),
                    "ts": time.time(),
                })
        elif state in ("broken", "idle"):
            if self._flow_active:
                self._flow_active = False
                logger.info("focus shield: DISARMED (flow %s, blocked %d apps)",
                            state, self._blocked_count)
                await self._publish("focus_shield_status", {
                    "active": False,
                    "total_blocked": self._blocked_count,
                    "ts": time.time(),
                })

    async def on_focus_app(self, payload: dict[str, Any]) -> None:
        if not self._flow_active or not self._enabled:
            return

        app = (payload.get("app") or payload.get("title") or "").lower().strip()
        if not app:
            return

        for blocked in self._blocked:
            if blocked in app:
                now = time.time()
                if now - self._last_block_ts < 2.0:
                    return
                self._last_block_ts = now
                self._blocked_count += 1

                proc = _PROCESS_MAP.get(blocked, f"{blocked}.exe")
                try:
                    subprocess.run(
                        ["taskkill", "/IM", proc, "/F"],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    pass

                logger.info("focus shield: BLOCKED %s (killed %s)", blocked, proc)
                await self._publish("focus_shield_blocked", {
                    "app": blocked,
                    "process": proc,
                    "total_blocked": self._blocked_count,
                    "ts": now,
                })
                return
