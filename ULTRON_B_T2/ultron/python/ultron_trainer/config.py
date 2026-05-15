"""Config for Module TT (Trainer Twin)."""
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


# Habit kinds that contribute to streaks. A workout *or* a sleep log on
# a given date counts; missing days break the streak.
HABIT_KINDS: tuple[str, ...] = ("workout", "sleep", "weight")


@dataclass
class TrainerConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "trainer.db")

    # Daily target minutes of workout. Used to score effort.
    daily_workout_target_min: int = 30

    # Sleep targets — hours and a window for "on-time" detection (24h clock).
    sleep_target_hours: float = 7.5
    sleep_window_start_hour: int = 22   # 22:00
    sleep_window_end_hour: int = 24     # midnight

    # Cap on how many rows a single query may return.
    max_query_rows: int = 500


def load_trainer_config(config_path: Path | None = None) -> TrainerConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    t = raw.get("trainer", {}) if isinstance(raw.get("trainer"), dict) else {}
    return TrainerConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(t.get("db_path", data_dir / "data" / "trainer.db"))),
        daily_workout_target_min=int(t.get("daily_workout_target_min", 30)),
        sleep_target_hours=float(t.get("sleep_target_hours", 7.5)),
        sleep_window_start_hour=int(t.get("sleep_window_start_hour", 22)),
        sleep_window_end_hour=int(t.get("sleep_window_end_hour", 24)),
        max_query_rows=int(t.get("max_query_rows", 500)),
    )
