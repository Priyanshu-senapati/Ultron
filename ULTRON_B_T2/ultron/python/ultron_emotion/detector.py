"""Pure emotion analyser — text + tension → EmotionSignal.

No I/O, no state — easy to unit-test. The service layer composes this
with an EWMA decay tracker (state.py) and the bus.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import EmotionConfig
from .lexicon import LEXICON, all_phrases


# Word-boundary matcher built once. Each pattern matches the longest
# phrase first (sorted at module load) so "not bad" wins over "bad".
def _build_pattern() -> re.Pattern[str]:
    phrases = all_phrases()
    # Escape + sort by length descending; alternation does longest-first.
    escaped = sorted((re.escape(p) for p in phrases), key=len, reverse=True)
    pat = r"(?:" + "|".join(escaped) + r")"
    # Require word/space boundaries so "okay" doesn't fire inside "tokay".
    return re.compile(r"(?<![A-Za-z])" + pat + r"(?![A-Za-z])", re.IGNORECASE)


_PATTERN = _build_pattern()


@dataclass
class EmotionSignal:
    """One reading of the user's emotional state."""
    valence: float = 0.0
    arousal: float = 0.0
    frustration: float = 0.0
    confidence: float = 0.0
    source: str = "neutral"
    matched_phrases: list[str] = field(default_factory=list)
    text_preview: str = ""
    ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "frustration": round(self.frustration, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "matched_phrases": list(self.matched_phrases),
            "text_preview": self.text_preview,
            "ts": self.ts,
        }


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def analyze(text: str, *,
            tension: Optional[float] = None,
            cognitive_load: Optional[float] = None,
            cfg: Optional[EmotionConfig] = None,
            ts: Optional[float] = None) -> EmotionSignal:
    """Score a single utterance.

    Args:
      text: the voice_transcript / llm input.
      tension: latest tension EWMA (0..1). Used to corroborate
               lexicon signals; rising tension boosts frustration.
      cognitive_load: latest load (0..1). Currently unused but plumbed
                      so the signature is stable.
      cfg: optional config for thresholds. Defaults are sensible.
      ts: timestamp; defaults to wall-clock now.
    """
    ts = ts if ts is not None else time.time()
    sig = EmotionSignal(ts=ts, source="neutral")
    text = (text or "").strip()
    if not text:
        return sig

    sig.text_preview = text[:120]
    matches = _PATTERN.findall(text.lower())
    if not matches:
        # No lexicon hit — but we may still infer from tension alone.
        if tension is not None and tension >= 0.7:
            sig.arousal = _clip(tension, 0.0, 1.0)
            sig.frustration = _clip((tension - 0.5) * 1.2, 0.0, 0.7)
            sig.confidence = 0.4
            sig.source = "tension_only"
        return sig

    # Sum deltas across matched phrases. Dedup so two mentions of
    # the same word don't double-count.
    seen: set[str] = set()
    v = a = f = 0.0
    for phrase in matches:
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        deltas = LEXICON.get(key)
        if deltas is None:
            continue
        dv, da, df = deltas
        v += dv
        a += da
        f += df
        sig.matched_phrases.append(key)
    sig.valence = _clip(v, -1.0, 1.0)
    sig.arousal = _clip(a, 0.0, 1.0)
    sig.frustration = _clip(f, 0.0, 1.0)

    # Confidence scales with number of matches + lexicon magnitude.
    raw_magnitude = abs(sig.valence) + sig.arousal + sig.frustration
    sig.confidence = _clip(0.4 + 0.15 * len(sig.matched_phrases)
                           + 0.15 * raw_magnitude, 0.0, 0.95)
    sig.source = "lexicon"

    # Tension corroboration: if user said something negative AND
    # physiological signal agrees, bump frustration confidence.
    cfg = cfg or EmotionConfig(ws_url="", ws_token="")
    if (tension is not None
            and tension >= cfg.tension_corroboration_threshold
            and sig.valence < 0):
        sig.frustration = _clip(sig.frustration + 0.2, 0.0, 1.0)
        sig.confidence = _clip(sig.confidence + 0.15, 0.0, 0.99)
        sig.source = "lexicon+tension"

    return sig
