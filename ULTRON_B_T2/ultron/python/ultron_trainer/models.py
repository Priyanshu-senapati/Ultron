"""Dataclasses for the Trainer Twin domain."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Workout:
    ts: float
    exercise: str
    sets: int = 1
    reps: int = 0
    weight_kg: float = 0.0
    duration_secs: int = 0
    note: str = ""
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SleepLog:
    date: str            # "YYYY-MM-DD"  (the *wake* date)
    bedtime_ts: float
    wake_ts: float
    quality: int = 3     # 1..5
    note: str = ""

    def hours(self) -> float:
        return max(0.0, (self.wake_ts - self.bedtime_ts) / 3600.0)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BodyMetric:
    ts: float
    weight_kg: float | None = None
    mood: int | None = None      # 1..5
    energy: int | None = None    # 1..5
    note: str = ""
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
