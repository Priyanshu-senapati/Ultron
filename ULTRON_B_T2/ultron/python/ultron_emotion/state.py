"""EWMA tracker for emotional state with time-aware decay.

A single frustrated utterance shouldn't stick ULTRON in "supportive
mode" for an hour. We blend each new signal into a rolling average
weighted by ``0.5 ** (dt / half_life_secs)`` of the prior value.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .config import EmotionConfig
from .detector import EmotionSignal


@dataclass
class EmotionTracker:
    """Holds the current EWMA emotional state."""
    half_life_secs: float = 600.0

    valence: float = 0.0
    arousal: float = 0.0
    frustration: float = 0.0
    confidence: float = 0.0
    source: str = "init"
    last_text_preview: str = ""
    last_matched: list[str] = None  # type: ignore[assignment]
    last_ts: float = 0.0

    def __post_init__(self) -> None:
        if self.last_matched is None:
            self.last_matched = []

    def _decay_to_now(self, now: float) -> None:
        """Decay current values toward zero proportional to elapsed time.

        Keeps the state from being stuck at a strong reading for hours
        when nothing new comes in.
        """
        if self.last_ts <= 0:
            self.last_ts = now
            return
        dt = max(0.0, now - self.last_ts)
        if dt <= 0:
            return
        # Decay factor — same exponential as the blend below.
        decay = math.pow(0.5, dt / max(60.0, self.half_life_secs))
        self.valence *= decay
        self.arousal *= decay
        self.frustration *= decay
        self.confidence *= decay
        self.last_ts = now

    def apply(self, sig: EmotionSignal) -> None:
        """Blend a new signal into the EWMA using a proper convex
        combination so values stay within [-1, 1] / [0, 1] ranges.

        ``w_move`` is how far we travel from the current value toward
        the new signal — scaled by the signal's confidence so weak
        readings barely budge the state.
        """
        now = sig.ts or time.time()
        # First, decay the existing state to the present.
        self._decay_to_now(now)
        w_move = max(0.05, min(0.95, sig.confidence * 0.6))
        w_keep = 1.0 - w_move
        # Convex combination — sum of weights is 1, so blended value
        # is bounded by min(old, sig) and max(old, sig).
        self.valence = self.valence * w_keep + sig.valence * w_move
        self.arousal = self.arousal * w_keep + sig.arousal * w_move
        self.frustration = (self.frustration * w_keep
                            + sig.frustration * w_move)
        # Final clamp as a belt-and-braces guarantee. Decay rounding +
        # consecutive strong signals could nibble at the boundary
        # otherwise.
        if self.valence > 1.0:  self.valence = 1.0
        if self.valence < -1.0: self.valence = -1.0
        if self.arousal > 1.0:  self.arousal = 1.0
        if self.arousal < 0.0:  self.arousal = 0.0
        if self.frustration > 1.0:  self.frustration = 1.0
        if self.frustration < 0.0: self.frustration = 0.0
        self.confidence = max(self.confidence * 0.7, sig.confidence)
        if sig.matched_phrases or sig.source == "tension_only":
            self.source = sig.source
            self.last_text_preview = sig.text_preview
            self.last_matched = list(sig.matched_phrases)
        self.last_ts = now

    def snapshot(self) -> dict:
        """Frozen view of the current tracker — used for publish payloads."""
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "frustration": round(self.frustration, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "last_text_preview": self.last_text_preview,
            "last_matched": list(self.last_matched),
            "last_ts": self.last_ts,
            "mood_label": self.mood_label(),
        }

    def mood_label(self) -> str:
        """Coarse-grained label for HUD / TTS. One word, predictable."""
        if self.frustration >= 0.5:
            return "frustrated"
        if self.valence <= -0.5:
            return "low"
        if self.valence >= 0.5 and self.arousal >= 0.4:
            return "energised"
        if self.valence >= 0.4:
            return "positive"
        if self.arousal <= 0.2 and self.valence >= -0.2:
            return "calm"
        return "neutral"

    def is_significant_change(self, prior_snapshot: dict,
                              min_delta: float) -> bool:
        """Return True if any dimension moved by >= ``min_delta``."""
        if not prior_snapshot:
            return True
        for k in ("valence", "arousal", "frustration"):
            if abs(self.__getattribute__(k) - float(prior_snapshot.get(k, 0.0))) \
                    >= min_delta:
                return True
        return False
