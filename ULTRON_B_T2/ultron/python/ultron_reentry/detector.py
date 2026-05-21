"""Pure state machine for the Re-entry Protocol.

States:
    PRESENT    — user is at the keyboard. Most recent idle_secs is small.
    AWAY       — idle_secs has crossed ``away_threshold_secs``. Mark the
                 timestamp; the next return will be briefed.
    RETURNING  — one-tick marker. Emitted on the first sample after AWAY
                 where idle_secs has dropped below ``return_idle_threshold_secs``.
                 The service uses this transition to fire the brief, then
                 the machine resets to PRESENT.

The detector is fed two kinds of samples:
- ``feed_idle(idle_secs, ts)`` from ``input_metrics_updated``
- ``mark_activity(ts)`` from any direct keystroke / mouse event that
  doesn't come through input_metrics_updated yet (optional, used to
  shorten the "first keystroke wakes the brief" latency).

The service is responsible for cooldown — the detector itself doesn't
remember the last brief time; it'll happily emit a RETURNING transition
every cycle, and the service decides whether to suppress.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import ReentryConfig


class PresenceState(str, Enum):
    PRESENT = "present"
    AWAY = "away"
    RETURNING = "returning"


@dataclass
class PresenceTransition:
    from_state: PresenceState
    to_state: PresenceState
    ts: float
    away_started_ts: float = 0.0
    away_duration_seconds: float = 0.0


class ReentryDetector:
    def __init__(self, config: ReentryConfig) -> None:
        self._cfg = config
        self._state: PresenceState = PresenceState.PRESENT
        self._away_started_ts: Optional[float] = None
        self._last_idle_secs: float = 0.0
        self._last_ts: float = 0.0

    @property
    def state(self) -> PresenceState:
        return self._state

    @property
    def away_started_ts(self) -> Optional[float]:
        return self._away_started_ts

    @property
    def last_idle_secs(self) -> float:
        return self._last_idle_secs

    def _go_away(self, ts: float) -> PresenceTransition:
        # When we cross the threshold we don't know the exact moment the
        # user actually left — we know idle_secs >= away_threshold_secs
        # NOW. Subtract to estimate the away-start.
        self._away_started_ts = ts - self._last_idle_secs
        self._state = PresenceState.AWAY
        return PresenceTransition(
            from_state=PresenceState.PRESENT,
            to_state=PresenceState.AWAY,
            ts=ts,
            away_started_ts=self._away_started_ts,
        )

    def _go_returning(self, ts: float) -> PresenceTransition:
        start = self._away_started_ts or ts
        duration = max(0.0, ts - start)
        trans = PresenceTransition(
            from_state=PresenceState.AWAY,
            to_state=PresenceState.RETURNING,
            ts=ts,
            away_started_ts=start,
            away_duration_seconds=duration,
        )
        # Immediately reset to PRESENT so the next sample doesn't
        # re-emit RETURNING. RETURNING is a one-tick marker.
        self._state = PresenceState.PRESENT
        self._away_started_ts = None
        return trans

    def feed_idle(self, idle_secs: float, ts: Optional[float] = None) -> Optional[PresenceTransition]:
        ts = ts if ts is not None else time.time()
        self._last_idle_secs = idle_secs
        self._last_ts = ts

        if self._state == PresenceState.PRESENT:
            if idle_secs >= self._cfg.away_threshold_secs:
                return self._go_away(ts)
            return None

        if self._state == PresenceState.AWAY:
            if idle_secs <= self._cfg.return_idle_threshold_secs:
                return self._go_returning(ts)
            return None

        return None

    def mark_activity(self, ts: Optional[float] = None) -> Optional[PresenceTransition]:
        """Treat a direct activity signal as idle_secs == 0."""
        return self.feed_idle(0.0, ts)
