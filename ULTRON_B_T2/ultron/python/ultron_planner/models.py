"""Dataclasses for the planner domain (goals/outcomes/blocks/events)."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Goal:
    title: str
    dream_kind: str = "personal"   # personal, career, health, learning, financial, creative
    target_date: Optional[str] = None  # "YYYY-MM-DD"
    status: str = "active"
    note: str = ""
    created_at: float = field(default_factory=time.time)
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Outcome:
    goal_id: int
    title: str
    status: str = "pending"
    weight: float = 1.0
    note: str = ""
    created_at: float = field(default_factory=time.time)
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Block:
    ts_start: float
    ts_end: float
    title: str
    kind: str = "focus"
    outcome_id: Optional[int] = None
    note: str = ""
    id: int | None = None

    def duration_minutes(self) -> float:
        return max(0.0, (self.ts_end - self.ts_start) / 60.0)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Event:
    ts: float
    title: str
    kind: str = "alarm"
    payload: str = ""        # arbitrary JSON-y blob (kept as text)
    fired_at: Optional[float] = None
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
