"""Config for Module Y (Dopamine Marker)."""
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


# Default pattern rules — case-insensitive substring match against the
# event text. Weight is added (or subtracted) into the rolling score.
# Negative weights = wasteful. Positive = rewarding.
DEFAULT_PATTERNS: tuple[tuple[str, str, int, str], ...] = (
    # (name, substring, weight, kind)
    ("instagram_reels",  "instagram",       -3, "wasteful"),
    ("tiktok",           "tiktok",          -3, "wasteful"),
    ("yt_shorts",        "youtube shorts",  -3, "wasteful"),
    ("reels_word",       "reel",            -2, "wasteful"),
    ("twitter_doom",     "twitter",         -1, "wasteful"),
    ("x_doom",           "x.com",           -1, "wasteful"),
    ("reddit_doom",      "reddit",          -1, "wasteful"),
    ("focus_dev",        "vscode",          +2, "rewarding"),
    ("focus_code",       "claude code",     +2, "rewarding"),
    ("focus_terminal",   "powershell",      +1, "rewarding"),
    ("focus_term2",      "windows terminal",+1, "rewarding"),
    ("focus_reading",    "book",            +2, "rewarding"),
    ("focus_writing",    "obsidian",        +2, "rewarding"),
    ("focus_pdf",        ".pdf",            +1, "rewarding"),
    ("workout_word",     "workout",         +3, "rewarding"),
    ("exercise_word",    "exercise",        +2, "rewarding"),
    ("reading_word",     "reading",         +2, "rewarding"),
    ("study_word",       "study",           +2, "rewarding"),
)


@dataclass
class DopamineConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "dopamine.db")

    # EWMA alpha for the rolling score. 0.10 → newer marks weigh ~10 %.
    ewma_alpha: float = 0.10

    # Score floor and ceiling for alerts.
    drift_floor: float = -5.0     # score below → emit drift_alert
    flow_ceiling: float = 5.0     # score above → emit flow_state

    # Minimum gap (seconds) between alerts of the same kind.
    alert_cooldown_seconds: int = 600   # 10 minutes

    # Rows-per-query cap.
    max_query_rows: int = 500


def load_dopamine_config(config_path: Path | None = None) -> DopamineConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    d = raw.get("dopamine", {}) if isinstance(raw.get("dopamine"), dict) else {}
    return DopamineConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(d.get("db_path", data_dir / "data" / "dopamine.db"))),
        ewma_alpha=float(d.get("ewma_alpha", 0.10)),
        drift_floor=float(d.get("drift_floor", -5.0)),
        flow_ceiling=float(d.get("flow_ceiling", 5.0)),
        alert_cooldown_seconds=int(d.get("alert_cooldown_seconds", 600)),
        max_query_rows=int(d.get("max_query_rows", 500)),
    )
