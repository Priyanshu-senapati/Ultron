"""Aggregations over the trainer ledger.

Derives streaks and weekly rollups; never mutates the store.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .config import HABIT_KINDS, TrainerConfig
from .store import TrainerStore

logger = logging.getLogger("ultron.trainer.analytics")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class TrainerAnalytics:
    def __init__(self, store: TrainerStore, config: TrainerConfig) -> None:
        self._store = store
        self._cfg = config

    # ── Streaks ────────────────────────────────────────────────────────

    def streak(self, kind: str, *, as_of: Optional[str] = None) -> dict[str, Any]:
        """Count consecutive days ending in ``as_of`` (default: today)
        that contain at least one record of ``kind``."""
        if kind not in HABIT_KINDS:
            return {"kind": kind, "current": 0, "error": f"unknown habit kind {kind!r}"}
        anchor = as_of or _today_utc()
        anchor_dt = datetime.strptime(anchor, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Pull the last ~365 entries for the kind, then walk backwards.
        days_present: set[str] = set()
        if kind == "workout":
            rows = self._store.list_workouts(limit=self._cfg.max_query_rows)
            for r in rows:
                days_present.add(_date_from_ts(r["ts"]))
        elif kind == "sleep":
            for r in self._store.list_sleep(limit=self._cfg.max_query_rows):
                days_present.add(r["date"])
        elif kind == "weight":
            for r in self._store.list_metrics(limit=self._cfg.max_query_rows):
                if r["weight_kg"] is not None:
                    days_present.add(_date_from_ts(r["ts"]))

        current = 0
        cursor = anchor_dt
        # Allow the user to log today *or* yesterday and still count today
        # if today is missing — common when checking late at night.
        if cursor.strftime("%Y-%m-%d") not in days_present:
            cursor -= timedelta(days=1)
        while cursor.strftime("%Y-%m-%d") in days_present:
            current += 1
            cursor -= timedelta(days=1)
        return {"kind": kind, "current": current, "as_of": anchor}

    def all_streaks(self, as_of: Optional[str] = None) -> list[dict[str, Any]]:
        return [self.streak(k, as_of=as_of) for k in HABIT_KINDS]

    # ── Weekly rollups ─────────────────────────────────────────────────

    def weekly_workout_summary(self, *, weeks: int = 1) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7 * max(1, weeks))).timestamp()
        rows = self._store.list_workouts(since_ts=cutoff, limit=self._cfg.max_query_rows)
        total_secs = sum(int(r["duration_secs"]) for r in rows)
        total_reps = sum(int(r["sets"]) * int(r["reps"]) for r in rows)
        unique_days = {_date_from_ts(r["ts"]) for r in rows}
        by_ex: dict[str, int] = {}
        for r in rows:
            by_ex[r["exercise"]] = by_ex.get(r["exercise"], 0) + 1
        return {
            "weeks": weeks,
            "sessions": len(rows),
            "active_days": len(unique_days),
            "total_minutes": round(total_secs / 60.0, 1),
            "total_reps": total_reps,
            "exercises": sorted(
                ({"exercise": k, "sessions": v} for k, v in by_ex.items()),
                key=lambda r: r["sessions"], reverse=True,
            ),
        }

    def weekly_sleep_summary(self, *, weeks: int = 1) -> dict[str, Any]:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=7 * max(1, weeks))
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
        rows = self._store.list_sleep(since_date=cutoff_date, limit=self._cfg.max_query_rows)
        if not rows:
            return {
                "weeks": weeks, "nights": 0, "avg_hours": 0.0,
                "avg_quality": 0.0, "below_target": 0,
                "target_hours": self._cfg.sleep_target_hours,
            }
        hours_list = [
            max(0.0, (r["wake_ts"] - r["bedtime_ts"]) / 3600.0) for r in rows
        ]
        avg_hours = sum(hours_list) / len(hours_list)
        avg_quality = sum(int(r["quality"]) for r in rows) / len(rows)
        below = sum(1 for h in hours_list if h < self._cfg.sleep_target_hours)
        return {
            "weeks": weeks,
            "nights": len(rows),
            "avg_hours": round(avg_hours, 2),
            "avg_quality": round(avg_quality, 2),
            "below_target": below,
            "target_hours": self._cfg.sleep_target_hours,
        }

    # ── Body trends ────────────────────────────────────────────────────

    def latest_metrics(self) -> dict[str, Any]:
        """The most recent non-null reading for each metric."""
        rows = self._store.list_metrics(limit=self._cfg.max_query_rows)
        out: dict[str, Any] = {"weight_kg": None, "mood": None, "energy": None}
        for r in rows:  # already sorted DESC by ts
            if out["weight_kg"] is None and r["weight_kg"] is not None:
                out["weight_kg"] = r["weight_kg"]
                out["weight_ts"] = r["ts"]
            if out["mood"] is None and r["mood"] is not None:
                out["mood"] = r["mood"]
                out["mood_ts"] = r["ts"]
            if out["energy"] is None and r["energy"] is not None:
                out["energy"] = r["energy"]
                out["energy_ts"] = r["ts"]
            if all(out[k] is not None for k in ("weight_kg", "mood", "energy")):
                break
        return out

    def weight_trend(self, *, days: int = 30) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).timestamp()
        rows = [r for r in self._store.list_metrics(
            since_ts=cutoff, limit=self._cfg.max_query_rows
        ) if r["weight_kg"] is not None]
        if not rows:
            return {"days": days, "samples": 0, "first": None, "last": None, "delta": 0.0}
        # store returns DESC; first = oldest, last = newest in window.
        first = rows[-1]["weight_kg"]
        last = rows[0]["weight_kg"]
        return {
            "days": days,
            "samples": len(rows),
            "first": first,
            "last": last,
            "delta": round(last - first, 2),
        }

    @staticmethod
    def today() -> str:
        return _today_utc()
