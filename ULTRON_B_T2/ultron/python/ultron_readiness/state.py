"""Rolling state the service feeds the calculator from.

Holds the freshest values for each signal — none of these are queries
against another sidecar; they're updated whenever an inbound bus event
delivers a value. Flow minutes are an exception: we keep a running
counter of completed flow sessions within the trailing 24h window so we
don't need to RPC the flow service on every recompute.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class _FlowEntry:
    end_ts: float
    duration_secs: float


class ReadinessState:
    def __init__(self, calm_half_life_secs: float) -> None:
        self._half_life = max(60.0, calm_half_life_secs)
        # Sleep: payload from sleep_recorded already has `hours`.
        self._last_sleep_hours: Optional[float] = None
        self._last_sleep_ts: float = 0.0
        # Workout: timestamp of most recent workout_recorded.
        self._last_workout_ts: Optional[float] = None
        self._last_workout_duration_secs: int = 0
        # Tension EWMA, fed by insight_snapshot.
        self._tension_ewma: Optional[float] = None
        self._tension_ewma_ts: float = 0.0
        # 24h trailing flow window.
        self._flow_sessions: deque[_FlowEntry] = deque()

    # ── Updaters (called by service) ───────────────────────────────────

    def update_sleep(self, hours: float, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        # Only overwrite if the new entry is strictly more recent.
        if ts >= self._last_sleep_ts:
            self._last_sleep_hours = float(hours)
            self._last_sleep_ts = ts

    def update_workout(self, ts: float, duration_secs: int = 0) -> None:
        if ts >= (self._last_workout_ts or 0.0):
            self._last_workout_ts = float(ts)
            self._last_workout_duration_secs = int(duration_secs)

    def update_tension(self, tension: float, ts: Optional[float] = None) -> None:
        """Apply an EWMA that decays based on elapsed wall-clock time.

        Using a time-aware decay (not a fixed alpha) so that signals
        arriving in bursts don't over-weight the rolling average.
        """
        ts = ts if ts is not None else time.time()
        t = float(tension)
        if self._tension_ewma is None or self._tension_ewma_ts == 0.0:
            self._tension_ewma = t
            self._tension_ewma_ts = ts
            return
        dt = max(0.0, ts - self._tension_ewma_ts)
        # Exponential decay: weight of OLD value after dt = 0.5 ** (dt / half_life).
        old_weight = math.pow(0.5, dt / self._half_life)
        new_weight = 1.0 - old_weight
        self._tension_ewma = (old_weight * self._tension_ewma) + (new_weight * t)
        self._tension_ewma_ts = ts

    def update_flow_session(self, end_ts: float, duration_secs: float) -> None:
        self._flow_sessions.append(_FlowEntry(end_ts=float(end_ts),
                                              duration_secs=float(duration_secs)))
        # Prune entries that have fallen out of any plausible look-back.
        self._prune_flow_sessions(now=end_ts)

    def _prune_flow_sessions(self, now: float, lookback_secs: float = 48 * 3600.0) -> None:
        cutoff = now - lookback_secs
        while self._flow_sessions and self._flow_sessions[0].end_ts < cutoff:
            self._flow_sessions.popleft()

    # ── Readers (consumed by service.compute_now) ──────────────────────

    @property
    def last_sleep_hours(self) -> Optional[float]:
        return self._last_sleep_hours

    @property
    def last_sleep_ts(self) -> float:
        return self._last_sleep_ts

    @property
    def last_workout_ts(self) -> Optional[float]:
        return self._last_workout_ts

    @property
    def tension_ewma(self) -> Optional[float]:
        return self._tension_ewma

    def flow_minutes_in_last_24h(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        cutoff = now - 86400.0
        self._prune_flow_sessions(now=now)
        total = 0.0
        for entry in self._flow_sessions:
            if entry.end_ts >= cutoff:
                total += entry.duration_secs
        return total / 60.0
