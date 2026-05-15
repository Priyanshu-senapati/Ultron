"""Config for Module L (HUD)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


@dataclass
class HudConfig:
    ws_url: str
    ws_token: str

    # Aggregator tick: how often to publish hud_status_tick events.
    tick_seconds: int = 5

    # Enable the Windows tray icon (requires pystray).
    enable_tray: bool = True

    # When the tray icon is clicked, what should the menu show?
    show_score_in_title: bool = True


def load_hud_config(config_path: Path | None = None) -> HudConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    h = raw.get("hud", {}) if isinstance(raw.get("hud"), dict) else {}
    return HudConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        tick_seconds=int(h.get("tick_seconds", 5)),
        enable_tray=bool(h.get("enable_tray", True)),
        show_score_in_title=bool(h.get("show_score_in_title", True)),
    )
