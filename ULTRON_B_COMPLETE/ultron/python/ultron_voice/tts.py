"""Text-to-speech with a primary/fallback backend chain.

Primary: **Piper** — local, CPU, ~100ms first-token latency, fully offline,
zero VRAM cost. Voices download once to ``%APPDATA%/ULTRON/models/piper/``.

Fallback: **Edge-TTS** — Microsoft's free streaming endpoint. Better
prosody than Piper, but requires network access. Used automatically if
Piper fails (model missing on first run, ONNX runtime exception, etc.).

Both backends produce audio bytes that ``AudioPlayer.play()`` accepts:

- Piper returns raw PCM (16-bit signed, 22050Hz mono). Player decodes
  with ``fmt="pcm"``.
- Edge-TTS returns a WAV-encoded byte stream. Player decodes with ``fmt="wav"``.

The synthesise method never raises on backend failures — it logs and
returns ``b""``. The orchestrator interprets empty bytes as "no audio
to play" and just transitions through SPEAKING → IDLE without calling
the player. This keeps the voice pipeline running even when the network
is down and Piper is broken.

Truncation
----------

LLM responses can be long. We never want to TTS a 3,000-character
response — it's tedious, blocks barge-in for minutes. ``truncate_to_limit``
clips to the last full sentence within ``max_chars``, falling back to a
hard character cut only if no sentence boundary exists. Sentence
boundaries: ``.``, ``!``, ``?`` followed by whitespace.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Optional

logger = logging.getLogger("ultron.voice.tts")


# Matches `.`, `!`, `?` followed by whitespace or end-of-string. We
# include trailing quotes/parens so "She said, 'go ahead.'" cuts after
# the closing quote, not before it.
_SENTENCE_END = re.compile(r'[.!?][\'")\]]*(?=\s|$)')


class TTSError(Exception):
    """Raised internally by backends; never propagated to callers of synthesize()."""


class TTSEngine:
    """Coordinates Piper + Edge-TTS with automatic fallback.

    Construct once at startup. ``synthesize()`` is fully async-safe — the
    Piper call wraps its sync API in ``run_in_executor``; Edge-TTS is
    natively async.
    """

    def __init__(
        self,
        backend: str,
        piper_voice: str,
        edge_tts_voice: str,
        piper_model_path: str = "",
    ) -> None:
        self.backend = backend.lower()
        self.piper_voice = piper_voice
        self.edge_tts_voice = edge_tts_voice
        # Empty string → let piper-tts auto-resolve; otherwise honour it.
        self.piper_model_path = piper_model_path
        # Lazily loaded so construction is cheap and tests don't pay model
        # download costs.
        self._piper_voice = None  # type: ignore[assignment]
        self._piper_failed_permanently = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> bytes:
        """Synthesise ``text``. Returns audio bytes, ``b""`` on failure.

        The returned format depends on which backend won:

        - Piper succeeded → raw PCM (`fmt="pcm"` for AudioPlayer)
        - Edge-TTS used  → WAV bytes (`fmt="wav"`)
        - Both failed    → ``b""`` (no playback)

        The caller can disambiguate by checking the magic bytes (``RIFF``
        for WAV) — or use the convenience helper :meth:`format_of` below.
        Empty input returns empty bytes immediately.
        """
        text = text.strip() if text else ""
        if not text:
            return b""

        # ----- Try Piper first if it's the configured primary -----
        if self.backend == "piper" and not self._piper_failed_permanently:
            try:
                return await self._synthesize_piper(text)
            except TTSError as exc:
                logger.warning("Piper failed (%s); falling back to Edge-TTS.", exc)
                # Don't permanently disable; transient ONNX errors recover.
            except Exception as exc:
                # Unexpected: log loudly, disable Piper for this run.
                logger.error(
                    "Piper raised unexpectedly (%s); disabling for this session.",
                    exc,
                )
                self._piper_failed_permanently = True

        # ----- Fall back to Edge-TTS -----
        try:
            return await self._synthesize_edge(text)
        except TTSError as exc:
            logger.warning("Edge-TTS failed (%s); no audio for this response.", exc)
            return b""
        except Exception as exc:
            logger.error("Edge-TTS raised unexpectedly (%s); no audio.", exc)
            return b""

    # ------------------------------------------------------------------
    # Truncation
    # ------------------------------------------------------------------

    def truncate_to_limit(self, text: str, max_chars: int) -> str:
        """Trim to the last complete sentence within ``max_chars``.

        If no sentence boundary exists within the limit, hard-cut at
        ``max_chars`` — we never speak more than the user wanted. Empty
        / short input is returned unchanged.
        """
        if not text or len(text) <= max_chars:
            return text
        head = text[:max_chars]
        # Find the last sentence-end inside `head`.
        last = None
        for m in _SENTENCE_END.finditer(head):
            last = m
        if last is None:
            # No sentence boundary — hard cut. Rare but defended.
            return head
        # `last.end()` points just past the punctuation; include it.
        cut = head[: last.end()].rstrip()
        return cut

    # ------------------------------------------------------------------
    # Helpers exposed for tests / advanced callers
    # ------------------------------------------------------------------

    @staticmethod
    def format_of(audio: bytes) -> str:
        """Return ``"wav"`` if the buffer looks like a RIFF/WAVE file,
        else ``"pcm"``. ``b""`` returns ``"empty"``.
        """
        if not audio:
            return "empty"
        if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
            return "wav"
        return "pcm"

    # ------------------------------------------------------------------
    # Piper backend
    # ------------------------------------------------------------------

    async def _synthesize_piper(self, text: str) -> bytes:
        """Run Piper synchronously off the event loop.

        Loads the voice on first call. Subsequent calls reuse it. The
        ``run_in_executor(None, ...)`` indirection keeps the asyncio
        loop responsive — Piper inference is ~100ms but we still don't
        want it to block hotkey / event handling.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._piper_synth_sync, text)

    def _piper_synth_sync(self, text: str) -> bytes:
        # Lazy import — keeps module importable without piper-tts installed.
        try:
            from piper.voice import PiperVoice  # type: ignore[import-not-found]
        except Exception as exc:
            raise TTSError(f"piper-tts not installed or import failed: {exc}") from exc

        if self._piper_voice is None:
            # Loading the voice is the slow part (~500ms). Cache it.
            if not self.piper_model_path:
                # Without an explicit path, piper-tts looks in well-known
                # locations. If you've not downloaded the voice it'll
                # raise — we let that propagate as TTSError below.
                try:
                    self._piper_voice = PiperVoice.load(self.piper_voice)  # type: ignore[arg-type]
                except Exception as exc:
                    raise TTSError(
                        f"could not load Piper voice {self.piper_voice!r}: {exc}"
                    ) from exc
            else:
                try:
                    self._piper_voice = PiperVoice.load(self.piper_model_path)
                except Exception as exc:
                    raise TTSError(
                        f"could not load Piper model at {self.piper_model_path!r}: {exc}"
                    ) from exc
            logger.info("piper voice loaded: %s", self.piper_voice)

        # Stream PCM into an in-memory buffer.
        buf = io.BytesIO()
        try:
            self._piper_voice.synthesize(text, buf)
        except Exception as exc:
            raise TTSError(f"piper synthesize failed: {exc}") from exc
        data = buf.getvalue()
        if not data:
            raise TTSError("piper produced 0 bytes — model may be misconfigured")
        return data

    # ------------------------------------------------------------------
    # Edge-TTS backend
    # ------------------------------------------------------------------

    async def _synthesize_edge(self, text: str) -> bytes:
        """Call Microsoft's free Edge-TTS streaming endpoint."""
        try:
            import edge_tts  # type: ignore[import-not-found]
        except Exception as exc:
            raise TTSError(f"edge-tts not installed or import failed: {exc}") from exc

        try:
            communicate = edge_tts.Communicate(text, self.edge_tts_voice)
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and "data" in chunk:
                    chunks.append(chunk["data"])
            audio = b"".join(chunks)
        except Exception as exc:
            raise TTSError(f"edge-tts stream failed: {exc}") from exc

        if not audio:
            raise TTSError("edge-tts returned no audio chunks")
        return audio
