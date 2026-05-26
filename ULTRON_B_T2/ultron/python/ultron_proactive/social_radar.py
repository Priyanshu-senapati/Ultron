"""Social Radar -- track who you mention in conversations and nudge
when you haven't been in touch with someone for a while.

Extracts names from voice transcripts using simple NLP (capitalized
words that appear multiple times). Tracks last-mention timestamps.
After 14+ days of silence, publishes a gentle nudge.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ultron.proactive.social_radar")

_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,15})\b")
_IGNORE = {
    "Ultron", "Sir", "Please", "Thanks", "Google", "Chrome", "Spotify",
    "Discord", "YouTube", "GitHub", "Windows", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "June", "July",
    "August", "September", "October", "November", "December",
    "Hello", "Goodbye", "Sorry", "Okay", "India", "Delhi",
    "Mumbai", "The", "This", "That", "What", "When", "Where",
    "How", "Why", "Who", "Can", "Could", "Would", "Should",
    "Will", "Just", "Also", "Maybe", "Yes", "Hey", "Here",
}


class SocialRadar:
    def __init__(self, stale_days: int = 14, cooldown_hours: float = 72.0) -> None:
        self._stale_days = stale_days
        self._cooldown_secs = cooldown_hours * 3600
        self._contacts: dict[str, dict[str, Any]] = {}
        self._last_nudge: dict[str, float] = {}
        self._data_path = self._get_data_path()
        self._load()

    @staticmethod
    def _get_data_path() -> Path:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = Path(appdata) / "ULTRON" / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d / "social_radar.json"

    def _load(self) -> None:
        if self._data_path.exists():
            try:
                self._contacts = json.loads(
                    self._data_path.read_text(encoding="utf-8")
                )
            except Exception:
                pass

    def _save(self) -> None:
        try:
            self._data_path.write_text(
                json.dumps(self._contacts, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def extract_and_track(self, text: str) -> None:
        names = set(_NAME_RE.findall(text)) - _IGNORE
        now = time.time()
        for name in names:
            if name not in self._contacts:
                self._contacts[name] = {"first_seen": now, "mention_count": 0}
            self._contacts[name]["last_seen"] = now
            self._contacts[name]["mention_count"] = (
                self._contacts[name].get("mention_count", 0) + 1
            )
        if names:
            self._save()

    def get_stale_contacts(self) -> list[dict[str, Any]]:
        now = time.time()
        stale_secs = self._stale_days * 86400
        nudges = []
        for name, info in self._contacts.items():
            if info.get("mention_count", 0) < 2:
                continue
            last = info.get("last_seen", now)
            if now - last < stale_secs:
                continue
            if now - self._last_nudge.get(name, 0) < self._cooldown_secs:
                continue
            days_ago = int((now - last) / 86400)
            nudges.append({
                "name": name,
                "days_ago": days_ago,
                "mention_count": info.get("mention_count", 0),
                "suggestion": (
                    f"You haven't mentioned {name} in {days_ago} days. "
                    f"Might be worth checking in."
                ),
            })
            self._last_nudge[name] = now
        return nudges
