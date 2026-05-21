"""Config for the Re-entry Protocol."""
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
class ReentryConfig:
    ws_url: str
    ws_token: str

    # ── Detection thresholds ──────────────────────────────────────────
    # User is "away" once idle_secs crosses this value.
    away_threshold_secs: float = 300.0          # 5 minutes
    # First non-idle sample after away → return. To avoid spurious
    # returns from a single noisy sample, require the idle gauge to
    # actually drop below the wake threshold.
    return_idle_threshold_secs: float = 30.0
    # Quiet window before the brief is allowed to fire again. Prevents
    # a flapping idle gauge from re-triggering the brief minute-by-minute.
    cooldown_secs: float = 120.0

    # ── Brief content ─────────────────────────────────────────────────
    # Don't bother briefing for short bathroom breaks — speak only if
    # the user was away at least this long.
    min_away_minutes_for_brief: float = 5.0
    # Cap the spoken brief at this character count. ~25-35 words ≈ 10s
    # at normal TTS speed; clip rather than ramble.
    max_brief_chars: int = 260
    # Cap how far back we'll look for the most recent context items.
    recent_lookback_secs: float = 900.0          # 15 minutes
    # How much of the last LLM reply to quote (chars). Whole sentences
    # only — we cut at the last sentence boundary that still fits.
    max_llm_quote_chars: int = 140
    # Whether to mention git activity ("3 commits while you were away").
    include_git_delta: bool = True

    # ── Reactions ─────────────────────────────────────────────────────
    # When true, publish a reentry_brief event with the composed text;
    # the voice engine speaks it via _speak_directly.
    speak_brief: bool = True


def load_reentry_config(config_path: Path | None = None) -> ReentryConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"
    r = raw.get("reentry", {}) if isinstance(raw.get("reentry"), dict) else {}
    return ReentryConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        away_threshold_secs=float(r.get("away_threshold_secs", 300.0)),
        return_idle_threshold_secs=float(r.get("return_idle_threshold_secs", 30.0)),
        cooldown_secs=float(r.get("cooldown_secs", 120.0)),
        min_away_minutes_for_brief=float(r.get("min_away_minutes_for_brief", 5.0)),
        max_brief_chars=int(r.get("max_brief_chars", 260)),
        recent_lookback_secs=float(r.get("recent_lookback_secs", 900.0)),
        max_llm_quote_chars=int(r.get("max_llm_quote_chars", 140)),
        include_git_delta=bool(r.get("include_git_delta", True)),
        speak_brief=bool(r.get("speak_brief", True)),
    )
