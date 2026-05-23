"""Config for the Windows-toast bridge."""
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
class ToastConfig:
    ws_url: str
    ws_token: str

    # ── Master switch ─────────────────────────────────────────────────
    enabled: bool = True

    # ── Per-kind enable + min interval (seconds between toasts of the
    # same kind). Throttling stops a flapping signal from spamming the
    # notification centre.
    enable_wellness_nudge: bool = True
    enable_flow_break: bool = True
    enable_tuning_suggestion: bool = True
    enable_self_reflection: bool = True
    enable_readiness_bucket_change: bool = True
    enable_voice_shutdown: bool = True

    min_interval_wellness_nudge: float = 300.0     # 5 min
    min_interval_flow_break: float = 60.0
    min_interval_tuning_suggestion: float = 1800.0  # 30 min — these are heavy
    min_interval_self_reflection: float = 3600.0    # 1 hr (auto fires once/day anyway)
    min_interval_readiness_change: float = 600.0
    min_interval_voice_shutdown: float = 5.0

    # Below this flow-session duration we don't bother with a toast —
    # short flow blips aren't worth a notification.
    flow_break_min_minutes: float = 5.0


def load_toast_config(config_path: Path | None = None) -> ToastConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    t = raw.get("toast", {}) if isinstance(raw.get("toast"), dict) else {}
    return ToastConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        enabled=bool(t.get("enabled", True)),
        enable_wellness_nudge=bool(t.get("enable_wellness_nudge", True)),
        enable_flow_break=bool(t.get("enable_flow_break", True)),
        enable_tuning_suggestion=bool(t.get("enable_tuning_suggestion", True)),
        enable_self_reflection=bool(t.get("enable_self_reflection", True)),
        enable_readiness_bucket_change=bool(
            t.get("enable_readiness_bucket_change", True)),
        enable_voice_shutdown=bool(t.get("enable_voice_shutdown", True)),
        min_interval_wellness_nudge=float(t.get("min_interval_wellness_nudge", 300.0)),
        min_interval_flow_break=float(t.get("min_interval_flow_break", 60.0)),
        min_interval_tuning_suggestion=float(
            t.get("min_interval_tuning_suggestion", 1800.0)),
        min_interval_self_reflection=float(
            t.get("min_interval_self_reflection", 3600.0)),
        min_interval_readiness_change=float(
            t.get("min_interval_readiness_change", 600.0)),
        min_interval_voice_shutdown=float(t.get("min_interval_voice_shutdown", 5.0)),
        flow_break_min_minutes=float(t.get("flow_break_min_minutes", 5.0)),
    )
