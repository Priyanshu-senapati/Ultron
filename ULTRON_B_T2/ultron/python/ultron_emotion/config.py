"""Config for the Emotional Intelligence layer."""
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
class EmotionConfig:
    ws_url: str
    ws_token: str

    db_path: Path = Path()

    # ── EWMA decay ────────────────────────────────────────────────────
    # Time-aware decay: weight of OLD value after dt secs =
    # 0.5 ** (dt / half_life_secs). 600s = a frustrated utterance
    # halves its influence over 10 min.
    half_life_secs: float = 600.0

    # ── Tension cross-reference ──────────────────────────────────────
    # If tension is above this AND the user just said something
    # negative, frustration confidence gets a +0.3 boost.
    tension_corroboration_threshold: float = 0.55

    # If the lexicon was confident in a strong frustration signal
    # (raw frustration >= this), publish it even before EWMA settles.
    immediate_publish_frustration: float = 0.6

    # ── Publish gating ────────────────────────────────────────────────
    # Minimum delta in any dimension to consider re-publishing.
    # Stops a stream of similar utterances from spamming the bus.
    min_change_for_publish: float = 0.10

    # Throttle: don't publish more often than this.
    min_publish_interval_secs: float = 2.0

    # ── Prompt-block gating ──────────────────────────────────────────
    # The LLM service only injects the mood block when the signal is
    # interesting enough to warrant nudging the response. These
    # thresholds define "interesting".
    inject_when_frustration_at_least: float = 0.4
    inject_when_negative_valence_at_most: float = -0.4
    inject_when_positive_valence_at_least: float = 0.6


def load_emotion_config(config_path: Path | None = None) -> EmotionConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    e = raw.get("emotion", {}) if isinstance(raw.get("emotion"), dict) else {}
    db_default = data_dir / "data" / "emotion.db"
    return EmotionConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        db_path=Path(str(e.get("db_path", db_default))),
        half_life_secs=float(e.get("half_life_secs", 600.0)),
        tension_corroboration_threshold=float(
            e.get("tension_corroboration_threshold", 0.55)),
        immediate_publish_frustration=float(
            e.get("immediate_publish_frustration", 0.6)),
        min_change_for_publish=float(e.get("min_change_for_publish", 0.10)),
        min_publish_interval_secs=float(e.get("min_publish_interval_secs", 2.0)),
        inject_when_frustration_at_least=float(
            e.get("inject_when_frustration_at_least", 0.4)),
        inject_when_negative_valence_at_most=float(
            e.get("inject_when_negative_valence_at_most", -0.4)),
        inject_when_positive_valence_at_least=float(
            e.get("inject_when_positive_valence_at_least", 0.6)),
    )
