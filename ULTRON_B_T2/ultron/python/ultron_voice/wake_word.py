"""Wake-word listener.

Continuously records short speech segments from the mic, transcribes each
via the already-loaded Whisper model, and fires a callback whenever the
configured wake word is found at the start of the transcript.

Design
------

A single asyncio task runs the loop. Each iteration:

1. Wait until the engine state is IDLE (i.e. nothing else is using the mic).
2. Open an input stream, record a VAD-bounded segment (just like the hotkey
   recorder, but with a shorter cap).
3. Transcribe.
4. If the transcript starts with (or contains near the beginning) one of
   the wake words, strip it and fire ``on_wake_word(query)`` with the rest.
   If the rest is empty, fire ``on_wake_word("")`` so the caller can decide
   whether to treat that as a hotkey-style "listen for the real query".

The listener is **paused** while the engine is recording for a hotkey press
or while ULTRON is speaking. Resuming is automatic — the loop polls the
``is_busy`` callback every ~250ms.

This module reuses the engine's existing :class:`WhisperSTT` instance — we
never load a second model. Wake-word detection is essentially a free side
effect of having Whisper resident in memory.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, Optional

import numpy as np

from .vad import CHUNK_SAMPLES, SileroVAD
from .stt import WhisperSTT

logger = logging.getLogger("ultron.voice.wake")

WakeCallback = Callable[[str], Awaitable[None]]
PublishCallback = Callable[[str, dict], Awaitable[None]]
BusyPredicate = Callable[[], bool]


def _normalise(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace.

    Whisper returns transcripts like ``"Hey, Ultron."`` or
    ``"hey   ultron"``. Without collapsing internal whitespace, the
    naive ``norm.find("hey ultron")`` misses because the punctuation
    replacement leaves a double-space gap (``"hey  ultron"``).
    """
    stripped = re.sub(r"[^\w\s']", " ", text.lower())
    return re.sub(r"\s+", " ", stripped).strip()


def amplify_to_peak(audio: np.ndarray, target_peak: float = 0.3,
                    max_gain: float = 20.0) -> np.ndarray:
    """Scale a float32 mono buffer toward a target peak amplitude.

    Whisper transcribes much more reliably when peak >= ~0.1. Windows
    laptops with low mic gain often produce 0.005-0.05. This boosts
    them in software, capped at ``max_gain`` so we don't blow up pure
    silence into white noise.

    No-op if the audio is already at or above ``target_peak``.
    """
    if audio is None or audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak >= target_peak or peak < 1e-5:
        return audio
    gain = min(target_peak / peak, max_gain)
    boosted = audio * gain
    # Hard clip just in case rounding edges push past 1.0.
    return np.clip(boosted, -1.0, 1.0).astype(np.float32)


class WakeWordListener:
    """Background mic listener that fires on wake-word detection."""

    # Phrases that trigger a graceful shutdown. Checked BEFORE the wake
    # word — saying "bye ultron" shouldn't bring up a listening prompt,
    # it should send ULTRON to sleep.
    # Whisper homophones for "bye": "by", "buy", "bai", "bi". Include
    # them so the user can fire the shutdown without enunciating
    # carefully. Likewise "good night"/"goodnight" — natural shutoff phrases.
    SHUTDOWN_PHRASES: tuple[str, ...] = (
        "bye ultron", "by ultron", "buy ultron", "bai ultron", "bi ultron",
        "goodbye ultron", "good bye ultron",
        "goodnight ultron", "good night ultron",
        "shutdown ultron", "shut down ultron",
        "ultron shutdown", "ultron shut down",
        "ultron stop", "stop ultron",
        "see you ultron", "see ya ultron",
        "go to sleep ultron", "sleep ultron", "ultron sleep",
        "power down ultron", "power off ultron",
    )

    def __init__(
        self,
        stt: WhisperSTT,
        vad: Optional[SileroVAD],
        sample_rate: int,
        segment_max_secs: int,
        silence_timeout_ms: int,
        device: Optional[int],
        wake_words: list[str],
        on_wake_word: WakeCallback,
        is_busy: BusyPredicate,
        publish: Optional[PublishCallback] = None,
        on_shutdown_phrase: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.stt = stt
        self.vad = vad
        self.sample_rate = sample_rate
        self.segment_max_secs = segment_max_secs
        self.silence_timeout_ms = silence_timeout_ms
        self.device = device
        # Pre-normalise wake words; sort longest-first so multi-word
        # phrases like "hey ultron" win over the single-word "ultron".
        self.wake_words = sorted(
            (_normalise(w) for w in wake_words if w.strip()),
            key=lambda w: -len(w.split()),
        )
        self.on_wake_word = on_wake_word
        self.is_busy = is_busy
        self.publish = publish
        self.on_shutdown_phrase = on_shutdown_phrase
        self._shutdown_norm = tuple(_normalise(p) for p in self.SHUTDOWN_PHRASES)

        self._task: Optional[asyncio.Task] = None
        self._stop = False
        chunk_ms = (CHUNK_SAMPLES * 1000) // sample_rate
        self._silence_chunks_needed = max(1, silence_timeout_ms // chunk_ms)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop(), name="wake-word-listener")
        logger.info(
            "wake-word listener started: words=%s segment_max=%ds",
            self.wake_words, self.segment_max_secs,
        )

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        loop = asyncio.get_event_loop()
        while not self._stop:
            # Yield the mic if the engine is busy (hotkey recording, playback).
            if self.is_busy():
                await asyncio.sleep(0.25)
                continue
            try:
                audio = await loop.run_in_executor(None, self._record_segment)
            except Exception as exc:
                logger.error("wake listener: record failed: %s", exc)
                await asyncio.sleep(0.5)
                continue
            if audio is None or audio.size == 0:
                continue

            try:
                result = await loop.run_in_executor(
                    None, self.stt.transcribe, audio
                )
            except Exception as exc:
                logger.error("wake listener: transcribe failed: %s", exc)
                continue

            text = (result.text or "").strip()
            if not text:
                continue

            # Check shutdown phrase first — "bye ultron" should NOT trigger
            # the normal wake path. Whisper sometimes flips word order, so
            # we look for the phrase anywhere in the transcript.
            if self._is_shutdown_phrase(text):
                logger.info("shutdown phrase detected: %r", text)
                if self.publish is not None:
                    try:
                        await self.publish("wake_listener_heard", {
                            "text": text, "matched": True, "query": "",
                            "intent": "shutdown",
                        })
                    except Exception:
                        pass
                if self.on_shutdown_phrase is not None:
                    try:
                        await self.on_shutdown_phrase()
                    except Exception as exc:
                        logger.error("on_shutdown_phrase raised: %s", exc)
                # Whatever the callback does, exit the loop — we expect
                # the process to be torn down shortly.
                return

            query = self._extract_query(text)
            matched = query is not None
            # Tell the HUD what we heard, matched or not.
            if self.publish is not None:
                try:
                    await self.publish("wake_listener_heard", {
                        "text": text,
                        "matched": matched,
                        "query": query or "",
                    })
                except Exception:
                    pass

            if not matched:
                logger.debug("wake listener: no wake word in %r", text[:60])
                continue

            logger.info(
                "wake word detected (transcript=%r, query=%r)", text, query
            )
            # Audible cue the moment we know the wake word fired. The
            # recording is already finished by this point but the user
            # has been waiting through silence_timeout + STT — the
            # rising two-note chime tells them ULTRON has them and is
            # acting. Fire-and-forget on a thread, never blocks.
            try:
                from .chime import play_listen_start
                play_listen_start(device=self.device)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.on_wake_word(query)
            except Exception as exc:
                logger.error("wake listener: on_wake_word raised: %s", exc)

    # ------------------------------------------------------------------
    # Wake-word matching
    # ------------------------------------------------------------------

    def _is_shutdown_phrase(self, transcript: str) -> bool:
        """True if the transcript contains a shutdown phrase."""
        norm = _normalise(transcript)
        for phrase in self._shutdown_norm:
            if phrase in norm:
                return True
        return False

    def _extract_query(self, transcript: str) -> Optional[str]:
        """Return the post-wake-word query, or None if no wake word."""
        norm = _normalise(transcript)
        for ww in self.wake_words:
            # Match the wake word anywhere up to ~the first 25 chars.
            idx = norm.find(ww)
            if idx == -1 or idx > 25:
                continue
            # Map back to original transcript indices roughly: count words.
            words = transcript.split()
            ww_word_count = len(ww.split())
            # Skip the first N words; whatever comes after is the query.
            remainder = " ".join(words[ww_word_count:]).strip(" ,.;:!?")
            return remainder
        return None

    # ------------------------------------------------------------------
    # Recording (mirrors AudioRecorder, but its own stream so the engine
    # recorder can claim the mic exclusively for hotkey paths).
    # ------------------------------------------------------------------

    def _record_segment(self) -> Optional[np.ndarray]:
        # Lazy import so the module is importable in tests without sounddevice.
        import sounddevice as sd  # type: ignore[import-not-found]

        chunks: list[np.ndarray] = []
        max_chunks = (self.segment_max_secs * self.sample_rate) // CHUNK_SAMPLES
        silent_run = 0
        # Without VAD, every recording is "speech" - we can't detect
        # end-of-speech, so we just cap at segment_max_secs and process
        # whatever we got. With VAD, this flips True only on detected speech.
        saw_speech = (self.vad is None)

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=CHUNK_SAMPLES,
                device=self.device,
            ) as stream:
                for _ in range(max_chunks):
                    if self._stop or self.is_busy():
                        break
                    chunk, _ = stream.read(CHUNK_SAMPLES)
                    mono = chunk[:, 0] if chunk.ndim == 2 else chunk
                    chunks.append(mono.copy())

                    if self.vad is not None:
                        try:
                            is_speech = self.vad.is_speech(mono)
                        except Exception:
                            is_speech = True
                        if is_speech:
                            saw_speech = True
                            silent_run = 0
                        else:
                            if saw_speech:
                                silent_run += 1
                                if silent_run >= self._silence_chunks_needed:
                                    break
        except Exception as exc:
            logger.error("wake listener: input stream failed: %s", exc)
            return None

        if not chunks or not saw_speech:
            return None
        audio = np.concatenate(chunks)
        boosted = amplify_to_peak(audio)
        if boosted is not audio:
            logger.debug(
                "wake listener: amplified segment (peak %.4f -> %.4f)",
                float(np.max(np.abs(audio))), float(np.max(np.abs(boosted))),
            )
        return boosted
