"""Config for the Self-Improvement / Self-Tuning service."""
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
class SelfTunerConfig:
    ws_url: str
    ws_token: str

    # ── Paths to read from ─────────────────────────────────────────────
    flow_db: Path = Path()
    interrupt_db: Path = Path()
    readiness_db: Path = Path()
    recall_db: Path = Path()

    # ── Output ─────────────────────────────────────────────────────────
    # Daily reflections live here as YYYY-MM-DD.md. Newest also gets
    # cached as latest.md for quick grep.
    reflection_dir: Path = Path()
    latest_md_path: Path = Path()

    # ── Cadence ────────────────────────────────────────────────────────
    # Auto-reflect once every 24h while running. The service also
    # exposes a self_reflect_request for manual / smoke triggering.
    reflection_interval_secs: float = 86400.0
    # Wait this long after boot before the first auto-reflection — gives
    # the running stack time to be representative of "today".
    boot_delay_secs: float = 600.0

    # ── Tool-usage observer ────────────────────────────────────────────
    # In-memory rolling window. Older audits are dropped.
    tool_usage_window_secs: float = 86400.0
    # A tool with at least this many recent calls qualifies for an
    # error-rate flag.
    tool_error_rate_min_calls: int = 5
    # Error rate above which we'll surface a "tool is flaky" suggestion.
    tool_error_rate_alert: float = 0.20

    # ── Tuning thresholds ──────────────────────────────────────────────
    # Used by the suggester. Each value is the threshold at which we
    # surface a particular suggestion. Tunable so the user can quiet
    # the system down if it's too chatty.
    long_session_min_minutes: float = 25.0
    short_session_max_minutes: float = 5.0
    interrupt_source_majority: float = 0.40
    sleep_undercounted_floor: int = 4
    recall_miss_min_count: int = 3


def load_selftuner_config(config_path: Path | None = None) -> SelfTunerConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    s = raw.get("selftuner", {}) if isinstance(raw.get("selftuner"), dict) else {}
    return SelfTunerConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        flow_db=Path(str(s.get("flow_db", data_dir / "data" / "flow.db"))),
        interrupt_db=Path(str(s.get("interrupt_db",
                                    data_dir / "data" / "interrupts.db"))),
        readiness_db=Path(str(s.get("readiness_db",
                                    data_dir / "data" / "readiness.db"))),
        recall_db=Path(str(s.get("recall_db",
                                 data_dir / "data" / "recall.db"))),
        reflection_dir=Path(str(s.get("reflection_dir",
                                      data_dir / "self_reflections"))),
        latest_md_path=Path(str(s.get("latest_md_path",
                                      data_dir / "self_reflections"
                                      / "latest.md"))),
        reflection_interval_secs=float(s.get("reflection_interval_secs", 86400.0)),
        boot_delay_secs=float(s.get("boot_delay_secs", 600.0)),
        tool_usage_window_secs=float(s.get("tool_usage_window_secs", 86400.0)),
        tool_error_rate_min_calls=int(s.get("tool_error_rate_min_calls", 5)),
        tool_error_rate_alert=float(s.get("tool_error_rate_alert", 0.20)),
        long_session_min_minutes=float(s.get("long_session_min_minutes", 25.0)),
        short_session_max_minutes=float(s.get("short_session_max_minutes", 5.0)),
        interrupt_source_majority=float(s.get("interrupt_source_majority", 0.40)),
        sleep_undercounted_floor=int(s.get("sleep_undercounted_floor", 4)),
        recall_miss_min_count=int(s.get("recall_miss_min_count", 3)),
    )
