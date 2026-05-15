"""Rolling EWMA score over recent dopamine marks.

The scorer is pure logic: no I/O. The service feeds it raw events and
matched marks; the scorer returns ``MatchResult`` objects and the
current score so the service can decide whether to publish an alert.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import DopamineConfig


@dataclass
class MatchResult:
    pattern: str
    substring: str
    weight: int
    kind: str


class DopamineScorer:
    def __init__(self, config: DopamineConfig) -> None:
        self._cfg = config
        self._score: float = 0.0

    @property
    def score(self) -> float:
        return self._score

    def reset(self) -> None:
        self._score = 0.0

    def match(self, text: str, patterns: Iterable[dict]) -> list[MatchResult]:
        """Return every pattern whose substring is in ``text`` (case-insensitive).

        Multiple patterns may match the same text — for example, an
        Instagram Reel hit matches both ``instagram_reels`` and the
        broad ``reels_word``. Both are fine; we want the strongest
        signal to win on score, which the additive EWMA naturally does.
        """
        if not text:
            return []
        lower = text.lower()
        out: list[MatchResult] = []
        for p in patterns:
            sub = str(p.get("substring", "")).lower()
            if not sub:
                continue
            if sub in lower:
                out.append(MatchResult(
                    pattern=str(p["name"]),
                    substring=sub,
                    weight=int(p["weight"]),
                    kind=str(p["kind"]),
                ))
        return out

    def apply(self, matches: Iterable[MatchResult]) -> float:
        """Fold ``matches`` into the rolling score and return new value."""
        a = max(0.001, min(1.0, self._cfg.ewma_alpha))
        for m in matches:
            # EWMA toward the mark's weight, with the alpha controlling
            # how fast the rolling score reacts. Newer marks pull the
            # score toward themselves.
            self._score = (1.0 - a) * self._score + a * float(m.weight)
        return self._score

    def decay(self, *, factor: float = 0.99) -> float:
        """Gentle decay applied when no marks fire this tick (called
        from the scheduler so a long stretch of neutral activity drifts
        the score back to zero)."""
        self._score *= max(0.0, min(1.0, factor))
        return self._score
