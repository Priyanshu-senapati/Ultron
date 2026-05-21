"""Config for the Interrupt Ledger."""
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
class InterruptConfig:
    ws_url: str
    ws_token: str

    db_path: Path = Path()

    # ── Source filtering ──────────────────────────────────────────────
    # Wake-word interactions while the user is PRESENT count as
    # self-interrupts. While AWAY they're just the user coming back —
    # don't double-log.
    record_wake_word: bool = True
    record_flow_break: bool = True
    record_wellness_nudge: bool = True
    record_reentry: bool = False  # opt-in; re-entry is already its own signal

    # Minimum flow duration in seconds for a break to be worth logging.
    # Sub-minute flow blips aren't real interruptions.
    min_flow_break_duration_secs: float = 60.0

    # ── Recovery window ───────────────────────────────────────────────
    # An interrupt is paired with the next flow ACTIVE transition that
    # follows within this window. Beyond it, the interrupt is "stale"
    # and gets no recovery time (the user moved on).
    recovery_window_secs: float = 1800.0   # 30 minutes

    # Cap the open-interrupt set so a runaway publisher can't bloat the
    # in-memory queue.
    max_pending_interrupts: int = 200


def load_interrupt_config(config_path: Path | None = None) -> InterruptConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    i = raw.get("interrupts", {}) if isinstance(raw.get("interrupts"), dict) else {}
    db_default = data_dir / "data" / "interrupts.db"
    return InterruptConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(i.get("db_path", db_default))),
        record_wake_word=bool(i.get("record_wake_word", True)),
        record_flow_break=bool(i.get("record_flow_break", True)),
        record_wellness_nudge=bool(i.get("record_wellness_nudge", True)),
        record_reentry=bool(i.get("record_reentry", False)),
        min_flow_break_duration_secs=float(i.get("min_flow_break_duration_secs", 60.0)),
        recovery_window_secs=float(i.get("recovery_window_secs", 1800.0)),
        max_pending_interrupts=int(i.get("max_pending_interrupts", 200)),
    )
