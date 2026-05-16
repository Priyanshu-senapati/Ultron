"""Config for the System Info bridge."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
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
class SysInfoConfig:
    ws_url: str
    ws_token: str

    # Tick cadence. 5 s matches the existing HUD tick and is more than
    # frequent enough for battery / network state.
    tick_seconds: int = 5

    # Heavier checks (wifi SSID via netsh, bluetooth via Get-PnpDevice)
    # are spawn-heavy; do them at this slower cadence.
    heavy_tick_seconds: int = 30

    # IANA timezone for local-time formatting. Defaults to Asia/Kolkata
    # (single-user, India-resident project).
    timezone: str = "Asia/Kolkata"


def load_sysinfo_config(config_path: Path | None = None) -> SysInfoConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    s = raw.get("sysinfo", {}) if isinstance(raw.get("sysinfo"), dict) else {}
    return SysInfoConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        tick_seconds=int(s.get("tick_seconds", 5)),
        heavy_tick_seconds=int(s.get("heavy_tick_seconds", 30)),
        timezone=str(s.get("timezone", "Asia/Kolkata")),
    )
