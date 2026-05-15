"""DataClassifier — classify strings as LOCAL_ONLY / ANONYMIZED / SHAREABLE.

A string's class is the *most restrictive* category that any of its
patterns or characteristics imply. LOCAL_ONLY > ANONYMIZED > SHAREABLE.

The classifier is pure (no I/O, no LLM) so it's cheap to call on every
outbound payload — no rate-limiting needed.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Iterable


class DataClass(str, Enum):
    LOCAL_ONLY = "local_only"
    ANONYMIZED = "anonymized"
    SHAREABLE = "shareable"

    @classmethod
    def most_restrictive(cls, *classes: "DataClass") -> "DataClass":
        """Return the most restrictive class from the set."""
        order = {cls.LOCAL_ONLY: 3, cls.ANONYMIZED: 2, cls.SHAREABLE: 1}
        return max(classes, key=lambda c: order[c]) if classes else cls.SHAREABLE


class DataClassifier:
    def __init__(self, local_only_patterns: Iterable[str]) -> None:
        self._patterns: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in local_only_patterns
        ]

    # ── Generic ─────────────────────────────────────────────────────────

    def classify(self, text: str) -> DataClass:
        """Classify any text. LOCAL_ONLY if any LOCAL_ONLY pattern hits."""
        if not text:
            return DataClass.SHAREABLE
        for pat in self._patterns:
            if pat.search(text):
                return DataClass.LOCAL_ONLY
        return DataClass.SHAREABLE

    # ── Specific kinds (defaults documented per ULTRON's policy) ────────

    def classify_window_title(self, title: str) -> DataClass:
        """Window titles often contain document names / repo paths → LOCAL_ONLY."""
        return DataClass.LOCAL_ONLY

    def classify_file_path(self, path: str) -> DataClass:
        """Filesystem paths leak username + tree structure → LOCAL_ONLY."""
        return DataClass.LOCAL_ONLY

    def classify_tension_score(self, score: float) -> DataClass:
        """Pure numeric metrics with no PII → SHAREABLE."""
        # Argument unused at runtime; type-checked at call site.
        del score
        return DataClass.SHAREABLE

    def classify_visual_label(self, label: str) -> DataClass:
        """LLaVA labels like 'writing rust code' — usually generic, but
        run through pattern check in case the label captured a filename
        or username."""
        return self.classify(label)

    def classify_payload(self, payload: dict) -> dict[str, DataClass]:
        """Classify each top-level string value in a payload.

        Returns a map {key: DataClass}. Numeric/bool/None values default
        to SHAREABLE.
        """
        out: dict[str, DataClass] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                # Special keys with policy override.
                k_low = key.lower()
                if k_low in {"focus_app", "window_title", "file_path", "path"}:
                    out[key] = DataClass.LOCAL_ONLY
                elif k_low in {"visual_label", "label"}:
                    out[key] = self.classify_visual_label(value)
                else:
                    out[key] = self.classify(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                out[key] = DataClass.SHAREABLE
            elif isinstance(value, dict):
                # Recursive — pick the most restrictive class found.
                sub = self.classify_payload(value)
                out[key] = DataClass.most_restrictive(*sub.values()) if sub else DataClass.SHAREABLE
            elif isinstance(value, list):
                # Most restrictive across list items.
                sub_classes = []
                for item in value:
                    if isinstance(item, str):
                        sub_classes.append(self.classify(item))
                    elif isinstance(item, dict):
                        sub = self.classify_payload(item)
                        if sub:
                            sub_classes.append(DataClass.most_restrictive(*sub.values()))
                out[key] = DataClass.most_restrictive(*sub_classes) if sub_classes else DataClass.SHAREABLE
            else:
                out[key] = DataClass.SHAREABLE
        return out
