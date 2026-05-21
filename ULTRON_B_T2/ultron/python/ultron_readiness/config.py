"""Config for the Readiness Score module."""
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
class ReadinessConfig:
    ws_url: str
    ws_token: str

    db_path: Path = Path()

    # ── Component weights (must sum to 100) ───────────────────────────
    weight_sleep: float = 40.0
    weight_flow_yesterday: float = 30.0
    weight_calm: float = 15.0
    weight_activity: float = 15.0

    # ── Targets / thresholds ──────────────────────────────────────────
    # Target nightly hours. Sleep score peaks within 0.5h of this and
    # decays as the delta grows.
    sleep_target_hours: float = 7.5
    # Yesterday flow minutes that lock in the full flow-component score.
    flow_target_minutes: float = 120.0
    # Tension threshold below which we award full calm points.
    calm_tension_threshold: float = 0.3
    # Insight EWMA half-life in seconds — controls how quickly the
    # calm signal forgets old samples.
    calm_ewma_half_life_secs: float = 1800.0       # 30 minutes
    # Workout-recency window. A workout within this window scores full
    # activity points; older = neutral (a rest day is fine).
    activity_window_hours: float = 24.0

    # ── Cadence ───────────────────────────────────────────────────────
    # Recompute the score every N seconds while the service is idle.
    # The score also updates on every relevant inbound event.
    recompute_interval_secs: float = 300.0
    # On a fresh process boot, wait this long before publishing the
    # first score so subscribers have time to be ready.
    boot_delay_secs: float = 2.0


def load_readiness_config(config_path: Path | None = None) -> ReadinessConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    r = raw.get("readiness", {}) if isinstance(raw.get("readiness"), dict) else {}
    db_default = data_dir / "data" / "readiness.db"
    return ReadinessConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(r.get("db_path", db_default))),
        weight_sleep=float(r.get("weight_sleep", 40.0)),
        weight_flow_yesterday=float(r.get("weight_flow_yesterday", 30.0)),
        weight_calm=float(r.get("weight_calm", 15.0)),
        weight_activity=float(r.get("weight_activity", 15.0)),
        sleep_target_hours=float(r.get("sleep_target_hours", 7.5)),
        flow_target_minutes=float(r.get("flow_target_minutes", 120.0)),
        calm_tension_threshold=float(r.get("calm_tension_threshold", 0.3)),
        calm_ewma_half_life_secs=float(r.get("calm_ewma_half_life_secs", 1800.0)),
        activity_window_hours=float(r.get("activity_window_hours", 24.0)),
        recompute_interval_secs=float(r.get("recompute_interval_secs", 300.0)),
        boot_delay_secs=float(r.get("boot_delay_secs", 2.0)),
    )
