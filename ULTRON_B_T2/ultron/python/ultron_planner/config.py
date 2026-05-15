"""Config for Module S+J (Dream Weaver + Scheduler)."""
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


GOAL_STATUSES = ("active", "paused", "done", "archived")
OUTCOME_STATUSES = ("pending", "in_progress", "blocked", "done")
BLOCK_KINDS = ("focus", "break", "admin", "exercise", "study", "social", "other")
EVENT_KINDS = ("alarm", "reminder", "meeting", "deadline")


@dataclass
class PlannerConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "planner.db")

    # Scheduler loop tick (seconds). The tick fires alarm checks.
    tick_seconds: int = 30

    # Horizon (seconds) over which an event is considered "upcoming" and
    # gets an ``upcoming_event`` heads-up before its ``alarm_fire``.
    upcoming_horizon_seconds: int = 300  # 5 minutes

    # Hard cap on rows returned by a single query.
    max_query_rows: int = 500


def load_planner_config(config_path: Path | None = None) -> PlannerConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    p = raw.get("planner", {}) if isinstance(raw.get("planner"), dict) else {}
    return PlannerConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(p.get("db_path", data_dir / "data" / "planner.db"))),
        tick_seconds=int(p.get("tick_seconds", 30)),
        upcoming_horizon_seconds=int(p.get("upcoming_horizon_seconds", 300)),
        max_query_rows=int(p.get("max_query_rows", 500)),
    )
