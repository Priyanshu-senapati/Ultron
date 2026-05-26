"""Predictive app launcher -- learns time-of-day app patterns and
pre-launches apps before you need them.

Tracks which apps are opened at which hour over multiple days. When
a pattern is strong enough (opened 3+ times at the same hour on
different days), publishes a proactive_suggestion to pre-launch.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("ultron.proactive.predictor")
IST = ZoneInfo("Asia/Kolkata")


class AppPredictor:
    def __init__(self, min_occurrences: int = 3,
                 lookahead_minutes: int = 2) -> None:
        self._min_occurrences = min_occurrences
        self._lookahead_minutes = lookahead_minutes
        # hour → app_name → count of distinct days
        self._patterns: dict[int, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self._data_path = self._get_data_path()
        self._load()
        self._last_predicted: dict[str, float] = {}

    @staticmethod
    def _get_data_path() -> Path:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = Path(appdata) / "ULTRON" / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d / "app_patterns.json"

    def _load(self) -> None:
        if self._data_path.exists():
            try:
                raw = json.loads(self._data_path.read_text(encoding="utf-8"))
                for hour_str, apps in raw.items():
                    hour = int(hour_str)
                    for app, days in apps.items():
                        self._patterns[hour][app] = set(days)
            except Exception:
                pass

    def _save(self) -> None:
        serializable = {}
        for hour, apps in self._patterns.items():
            serializable[str(hour)] = {
                app: list(days) for app, days in apps.items()
            }
        try:
            self._data_path.write_text(
                json.dumps(serializable, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("app_patterns save failed: %s", exc)

    def record_app_open(self, app_name: str) -> None:
        now = datetime.now(IST)
        hour = now.hour
        date_str = now.strftime("%Y-%m-%d")
        app_key = app_name.lower().strip()
        if not app_key:
            return
        self._patterns[hour][app_key].add(date_str)
        self._save()

    def get_predictions(self) -> list[dict[str, Any]]:
        now = datetime.now(IST)
        target_hour = (now.hour + (1 if now.minute >= 60 - self._lookahead_minutes else 0)) % 24
        check_hours = [now.hour, target_hour]

        predictions = []
        for hour in check_hours:
            for app, days in self._patterns.get(hour, {}).items():
                if len(days) >= self._min_occurrences:
                    last_pred = self._last_predicted.get(app, 0)
                    if time.time() - last_pred < 3600:
                        continue
                    predictions.append({
                        "app": app,
                        "hour": hour,
                        "confidence": len(days),
                        "suggestion": f"You usually open {app} around this time. Want me to launch it?",
                    })
                    self._last_predicted[app] = time.time()

        return predictions
