"""Config for the Flow State Protector."""
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


# Productive app categories. Time spent in apps outside this set never
# counts as flow even if the typing metrics look right (e.g. typing fast
# in a chat window isn't "deep work").
DEFAULT_PRODUCTIVE_CATEGORIES: tuple[str, ...] = (
    "editor", "ide", "terminal", "code", "browser_dev",
)


@dataclass
class FlowConfig:
    ws_url: str
    ws_token: str

    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "flow.db")

    # ── Thresholds for "in flow" ──────────────────────────────────────
    # All of these must hold for a tick to be considered "flow-eligible".
    max_tension: float = 0.45           # calm
    min_cognitive_load: float = 0.30    # engaged
    max_cognitive_load: float = 0.85    # not overloaded
    max_app_switch_per_min: float = 3.0
    max_backspace_per_min: float = 10.0
    max_idle_secs: float = 90.0
    # Cadence must be steady or fast (not idle / slow). Empty string in
    # the snapshot also fails — we want active engagement.
    eligible_cadence_bands: tuple[str, ...] = ("steady", "fast", "burst")

    productive_categories: tuple[str, ...] = DEFAULT_PRODUCTIVE_CATEGORIES

    # ── Hysteresis ────────────────────────────────────────────────────
    # Snapshots arrive every ~5s. Require this many consecutive
    # eligible samples before flipping ENTERING → ACTIVE.
    samples_to_activate: int = 3        # ~15 s of sustained eligibility
    # On the way out, allow this many violations before we declare flow
    # broken. Stops a single noisy sample from killing flow.
    samples_to_break: int = 2

    # ── Reactions while ACTIVE ────────────────────────────────────────
    # Voice engine reads these via the flow_state_changed event.
    silence_non_urgent_voice: bool = True
    dim_hud: bool = True


def load_flow_config(config_path: Path | None = None) -> FlowConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    f = raw.get("flow", {}) if isinstance(raw.get("flow"), dict) else {}
    return FlowConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(f.get("db_path", data_dir / "data" / "flow.db"))),
        max_tension=float(f.get("max_tension", 0.45)),
        min_cognitive_load=float(f.get("min_cognitive_load", 0.30)),
        max_cognitive_load=float(f.get("max_cognitive_load", 0.85)),
        max_app_switch_per_min=float(f.get("max_app_switch_per_min", 3.0)),
        max_backspace_per_min=float(f.get("max_backspace_per_min", 10.0)),
        max_idle_secs=float(f.get("max_idle_secs", 90.0)),
        samples_to_activate=int(f.get("samples_to_activate", 3)),
        samples_to_break=int(f.get("samples_to_break", 2)),
        silence_non_urgent_voice=bool(f.get("silence_non_urgent_voice", True)),
        dim_hud=bool(f.get("dim_hud", True)),
    )
