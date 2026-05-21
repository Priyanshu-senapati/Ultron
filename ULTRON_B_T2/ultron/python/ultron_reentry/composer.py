"""Build the spoken re-entry brief from a ContextSnapshot.

Target: ~25-35 words, < ~10 s of TTS at normal pace. We assemble
section fragments in priority order (welcome → focus → vision → LLM
reply → git delta) and stop adding once we'd exceed the char cap.

The brief is plain prose for TTS — no markdown, no bullets, no
abbreviations the synthesiser will mangle.
"""
from __future__ import annotations

import re

from .config import ReentryConfig
from .context import ContextSnapshot


def _humanise_minutes(secs: float) -> str:
    m = int(round(secs / 60.0))
    if m <= 0:
        return "less than a minute"
    if m == 1:
        return "one minute"
    if m < 60:
        return f"{m} minutes"
    h = m // 60
    rem = m % 60
    if h == 1 and rem == 0:
        return "one hour"
    if rem == 0:
        return f"{h} hours"
    if h == 1:
        return f"one hour and {rem} minutes"
    return f"{h} hours and {rem} minutes"


_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _clip_to_sentence(text: str, max_chars: int) -> str:
    """Trim ``text`` to <= ``max_chars`` ending on a sentence boundary
    when possible. Falls back to a hard cut + ellipsis."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Last sentence boundary inside the cut.
    matches = list(_SENT_BOUNDARY.finditer(cut))
    if matches:
        end = matches[-1].start()
        if end >= max_chars // 3:  # don't keep a stub
            return text[:end + 1].strip()
    # Cut at last space + ellipsis.
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",;:.") + "…"


def _strip_markdown_for_tts(text: str) -> str:
    """Mirror the voice engine's TTS sanitiser. Strip markdown markers
    so the synthesiser doesn't read them aloud."""
    # Code fences and inline code.
    text = re.sub(r"```[^`]*```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Bold / italic / headers.
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    text = re.sub(r"_+([^_]+)_+", r"\1", text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
    # Bullet markers.
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compose_brief(snap: ContextSnapshot, away_seconds: float, cfg: ReentryConfig) -> str:
    """Assemble the brief. Returns the spoken text (already TTS-clean)."""
    parts: list[str] = []
    away_phrase = _humanise_minutes(away_seconds)
    parts.append(f"Welcome back. You were away {away_phrase}.")

    # Focus app + vision label — collapse into one sentence when both
    # exist, otherwise emit whichever we have.
    focus = snap.last_focus_app.strip()
    label = snap.last_visual_label.strip()
    if focus and label:
        parts.append(f"You were in {focus}: {label}.")
    elif focus:
        parts.append(f"Last in {focus}.")
    elif label:
        parts.append(f"Last on screen: {label}.")

    # Last LLM reply — keep it short, prefer sentence boundary.
    llm = _strip_markdown_for_tts(snap.last_llm_text)
    if llm:
        clipped = _clip_to_sentence(llm, cfg.max_llm_quote_chars)
        parts.append(f"Earlier I said: {clipped}")

    # Git delta during absence.
    if cfg.include_git_delta and snap.commits_since_away > 0:
        n = snap.commits_since_away
        word = "commit" if n == 1 else "commits"
        parts.append(f"{n} {word} landed while you were away.")

    brief = " ".join(p.strip() for p in parts if p.strip())
    if len(brief) > cfg.max_brief_chars:
        # Drop trailing fragments until it fits.
        while parts and len(" ".join(parts)) > cfg.max_brief_chars and len(parts) > 1:
            parts.pop()
        brief = " ".join(parts)
        if len(brief) > cfg.max_brief_chars:
            brief = _clip_to_sentence(brief, cfg.max_brief_chars)
    return brief
