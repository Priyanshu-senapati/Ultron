"""Curated emotion lexicon — keyword/phrase → emotion deltas.

Each entry contributes to three dimensions:
  valence     : -1.0 (very negative) → +1.0 (very positive)
  arousal     :  0.0 (calm)          → +1.0 (energised / agitated)
  frustration :  0.0 (none)          → +1.0 (high)

Why a hand-curated dict instead of a sentiment library: ULTRON's
voice input is short imperative-ish utterances ("this is broken",
"perfect", "ugh") — sentiment models tuned on tweets / reviews
mislabel these. A small explicit table is both faster and more
accurate for the specific genre.

The detector matches longest phrase first to keep "not bad"
(+valence) from being scored as "bad" (-valence).
"""
from __future__ import annotations

# Each tuple: (valence_delta, arousal_delta, frustration_delta).
# Matched case-insensitively on word boundaries.
LEXICON: dict[str, tuple[float, float, float]] = {
    # ── Frustration markers (high-signal) ────────────────────────────
    "ugh":                (-0.6, 0.4, 0.7),
    "argh":               (-0.6, 0.4, 0.7),
    "frustrating":        (-0.6, 0.3, 0.8),
    "frustrated":         (-0.6, 0.3, 0.8),
    "annoying":           (-0.5, 0.3, 0.7),
    "annoyed":            (-0.5, 0.3, 0.7),
    "this is broken":     (-0.7, 0.4, 0.9),
    "doesn't work":       (-0.6, 0.3, 0.8),
    "doesnt work":        (-0.6, 0.3, 0.8),
    "not working":        (-0.6, 0.3, 0.8),
    "isn't working":      (-0.6, 0.3, 0.8),
    "still not":          (-0.5, 0.3, 0.7),
    "again":              (-0.2, 0.1, 0.3),
    "i'm stuck":          (-0.5, 0.2, 0.8),
    "stuck":              (-0.4, 0.2, 0.7),
    "can't figure":       (-0.4, 0.2, 0.6),
    "cant figure":        (-0.4, 0.2, 0.6),
    "what the hell":      (-0.5, 0.6, 0.7),
    "wtf":                (-0.5, 0.6, 0.7),
    "damn":               (-0.4, 0.5, 0.5),
    "fuck":               (-0.5, 0.7, 0.7),
    "shit":               (-0.4, 0.5, 0.5),
    "wrong":              (-0.3, 0.2, 0.4),
    "no no no":           (-0.6, 0.5, 0.7),
    "stop doing":         (-0.4, 0.4, 0.6),
    "why is it":          (-0.2, 0.2, 0.4),

    # ── Negation patterns (longest-first matching handles "not bad") ─
    "not bad":            (+0.3, 0.0, 0.0),     # mildly positive
    "not great":          (-0.3, 0.0, 0.2),
    "not good":           (-0.4, 0.0, 0.3),
    "not really":         (-0.2, 0.0, 0.0),

    # ── Negative valence (no specific frustration) ──────────────────
    "tired":              (-0.4, -0.4, 0.0),
    "exhausted":          (-0.5, -0.5, 0.1),
    "burned out":         (-0.5, -0.3, 0.3),
    "burnt out":          (-0.5, -0.3, 0.3),
    "down":               (-0.3, -0.2, 0.0),
    "sad":                (-0.5, -0.2, 0.0),
    "lonely":             (-0.4, -0.2, 0.0),
    "anxious":            (-0.4, 0.5, 0.3),
    "worried":            (-0.4, 0.4, 0.2),
    "stressed":           (-0.4, 0.5, 0.4),
    "overwhelmed":        (-0.5, 0.6, 0.5),
    "bored":              (-0.2, -0.3, 0.0),
    "meh":                (-0.2, -0.2, 0.0),
    "ok":                 ( 0.0, 0.0, 0.0),     # neutral; explicit so it doesn't hit other tokens
    "okay":               ( 0.0, 0.0, 0.0),
    "fine":               ( 0.1, 0.0, 0.0),
    "bad":                (-0.4, 0.1, 0.2),
    "terrible":           (-0.6, 0.2, 0.3),
    "awful":              (-0.6, 0.2, 0.3),
    "hate":               (-0.6, 0.4, 0.4),
    "i hate":             (-0.6, 0.4, 0.5),
    "no":                 (-0.2, 0.1, 0.1),

    # ── Positive valence ───────────────────────────────────────────
    "perfect":            (+0.7, 0.3, -0.3),
    "amazing":            (+0.8, 0.5, -0.3),
    "awesome":            (+0.7, 0.5, -0.3),
    "great":              (+0.5, 0.2, -0.2),
    "good":               (+0.3, 0.1, -0.1),
    "nice":               (+0.4, 0.1, -0.1),
    "love":               (+0.6, 0.3, -0.2),
    "love it":            (+0.8, 0.4, -0.3),
    "love this":          (+0.8, 0.4, -0.3),
    "thanks":             (+0.4, 0.0, -0.1),
    "thank you":          (+0.5, 0.0, -0.2),
    "appreciate":         (+0.5, 0.0, -0.2),
    "yes":                (+0.3, 0.2, -0.1),
    "yeah":               (+0.2, 0.1, 0.0),
    "yay":                (+0.7, 0.6, -0.3),
    "let's go":           (+0.5, 0.6, -0.2),
    "lets go":            (+0.5, 0.6, -0.2),
    "finally":            (+0.4, 0.3, -0.3),    # relief
    "got it":             (+0.3, 0.1, -0.2),
    "makes sense":        (+0.3, 0.0, -0.2),
    "exactly":            (+0.4, 0.2, -0.2),

    # ── Calm / contemplative (low arousal positive) ────────────────
    "relaxed":            (+0.3, -0.4, 0.0),
    "chill":              (+0.2, -0.4, 0.0),
    "ready":              (+0.3, 0.2, 0.0),
    "focused":            (+0.3, 0.2, -0.2),
    "in the zone":        (+0.4, 0.3, -0.3),

    # ── High arousal (signed by adjacent words / phrases) ──────────
    "hurry":              ( 0.0, 0.5, 0.2),
    "quickly":            ( 0.0, 0.4, 0.1),
    "right now":          ( 0.0, 0.5, 0.2),
    "asap":               ( 0.0, 0.6, 0.3),

    # ── Help-seeking softeners (slightly negative valence) ─────────
    "please help":        (-0.2, 0.3, 0.4),
    "help me":            (-0.1, 0.2, 0.3),
    "can you help":       ( 0.0, 0.1, 0.1),     # routine — not really negative
}


def all_phrases() -> list[str]:
    """Sorted longest-first so multi-word entries win on overlap."""
    return sorted(LEXICON.keys(), key=len, reverse=True)
