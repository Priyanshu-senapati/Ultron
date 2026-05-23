"""In-memory rolling counters for events the daily reflector ingests.

Two observers right now:
  - ToolUsageObserver: every tool_call_audit (n, ok_rate, last_error_ts).
  - EmotionObserver: mood label histogram over the day.

Both store entries with a timestamp and prune by age on each insert so
memory doesn't grow over a long-running session.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


@dataclass
class _ToolEntry:
    ts: float
    ok: bool
    error_reason: str = ""


class ToolUsageObserver:
    def __init__(self, window_secs: float) -> None:
        self._window = max(60.0, float(window_secs))
        self._per_tool: dict[str, deque[_ToolEntry]] = defaultdict(deque)

    def record(self, tool_name: str, ok: bool, error_reason: str = "",
               ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self._prune(ts)
        self._per_tool[tool_name].append(
            _ToolEntry(ts=ts, ok=ok, error_reason=error_reason)
        )

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for q in self._per_tool.values():
            while q and q[0].ts < cutoff:
                q.popleft()

    def stats(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = now if now is not None else time.time()
        self._prune(now)
        out: dict[str, dict[str, Any]] = {}
        for name, q in self._per_tool.items():
            if not q:
                continue
            n = len(q)
            oks = sum(1 for e in q if e.ok)
            errs = n - oks
            # Most recent error reason (if any) for surfacing.
            last_err = ""
            for e in reversed(q):
                if not e.ok and e.error_reason:
                    last_err = e.error_reason
                    break
            out[name] = {
                "n": n,
                "ok": oks,
                "errors": errs,
                "ok_rate": round(oks / n, 3) if n else 0.0,
                "last_error_reason": last_err,
            }
        return out


@dataclass
class _MoodEntry:
    ts: float
    label: str
    valence: float
    arousal: float
    frustration: float


class EmotionObserver:
    def __init__(self, window_secs: float = 86400.0,
                 max_samples: int = 5000) -> None:
        self._window = max(60.0, float(window_secs))
        self._max = max_samples
        self._samples: deque[_MoodEntry] = deque(maxlen=max_samples)

    def record(self, payload: dict[str, Any],
               ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        label = str(payload.get("mood_label") or "neutral")
        try:
            v = float(payload.get("valence", 0.0))
            a = float(payload.get("arousal", 0.0))
            f = float(payload.get("frustration", 0.0))
        except (TypeError, ValueError):
            return
        self._samples.append(_MoodEntry(ts=ts, label=label,
                                        valence=v, arousal=a, frustration=f))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0].ts < cutoff:
            self._samples.popleft()

    def histogram(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for e in self._samples:
            counts[e.label] += 1
        return dict(counts)

    def averages(self) -> dict[str, float]:
        if not self._samples:
            return {"valence": 0.0, "arousal": 0.0, "frustration": 0.0,
                    "samples": 0}
        n = len(self._samples)
        v = sum(e.valence for e in self._samples) / n
        a = sum(e.arousal for e in self._samples) / n
        f = sum(e.frustration for e in self._samples) / n
        return {
            "valence": round(v, 3),
            "arousal": round(a, 3),
            "frustration": round(f, 3),
            "samples": n,
        }

    def peak_frustration(self) -> dict[str, Any] | None:
        if not self._samples:
            return None
        peak = max(self._samples, key=lambda e: e.frustration)
        if peak.frustration < 0.4:
            return None
        return {"ts": peak.ts, "label": peak.label,
                "frustration": round(peak.frustration, 3)}
