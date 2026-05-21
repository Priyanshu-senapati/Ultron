"""Rolling buffer of recent bus events that feed the re-entry brief.

Each event is stored with the timestamp the service observed it (not
the publisher's ``ts_unix_ms``, which may be missing) so we can prune
by ``recent_lookback_secs`` cleanly. Counts of git activity since the
user went away are tracked separately because we want a *delta*, not a
"last seen" value.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ContextSnapshot:
    """What the composer reads to build the brief."""
    last_focus_app: str = ""
    last_focus_category: str = ""
    last_focus_ts: float = 0.0
    last_visual_label: str = ""
    last_visual_ts: float = 0.0
    last_llm_text: str = ""
    last_llm_shard: str = ""
    last_llm_ts: float = 0.0
    last_user_transcript: str = ""
    last_user_ts: float = 0.0
    commits_since_away: int = 0
    last_commit_sha: str = ""


class ReentryContext:
    """Track the most recent values of context-bearing bus events."""

    def __init__(self, lookback_secs: float) -> None:
        self._lookback = max(60.0, lookback_secs)
        self._focus_app: str = ""
        self._focus_category: str = ""
        self._focus_ts: float = 0.0
        self._visual_label: str = ""
        self._visual_ts: float = 0.0
        self._llm_text: str = ""
        self._llm_shard: str = ""
        self._llm_ts: float = 0.0
        self._user_text: str = ""
        self._user_ts: float = 0.0
        self._commits_during_away: list[str] = []
        self._away_started_ts: Optional[float] = None

    # ── Event sinks ────────────────────────────────────────────────────

    def on_insight_snapshot(self, payload: dict[str, Any], ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        app = str(payload.get("focus_app") or "").strip()
        cat = str(payload.get("focus_category") or "").strip()
        if app:
            self._focus_app = app
            self._focus_category = cat
            self._focus_ts = ts

    def on_visual_label(self, payload: dict[str, Any], ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        label = str(payload.get("label") or "").strip()
        if label:
            self._visual_label = label
            self._visual_ts = ts

    def on_llm_response(self, payload: dict[str, Any], ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        text = str(payload.get("text") or "").strip()
        shard = str(payload.get("shard") or "").strip()
        if text:
            self._llm_text = text
            self._llm_shard = shard
            self._llm_ts = ts

    def on_voice_transcript(self, payload: dict[str, Any], ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        text = str(payload.get("text") or "").strip()
        if text:
            self._user_text = text
            self._user_ts = ts

    def on_git_activity(self, payload: dict[str, Any], ts: Optional[float] = None) -> None:
        """git_activity payload: ``{commits: [...], head: sha, ts_unix_ms}``.

        While the user is away we accumulate commit SHAs so the brief
        can say "3 commits while you were away" even if multiple
        git_activity bursts arrived. While present we just remember the
        latest head.
        """
        commits = payload.get("commits") or []
        head = str(payload.get("head") or "")
        if self._away_started_ts is not None and commits:
            for c in commits:
                sha = str((c or {}).get("sha") or "")
                if sha and sha not in self._commits_during_away:
                    self._commits_during_away.append(sha)
        if head:
            # Always update the "last commit" sentinel for non-away view.
            self._last_head = head  # type: ignore[attr-defined]

    # ── Away lifecycle ─────────────────────────────────────────────────

    def mark_away(self, ts: float) -> None:
        self._away_started_ts = ts
        self._commits_during_away = []

    def mark_return(self) -> None:
        self._away_started_ts = None

    # ── Snapshot for composer ──────────────────────────────────────────

    def snapshot(self, now: Optional[float] = None) -> ContextSnapshot:
        now = now if now is not None else time.time()
        cutoff = now - self._lookback

        def _fresh(ts: float) -> bool:
            return ts > 0 and ts >= cutoff

        return ContextSnapshot(
            last_focus_app=self._focus_app if _fresh(self._focus_ts) else "",
            last_focus_category=self._focus_category if _fresh(self._focus_ts) else "",
            last_focus_ts=self._focus_ts,
            last_visual_label=self._visual_label if _fresh(self._visual_ts) else "",
            last_visual_ts=self._visual_ts,
            last_llm_text=self._llm_text if _fresh(self._llm_ts) else "",
            last_llm_shard=self._llm_shard if _fresh(self._llm_ts) else "",
            last_llm_ts=self._llm_ts,
            last_user_transcript=self._user_text if _fresh(self._user_ts) else "",
            last_user_ts=self._user_ts,
            commits_since_away=len(self._commits_during_away),
            last_commit_sha=(self._commits_during_away[-1] if self._commits_during_away else ""),
        )
