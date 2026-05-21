"""Snapshot dataclass — the pure-data shape of a context packet.

The service fills this in from inbound bus events. The markdown layer
formats it for human reading; the JSON sibling is just ``as_dict()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ContextSnapshot:
    saved_ts: float = 0.0
    reason: str = "heartbeat"
    user_name: str = "the user"

    # ── Focus ─────────────────────────────────────────────────────────
    focus_app: str = ""
    focus_category: str = ""
    focus_app_ts: float = 0.0
    visual_label: str = ""
    visual_label_ts: float = 0.0

    # ── Last conversation turn ────────────────────────────────────────
    last_user_transcript: str = ""
    last_user_ts: float = 0.0
    last_llm_response: str = ""
    last_llm_shard: str = ""
    last_llm_ts: float = 0.0

    # ── Flow ──────────────────────────────────────────────────────────
    flow_state: str = "idle"
    flow_session_start_ts: float = 0.0
    last_flow_break_minutes: float = 0.0
    last_flow_break_reason: str = ""
    last_flow_break_app: str = ""
    last_flow_break_ts: float = 0.0

    # ── Readiness ─────────────────────────────────────────────────────
    readiness_total: Optional[float] = None
    readiness_bucket: str = ""
    readiness_components: list[dict[str, Any]] = field(default_factory=list)
    readiness_ts: float = 0.0

    # ── Interrupts ────────────────────────────────────────────────────
    interrupts_today_count: int = 0
    interrupts_top_source: str = ""
    interrupts_avg_recovery_secs: Optional[float] = None
    interrupts_ts: float = 0.0

    # ── Git (last 24h) ────────────────────────────────────────────────
    recent_commits: list[dict[str, Any]] = field(default_factory=list)

    # ── Claude Code session ───────────────────────────────────────────
    claude_session_snippet: str = ""
    claude_session_ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "saved_ts": self.saved_ts,
            "reason": self.reason,
            "user_name": self.user_name,
            "focus": {
                "app": self.focus_app,
                "category": self.focus_category,
                "ts": self.focus_app_ts,
            },
            "vision": {
                "label": self.visual_label,
                "ts": self.visual_label_ts,
            },
            "last_turn": {
                "user_transcript": self.last_user_transcript,
                "user_ts": self.last_user_ts,
                "llm_response": self.last_llm_response,
                "llm_shard": self.last_llm_shard,
                "llm_ts": self.last_llm_ts,
            },
            "flow": {
                "state": self.flow_state,
                "session_start_ts": self.flow_session_start_ts,
                "last_break_minutes": self.last_flow_break_minutes,
                "last_break_reason": self.last_flow_break_reason,
                "last_break_app": self.last_flow_break_app,
                "last_break_ts": self.last_flow_break_ts,
            },
            "readiness": {
                "total": self.readiness_total,
                "bucket": self.readiness_bucket,
                "components": self.readiness_components,
                "ts": self.readiness_ts,
            },
            "interrupts_today": {
                "count": self.interrupts_today_count,
                "top_source": self.interrupts_top_source,
                "avg_recovery_secs": self.interrupts_avg_recovery_secs,
                "ts": self.interrupts_ts,
            },
            "recent_commits": self.recent_commits,
            "claude_session": {
                "snippet": self.claude_session_snippet,
                "ts": self.claude_session_ts,
            },
        }
