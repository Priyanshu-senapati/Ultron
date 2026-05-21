"""Pure score functions for the Readiness module.

Each component returns a :class:`ReadinessComponent`. ``compute_score``
glues them together into a 0-100 ``ReadinessScore``. Everything here is
deterministic and side-effect-free for easy unit testing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config import ReadinessConfig


@dataclass
class ReadinessComponent:
    name: str
    score: float
    max_score: float
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "max_score": round(self.max_score, 1),
            "detail": self.detail,
            "ratio": round(self.score / self.max_score, 3) if self.max_score else 0.0,
        }


@dataclass
class ReadinessScore:
    total: float
    components: list[ReadinessComponent] = field(default_factory=list)
    computed_ts: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def bucket(self) -> str:
        """Human-friendly bucket so voice/HUD can colour the number."""
        if self.total >= 80:
            return "primed"
        if self.total >= 60:
            return "ready"
        if self.total >= 40:
            return "fatigued"
        return "depleted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 1),
            "bucket": self.bucket,
            "computed_ts": self.computed_ts,
            "components": [c.as_dict() for c in self.components],
            "inputs": self.inputs,
        }


# ── Component scorers ───────────────────────────────────────────────────


def score_sleep(hours: Optional[float], cfg: ReadinessConfig) -> ReadinessComponent:
    """Bell-ish curve centred on cfg.sleep_target_hours.

    Within 0.5h of target = full points. Decays as delta grows.
    Missing data is treated as half — we don't punish the user for an
    unrecorded night, but it shouldn't carry a green readiness either.
    """
    max_pts = cfg.weight_sleep
    if hours is None:
        return ReadinessComponent("sleep", round(max_pts * 0.5, 1), max_pts,
                                  "no sleep recorded")
    diff = abs(float(hours) - cfg.sleep_target_hours)
    if diff <= 0.5:
        ratio = 1.0
    elif diff <= 1.5:
        ratio = 0.75
    elif diff <= 2.5:
        ratio = 0.40
    elif diff <= 3.5:
        ratio = 0.20
    else:
        ratio = 0.05
    return ReadinessComponent(
        "sleep", round(max_pts * ratio, 1), max_pts,
        f"{hours:.1f}h vs {cfg.sleep_target_hours:.1f}h target",
    )


def score_flow_yesterday(minutes: float, cfg: ReadinessConfig) -> ReadinessComponent:
    """Flow minutes from the prior ~24h window. More flow = more readiness."""
    max_pts = cfg.weight_flow_yesterday
    target = max(cfg.flow_target_minutes, 1.0)
    m = max(0.0, float(minutes))
    if m >= target:
        ratio = 1.0
    elif m >= target * 0.5:
        ratio = 0.75
    elif m >= target * 0.25:
        ratio = 0.50
    elif m >= 1.0:
        ratio = 0.25
    else:
        ratio = 0.0
    return ReadinessComponent(
        "flow_yesterday", round(max_pts * ratio, 1), max_pts,
        f"{m:.0f} min in last 24h",
    )


def score_calm(avg_tension: Optional[float], cfg: ReadinessConfig) -> ReadinessComponent:
    """EWMA tension over recent window. Lower = more calm = more points."""
    max_pts = cfg.weight_calm
    if avg_tension is None:
        return ReadinessComponent("calm", round(max_pts * 0.5, 1), max_pts,
                                  "no tension samples yet")
    t = float(avg_tension)
    th = cfg.calm_tension_threshold
    if t <= th:
        ratio = 1.0
    elif t <= th + 0.2:
        ratio = 0.67
    elif t <= th + 0.4:
        ratio = 0.33
    else:
        ratio = 0.0
    return ReadinessComponent(
        "calm", round(max_pts * ratio, 1), max_pts,
        f"avg tension {t:.2f}",
    )


def score_activity(last_workout_ts: Optional[float], now: float,
                   cfg: ReadinessConfig) -> ReadinessComponent:
    """Workout in the last N hours = full points. Otherwise partial (rest is fine)."""
    max_pts = cfg.weight_activity
    if last_workout_ts is None:
        return ReadinessComponent("activity", round(max_pts * 0.27, 1), max_pts,
                                  "no workout logged")
    age_hours = max(0.0, (now - float(last_workout_ts)) / 3600.0)
    if age_hours <= cfg.activity_window_hours:
        return ReadinessComponent("activity", max_pts, max_pts,
                                  f"workout {age_hours:.1f}h ago")
    return ReadinessComponent("activity", round(max_pts * 0.27, 1), max_pts,
                              f"last workout {age_hours:.0f}h ago")


# ── Orchestrator ────────────────────────────────────────────────────────


def compute_score(*, sleep_hours: Optional[float],
                  flow_minutes_yesterday: float,
                  avg_tension: Optional[float],
                  last_workout_ts: Optional[float],
                  now: float,
                  cfg: ReadinessConfig) -> ReadinessScore:
    parts = [
        score_sleep(sleep_hours, cfg),
        score_flow_yesterday(flow_minutes_yesterday, cfg),
        score_calm(avg_tension, cfg),
        score_activity(last_workout_ts, now, cfg),
    ]
    total = sum(p.score for p in parts)
    return ReadinessScore(
        total=round(total, 1),
        components=parts,
        computed_ts=now,
        inputs={
            "sleep_hours": sleep_hours,
            "flow_minutes_yesterday": round(flow_minutes_yesterday, 1),
            "avg_tension": avg_tension,
            "last_workout_ts": last_workout_ts,
        },
    )
