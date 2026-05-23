"""Pure: bus event → optional toast (title, body, footer) with throttle.

Separated from the WS layer so tests can drive synthetic events through
``route()`` without spinning a websocket. The service layer just turns
the returned tuple into a real toast.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from .config import ToastConfig


@dataclass
class ToastSpec:
    title: str
    body: str
    footer: Optional[str] = None


class ToastRouter:
    def __init__(self, cfg: ToastConfig) -> None:
        self._cfg = cfg
        # Last-shown timestamp per kind for throttling.
        self._last_shown: dict[str, float] = {}
        # Track readiness bucket so we only toast on transitions.
        self._last_readiness_bucket: str = ""

    def _throttle_allows(self, key: str, min_interval: float,
                         now: float) -> bool:
        # First call for a key always allows — the throttle is "don't
        # fire again too soon", not "wait min_interval after boot".
        last = self._last_shown.get(key)
        if last is not None and (now - last) < min_interval:
            return False
        self._last_shown[key] = now
        return True

    def route(self, kind: str, payload: dict[str, Any],
              now: Optional[float] = None) -> Optional[ToastSpec]:
        if not self._cfg.enabled:
            return None
        now = now if now is not None else time.time()
        cfg = self._cfg
        payload = payload or {}

        if kind == "wellness_nudge" and cfg.enable_wellness_nudge:
            if not self._throttle_allows(
                    "wellness_nudge", cfg.min_interval_wellness_nudge, now):
                return None
            sub_kind = str(payload.get("kind") or "wellness")
            # Skip streak milestones — they're celebratory, not actionable;
            # let voice handle those.
            if sub_kind == "streak_milestone":
                return None
            body_parts: list[str] = []
            if sub_kind == "low_sleep":
                hours = payload.get("hours")
                target = payload.get("target")
                title = "Low sleep last night"
                if hours is not None and target is not None:
                    body_parts.append(f"{hours:.1f}h vs {target:.1f}h target.")
                body_parts.append("Pace yourself today.")
            else:
                title = f"Wellness: {sub_kind.replace('_', ' ')}"
                body_parts.append(str(payload.get("message")
                                       or "Worth a look."))
            return ToastSpec(title=title, body=" ".join(body_parts),
                              footer="ULTRON · wellness")

        if kind == "flow_state_changed" and cfg.enable_flow_break:
            state = str(payload.get("state") or "")
            prev = str(payload.get("prev_state") or "")
            if not (prev == "active" and state == "broken"):
                return None
            minutes = float(payload.get("duration_minutes") or 0.0)
            if minutes < cfg.flow_break_min_minutes:
                return None
            if not self._throttle_allows(
                    "flow_break", cfg.min_interval_flow_break, now):
                return None
            reason = (payload.get("reason") or "").strip() or "unknown"
            app = (payload.get("last_focus_app") or "").strip()
            body = f"{int(round(minutes))} min, broken by {reason}"
            if app:
                body += f" (in {app})"
            return ToastSpec(title="Flow ended",
                              body=body, footer="ULTRON · flow")

        if kind == "tuning_suggestion" and cfg.enable_tuning_suggestion:
            if not self._throttle_allows(
                    "tuning_suggestion", cfg.min_interval_tuning_suggestion,
                    now):
                return None
            title = str(payload.get("title") or "ULTRON tuning suggestion")
            body = str(payload.get("rationale") or "")
            return ToastSpec(title=title, body=body[:200],
                              footer="ULTRON · selftuner")

        if kind == "self_reflection_written" and cfg.enable_self_reflection:
            if not self._throttle_allows(
                    "self_reflection", cfg.min_interval_self_reflection,
                    now):
                return None
            count = int(payload.get("suggestion_count") or 0)
            date = str(payload.get("date") or "")
            body = f"Daily reflection ready ({count} suggestions)."
            if date:
                body = f"{date}: " + body
            md_path = payload.get("latest_md_path") or payload.get("md_path")
            footer = "ULTRON · reflection"
            return ToastSpec(title="Self-reflection written",
                              body=body, footer=footer)

        if (kind == "readiness_score_update"
                and cfg.enable_readiness_bucket_change):
            bucket = str(payload.get("bucket") or "")
            if not bucket:
                return None
            # Only on transition.
            if bucket == self._last_readiness_bucket:
                return None
            prev = self._last_readiness_bucket
            self._last_readiness_bucket = bucket
            if not prev:
                # First reading after boot — don't toast yet.
                return None
            if not self._throttle_allows(
                    "readiness_change", cfg.min_interval_readiness_change,
                    now):
                return None
            total = payload.get("total")
            body = f"{prev} → {bucket}"
            if total is not None:
                body += f" ({total:.0f}/100)"
            return ToastSpec(title="Readiness shifted",
                              body=body, footer="ULTRON · readiness")

        if kind == "voice_shutdown_initiated" and cfg.enable_voice_shutdown:
            if not self._throttle_allows(
                    "voice_shutdown", cfg.min_interval_voice_shutdown, now):
                return None
            return ToastSpec(title="ULTRON shutting down",
                              body="Saving session context…",
                              footer="ULTRON · shutdown")

        return None
