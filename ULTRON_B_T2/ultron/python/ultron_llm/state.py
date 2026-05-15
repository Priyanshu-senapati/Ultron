"""
state.py — LiveState: latest data from WS bus subscriptions.

Updated by the WS event loop. Read by ContextAssembler.
Thread-safe via asyncio — all reads/writes happen in the same event loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PatternInfo:
    kind: str
    summary: str
    confidence: float


@dataclass
class LiveState:
    # Latest InsightSnapshot — updated every 5s by O
    snapshot: dict = field(default_factory=dict)
    snapshot_ts: float = 0.0   # monotonic time of last update

    # Latest productivity priors from D (24-element list, None = no data)
    priors: list[Optional[float]] = field(default_factory=lambda: [None] * 24)
    priors_ts: float = 0.0

    # Latest behavioral patterns from D
    patterns: list[PatternInfo] = field(default_factory=list)
    patterns_ts: float = 0.0

    # Forced shard override — set by voice command "switch to brutal mode"
    forced_shard: Optional[str] = None

    # Latest payloads from the ultron-bridges sidecar. Each kind has an
    # independent freshness clock; ContextAssembler drops stale data so
    # the LLM doesn't reason about a track the user finished 10 min ago.
    spotify: dict = field(default_factory=dict)
    spotify_ts: float = 0.0
    browser_tab: dict = field(default_factory=dict)
    browser_tab_ts: float = 0.0
    gh_activity: dict = field(default_factory=dict)
    gh_activity_ts: float = 0.0
    calendar: dict = field(default_factory=dict)
    calendar_ts: float = 0.0
    gmail: dict = field(default_factory=dict)
    gmail_ts: float = 0.0
    app_detail: dict = field(default_factory=dict)
    app_detail_ts: float = 0.0

    # Self-awareness bridges (dev_watch + claude_session)
    git_activity: dict = field(default_factory=dict)
    git_activity_ts: float = 0.0
    code_change: dict = field(default_factory=dict)
    code_change_ts: float = 0.0
    boot_reflection: dict = field(default_factory=dict)
    boot_reflection_ts: float = 0.0
    boot_reflection_acknowledged: bool = False
    claude_session: dict = field(default_factory=dict)
    claude_session_ts: float = 0.0

    def update_snapshot(self, payload: dict) -> None:
        self.snapshot = payload
        self.snapshot_ts = time.monotonic()

    def update_priors(self, payload: dict) -> None:
        raw = payload.get("priors", [None] * 24)
        self.priors = list(raw) if len(raw) == 24 else [None] * 24
        self.priors_ts = time.monotonic()

    def update_patterns(self, payload: dict) -> None:
        self.patterns = [
            PatternInfo(
                kind=p.get("kind", ""),
                summary=p.get("summary", ""),
                confidence=float(p.get("confidence", 0.0)),
            )
            for p in payload.get("patterns", [])
        ]
        self.patterns_ts = time.monotonic()

    # ── Bridge event setters ─────────────────────────────────────────────

    def update_spotify(self, payload: dict) -> None:
        self.spotify = payload or {}
        self.spotify_ts = time.monotonic()

    def update_browser_tab(self, payload: dict) -> None:
        self.browser_tab = payload or {}
        self.browser_tab_ts = time.monotonic()

    def update_gh_activity(self, payload: dict) -> None:
        self.gh_activity = payload or {}
        self.gh_activity_ts = time.monotonic()

    def update_calendar(self, payload: dict) -> None:
        self.calendar = payload or {}
        self.calendar_ts = time.monotonic()

    def update_gmail(self, payload: dict) -> None:
        self.gmail = payload or {}
        self.gmail_ts = time.monotonic()

    def update_app_detail(self, payload: dict) -> None:
        self.app_detail = payload or {}
        self.app_detail_ts = time.monotonic()

    def update_git_activity(self, payload: dict) -> None:
        self.git_activity = payload or {}
        self.git_activity_ts = time.monotonic()

    def update_code_change(self, payload: dict) -> None:
        # Keep only the latest file change — events flow fast on a save spree.
        self.code_change = payload or {}
        self.code_change_ts = time.monotonic()

    def update_boot_reflection(self, payload: dict) -> None:
        self.boot_reflection = payload or {}
        self.boot_reflection_ts = time.monotonic()
        # Reset "already mentioned" flag — a new boot deserves a fresh
        # acknowledgement opportunity.
        self.boot_reflection_acknowledged = False

    def update_claude_session(self, payload: dict) -> None:
        self.claude_session = payload or {}
        self.claude_session_ts = time.monotonic()

    # ── convenience accessors ────────────────────────────────────────────

    @property
    def cognitive_load(self) -> float:
        return float(self.snapshot.get("cognitive_load", 0.0))

    @property
    def tension(self) -> float:
        return float(self.snapshot.get("tension", 0.0))

    @property
    def tension_band(self) -> str:
        return str(self.snapshot.get("tension_band", "calm"))

    @property
    def focus_category(self) -> str:
        return str(self.snapshot.get("focus_category", "unknown"))

    @property
    def focus_app(self) -> str:
        return str(self.snapshot.get("focus_app", ""))

    @property
    def visual_label(self) -> Optional[str]:
        return self.snapshot.get("visual_label")

    @property
    def circadian_phase(self) -> str:
        return str(self.snapshot.get("circadian_phase", "unknown"))

    @property
    def wpm(self) -> float:
        return float(self.snapshot.get("wpm", 0.0))

    @property
    def fatigue_flag(self) -> bool:
        return bool(self.snapshot.get("fatigue_flag", False))

    @property
    def snapshot_age_secs(self) -> float:
        if self.snapshot_ts == 0.0:
            return float("inf")
        return time.monotonic() - self.snapshot_ts

    # ── Bridge freshness helpers ─────────────────────────────────────────

    @staticmethod
    def _age(ts: float) -> float:
        if ts == 0.0:
            return float("inf")
        return time.monotonic() - ts

    def spotify_age_secs(self) -> float:
        return self._age(self.spotify_ts)

    def browser_tab_age_secs(self) -> float:
        return self._age(self.browser_tab_ts)

    def gh_activity_age_secs(self) -> float:
        return self._age(self.gh_activity_ts)

    def calendar_age_secs(self) -> float:
        return self._age(self.calendar_ts)

    def gmail_age_secs(self) -> float:
        return self._age(self.gmail_ts)

    def app_detail_age_secs(self) -> float:
        return self._age(self.app_detail_ts)

    def git_activity_age_secs(self) -> float:
        return self._age(self.git_activity_ts)

    def code_change_age_secs(self) -> float:
        return self._age(self.code_change_ts)

    def boot_reflection_age_secs(self) -> float:
        return self._age(self.boot_reflection_ts)

    def claude_session_age_secs(self) -> float:
        return self._age(self.claude_session_ts)
