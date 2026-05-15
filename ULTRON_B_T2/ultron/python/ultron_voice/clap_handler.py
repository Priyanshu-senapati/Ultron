"""Clap protocol handler.

Module H's input monitor publishes `input_activity` events with
``kind="clap"`` and a ``clap_count`` field. We map those counts to
voice actions:

==========  =========================================================
Claps       Action
==========  =========================================================
1           Wake — same as hotkey press, start listening
2           Status report — ask the LLM what the user was just doing
3           Clipboard:
              * ≤ 200 chars → speak verbatim (no LLM round-trip)
              * > 200 chars → ask the LLM to summarise in one sentence
4           Replay the last TTS audio
≥ 5         Ignored (cat on the keyboard, etc.)
==========  =========================================================

The handler does **not** know about the state machine directly. It
expresses each action as a set of small callbacks the orchestrator
provides at construction time. This keeps the handler easy to test —
the test suite can pass in pure spies — and avoids a tangle of cross
references between handler, state machine, recorder, and bridge.

Replay buffer
-------------

We keep ``last_audio`` and ``last_audio_fmt`` in memory so action 4
can replay without re-running TTS. The orchestrator pushes the most
recent audio into the handler after every TTS synthesis. If no
audio has been spoken yet, action 4 is a silent no-op (logged at
``info`` so it's not surprising).
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("ultron.voice.clap")


# Below this length we speak the clipboard verbatim instead of summarising.
CLIPBOARD_DIRECT_SPEAK_THRESHOLD: int = 200

# Hard cap on how much clipboard text we even send to the LLM. Anything
# longer is truncated — we don't want a 10 MB clipboard to overflow C's
# context window or cost Priyanshu real money on a cloud shard.
CLIPBOARD_LLM_MAX_CHARS: int = 500

# Prompts used for action 2 / action 3-long. Kept as module constants
# so tests can pin the exact strings going to the bus.
STATUS_REPORT_PROMPT: str = (
    "Give me a brief 2-sentence status of what I was just working on "
    "based on what you know."
)


def _clipboard_summary_prompt(text: str) -> str:
    """Build the LLM prompt for a long-clipboard summarisation."""
    trimmed = text[:CLIPBOARD_LLM_MAX_CHARS]
    return f"Summarize this text in one sentence: {trimmed}"


# Callback signatures expressed for readability — duck-typed at runtime.
WakeCallback = Callable[[], Awaitable[None]]
PublishCallback = Callable[[str, dict], Awaitable[None]]
SpeakCallback = Callable[[str], Awaitable[None]]
PlayAudioCallback = Callable[[bytes, str], Awaitable[None]]
ClipboardReader = Callable[[], str]


class ClapHandler:
    """Dispatch clap counts to voice actions.

    Construct with callbacks the orchestrator owns:

    * ``wake`` — start a listening session (same as hotkey press)
    * ``publish`` — publish a custom event onto the WS bus (used to fire
      `voice_transcript` for actions 2 and long-clipboard 3)
    * ``speak`` — synthesise + play TTS for the given text (short
      clipboard, action 3 fast path). Must be ``async``.
    * ``play_audio`` — replay raw audio bytes (action 4). Async.
    * ``read_clipboard`` — synchronous clipboard read. Returns ``""`` on
      failure. Default is ``pyperclip.paste`` wrapped to swallow errors.

    The ``last_audio`` / ``last_audio_fmt`` slots are mutated by the
    orchestrator after each successful TTS synthesis. Action 4 reads
    them — never write from outside the orchestrator.
    """

    def __init__(
        self,
        wake: WakeCallback,
        publish: PublishCallback,
        speak: SpeakCallback,
        play_audio: PlayAudioCallback,
        read_clipboard: Optional[ClipboardReader] = None,
    ) -> None:
        self.wake = wake
        self.publish = publish
        self.speak = speak
        self.play_audio = play_audio
        self.read_clipboard = read_clipboard or _default_clipboard_reader
        # Mutated by the orchestrator post-TTS. Public on purpose.
        self.last_audio: bytes = b""
        self.last_audio_fmt: str = "pcm"

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def on_clap(self, clap_count: int) -> None:
        """Handle one ``input_activity`` clap event.

        Never raises — every failure is logged. Unknown counts (≥ 5 or
        ≤ 0) are dropped silently except for a debug log.
        """
        logger.info("clap count=%d", clap_count)
        try:
            if clap_count == 1:
                await self._activate_listening()
            elif clap_count == 2:
                await self._status_report()
            elif clap_count == 3:
                await self._read_clipboard()
            elif clap_count == 4:
                await self._replay_last()
            else:
                logger.debug("clap count %d ignored", clap_count)
        except Exception as exc:
            # We never let a clap handler explode the voice process.
            # State machine remains whatever it was.
            logger.error("clap action %d failed: %s", clap_count, exc)

    # ------------------------------------------------------------------
    # Action 1 — wake
    # ------------------------------------------------------------------

    async def _activate_listening(self) -> None:
        """Same as a hotkey press — defer to the orchestrator's wake fn."""
        await self.wake()

    # ------------------------------------------------------------------
    # Action 2 — status report
    # ------------------------------------------------------------------

    async def _status_report(self) -> None:
        """Publish a `voice_transcript` carrying the status-report prompt.

        C subscribes to `voice_transcript`, so this triggers an
        `llm_response` event the same way speech would — meaning the
        orchestrator's existing `llm_response` handler will TTS the
        result. No special path needed.
        """
        await self.publish(
            "voice_transcript",
            {
                "text": STATUS_REPORT_PROMPT,
                "duration_secs": 0.0,
                "confidence": 1.0,
                "activation": "clap_2",
                "ts_unix_ms": int(time.time() * 1000),
            },
        )

    # ------------------------------------------------------------------
    # Action 3 — clipboard
    # ------------------------------------------------------------------

    async def _read_clipboard(self) -> None:
        """Short clipboard: TTS verbatim. Long: summarise via LLM.

        We split at :data:`CLIPBOARD_DIRECT_SPEAK_THRESHOLD` because
        the user's intent differs by length: short snippets (a URL,
        an OTP, a short paragraph) are best read aloud unchanged.
        Long content (a doc, a code listing) is better as a summary.
        """
        try:
            text = self.read_clipboard()
        except Exception as exc:
            logger.warning("clipboard read failed: %s", exc)
            return
        text = (text or "").strip()
        if not text:
            logger.info("clipboard empty — nothing to read")
            return

        if len(text) <= CLIPBOARD_DIRECT_SPEAK_THRESHOLD:
            logger.info("clipboard: speaking verbatim (%d chars)", len(text))
            await self.speak(text)
            return

        logger.info("clipboard: summarising via LLM (%d chars)", len(text))
        await self.publish(
            "voice_transcript",
            {
                "text": _clipboard_summary_prompt(text),
                "duration_secs": 0.0,
                "confidence": 1.0,
                "activation": "clap_3",
                "ts_unix_ms": int(time.time() * 1000),
            },
        )

    # ------------------------------------------------------------------
    # Action 4 — replay
    # ------------------------------------------------------------------

    async def _replay_last(self) -> None:
        """Play back the last TTS audio if we have any."""
        if not self.last_audio:
            logger.info("replay requested but no audio buffered yet")
            return
        await self.play_audio(self.last_audio, self.last_audio_fmt)


def _default_clipboard_reader() -> str:
    """Best-effort clipboard read. Returns ``""`` on any failure.

    Lazy-imports `pyperclip` so the package stays importable on systems
    without it (or without the OS clipboard daemon configured).
    """
    try:
        import pyperclip  # type: ignore[import-not-found]
    except Exception as exc:
        logger.debug("pyperclip import failed: %s", exc)
        return ""
    try:
        return pyperclip.paste() or ""
    except Exception as exc:
        logger.warning("pyperclip.paste() failed: %s", exc)
        return ""
