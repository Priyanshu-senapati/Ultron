"""Context tunnel -- tracks what you were doing per app/task and restores
the mental context when you switch back.

Listens for focus_app changes. When the user leaves an app (switches to
another), saves a snapshot of what they were doing: app name, window
title, last voice transcript in that app, time spent, emotion state.
When they return to the same app later, publishes a brief spoken
context restore: "You were debugging auth in VS Code, suspected the
token refresh — 15 minutes ago."
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("ultron.context_tunnel")


class ContextTunnel:
    def __init__(self, publish, min_away_secs: float = 120.0,
                 max_contexts: int = 20) -> None:
        self._publish = publish
        self._min_away_secs = min_away_secs
        self._max_contexts = max_contexts
        self._contexts: dict[str, dict[str, Any]] = {}
        self._current_app: str = ""
        self._current_app_since: float = 0.0
        self._last_transcript: str = ""
        self._last_restore_ts: float = 0.0

    def on_focus_change(self, app: str, title: str) -> Optional[dict]:
        now = time.time()
        app_key = app.lower().strip()
        if not app_key or app_key == self._current_app:
            return None

        if self._current_app:
            duration = now - self._current_app_since
            self._contexts[self._current_app] = {
                "app": self._current_app,
                "title": self._contexts.get(self._current_app, {}).get("title", ""),
                "last_transcript": self._last_transcript,
                "left_at": now,
                "duration_secs": duration,
            }
            if len(self._contexts) > self._max_contexts:
                oldest = min(self._contexts, key=lambda k: self._contexts[k].get("left_at", 0))
                del self._contexts[oldest]

        result = None
        if app_key in self._contexts:
            ctx = self._contexts[app_key]
            away_secs = now - ctx.get("left_at", now)
            if away_secs >= self._min_away_secs and now - self._last_restore_ts > 30:
                result = {
                    "app": app_key,
                    "title": ctx.get("title", ""),
                    "last_transcript": ctx.get("last_transcript", ""),
                    "away_minutes": round(away_secs / 60, 1),
                    "prev_duration_minutes": round(ctx.get("duration_secs", 0) / 60, 1),
                }
                self._last_restore_ts = now

        self._current_app = app_key
        self._current_app_since = now
        return result

    def on_transcript(self, text: str) -> None:
        if text.strip():
            self._last_transcript = text.strip()[:200]

    def on_app_detail(self, app: str, title: str) -> None:
        app_key = app.lower().strip()
        if app_key in self._contexts:
            self._contexts[app_key]["title"] = title[:120]
        if app_key == self._current_app:
            self._contexts.setdefault(app_key, {})["title"] = title[:120]
