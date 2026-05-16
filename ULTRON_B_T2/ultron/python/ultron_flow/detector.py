"""Pure state machine — no I/O, no clock, easy to unit-test.

The service feeds it merged samples (each with the relevant fields from
``insight_snapshot`` + ``input_metrics_updated``). Transitions are
emitted as :class:`StateTransition` events for the service to publish
and log.

States:
    IDLE       — nothing flow-like happening.
    ENTERING   — first sample of eligibility after an IDLE/BROKEN state.
                 Don't react yet — could be a one-off.
    ACTIVE     — eligibility sustained for ``samples_to_activate``.
                 This is where the rest of the stack should adapt
                 (silence voice, dim HUD, suppress alerts).
    BROKEN     — eligibility lost for ``samples_to_break``. Carries the
                 reason ("app_switch" / "idle" / "tension_spike" /
                 "backspace_burst" / "cognitive_overload" /
                 "unproductive_app") and the prior session's duration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import FlowConfig


class FlowState(str, Enum):
    IDLE = "idle"
    ENTERING = "entering"
    ACTIVE = "active"
    BROKEN = "broken"


@dataclass
class FlowSample:
    """Subset of the bus signals the detector actually uses."""
    ts: float = field(default_factory=time.time)
    cognitive_load: float = 0.0
    tension: float = 0.0
    cadence_band: str = ""
    focus_category: str = ""
    app_switch_per_min: float = 0.0
    backspace_per_min: float = 0.0
    idle_secs: float = 0.0
    focus_app: str = ""


@dataclass
class StateTransition:
    """Emitted whenever the detector flips state."""
    from_state: FlowState
    to_state: FlowState
    ts: float
    # Populated for BROKEN transitions:
    duration_seconds: float = 0.0
    reason: str = ""
    last_focus_app: str = ""


class FlowDetector:
    def __init__(self, config: FlowConfig) -> None:
        self._cfg = config
        self._state: FlowState = FlowState.IDLE
        self._consec_eligible: int = 0
        self._consec_ineligible: int = 0
        self._session_start_ts: Optional[float] = None
        self._last_sample: Optional[FlowSample] = None
        self._last_eligible_sample: Optional[FlowSample] = None

    @property
    def state(self) -> FlowState:
        return self._state

    @property
    def session_start_ts(self) -> Optional[float]:
        return self._session_start_ts

    def _is_eligible(self, s: FlowSample) -> tuple[bool, str]:
        """Return (ok, fail_reason). Reason is empty on success."""
        cfg = self._cfg
        if s.idle_secs > cfg.max_idle_secs:
            return False, "idle"
        if s.tension > cfg.max_tension:
            return False, "tension_spike"
        if s.cognitive_load < cfg.min_cognitive_load:
            return False, "disengaged"
        if s.cognitive_load > cfg.max_cognitive_load:
            return False, "cognitive_overload"
        if s.app_switch_per_min > cfg.max_app_switch_per_min:
            return False, "app_switch"
        if s.backspace_per_min > cfg.max_backspace_per_min:
            return False, "backspace_burst"
        if s.cadence_band and s.cadence_band not in cfg.eligible_cadence_bands:
            return False, "cadence_" + s.cadence_band
        if (s.focus_category and cfg.productive_categories
                and s.focus_category not in cfg.productive_categories):
            return False, "unproductive_app"
        return True, ""

    def feed(self, sample: FlowSample) -> Optional[StateTransition]:
        """Apply one sample. Returns a transition if state changed."""
        self._last_sample = sample
        eligible, reason = self._is_eligible(sample)

        if eligible:
            self._consec_eligible += 1
            self._consec_ineligible = 0
            self._last_eligible_sample = sample
        else:
            self._consec_ineligible += 1
            self._consec_eligible = 0

        if self._state == FlowState.IDLE:
            if eligible:
                self._state = FlowState.ENTERING
                self._session_start_ts = sample.ts
                return StateTransition(
                    from_state=FlowState.IDLE, to_state=FlowState.ENTERING,
                    ts=sample.ts,
                )

        elif self._state == FlowState.ENTERING:
            if not eligible:
                # Lost it before activation — fold back to idle silently.
                self._state = FlowState.IDLE
                self._session_start_ts = None
                self._consec_eligible = 0
                return StateTransition(
                    from_state=FlowState.ENTERING, to_state=FlowState.IDLE,
                    ts=sample.ts, reason=reason,
                )
            if self._consec_eligible >= self._cfg.samples_to_activate:
                self._state = FlowState.ACTIVE
                return StateTransition(
                    from_state=FlowState.ENTERING, to_state=FlowState.ACTIVE,
                    ts=sample.ts,
                )

        elif self._state == FlowState.ACTIVE:
            if not eligible and self._consec_ineligible >= self._cfg.samples_to_break:
                duration = (sample.ts - self._session_start_ts) if self._session_start_ts else 0.0
                last_app = (self._last_eligible_sample.focus_app
                            if self._last_eligible_sample else sample.focus_app)
                self._state = FlowState.BROKEN
                start = self._session_start_ts
                self._session_start_ts = None
                trans = StateTransition(
                    from_state=FlowState.ACTIVE, to_state=FlowState.BROKEN,
                    ts=sample.ts, duration_seconds=duration,
                    reason=reason, last_focus_app=last_app,
                )
                # The BROKEN state is a one-tick marker; reset to IDLE
                # immediately so a new flow can start cleanly.
                self._state = FlowState.IDLE
                self._consec_eligible = 0
                self._consec_ineligible = 0
                _ = start  # silence linter
                return trans

        return None
