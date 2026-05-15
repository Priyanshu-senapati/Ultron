"""Audio I/O — recording from the microphone and playing back TTS.

Both classes wrap `sounddevice` (PortAudio bindings). On Windows that
means WASAPI; on macOS Core Audio; on Linux ALSA/PulseAudio. The
voice engine doesn't care which backend — sounddevice handles it.

Recorder
--------

``AudioRecorder.record_utterance()`` is the workhorse:

1. Open an input stream at 16kHz mono.
2. Pull 512-sample chunks (32ms windows) until either:
   - VAD reports ``silence_timeout_ms`` worth of consecutive non-speech
     chunks (= end-of-utterance), OR
   - ``max_record_secs`` elapsed (safety cap), OR
   - The caller's task is cancelled (barge-in / shutdown)
3. Concatenate all chunks (including the trailing silence — Whisper
   filters internally and gets confused by overly-trimmed clips) and
   return as a float32 numpy array.

If the VAD failed to load (silero couldn't reach torch hub), the
recorder falls back to fixed-duration recording of ``max_record_secs``.
The result is still usable by Whisper but the user has to wait the
full cap before transcription starts.

Player
------

``AudioPlayer.play()`` accepts two formats:

- ``fmt="pcm"`` — raw 16-bit signed PCM at the synth rate (Piper produces
  this at 22050Hz mono).
- ``fmt="wav"`` — full WAV file bytes (Edge-TTS). We parse the WAV header
  to get the actual sample rate, then play.

Playback is awaitable but blocking-by-design — the orchestrator awaits
``play()`` before transitioning out of ``SPEAKING``. Barge-in works by
calling ``player.stop()`` from a separate task, which aborts the
sounddevice stream from underneath the play loop.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Optional

import numpy as np

from .vad import CHUNK_SAMPLES, SileroVAD

logger = logging.getLogger("ultron.voice.audio")


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #


class AudioRecorder:
    """Microphone capture with VAD-driven end-of-speech detection."""

    def __init__(
        self,
        vad: Optional[SileroVAD],
        sample_rate: int,
        max_secs: int,
        silence_timeout_ms: int,
        device: Optional[int] = None,
    ) -> None:
        self.vad = vad
        self.sample_rate = sample_rate
        self.max_secs = max_secs
        self.silence_timeout_ms = silence_timeout_ms
        self.device = device
        # Pre-compute how many consecutive silent chunks count as
        # end-of-speech. 1500ms / 32ms = ~47 chunks at the default.
        chunk_ms = (CHUNK_SAMPLES * 1000) // sample_rate
        self._silence_chunks_needed = max(1, silence_timeout_ms // chunk_ms)

    async def record_utterance(self) -> np.ndarray:
        """Record until VAD says we're done (or the safety cap fires).

        Returns a float32 mono numpy array at ``self.sample_rate``. Empty
        array if recording was cancelled before any audio arrived.

        Runs the synchronous sounddevice loop in an executor so the
        asyncio loop stays responsive (the orchestrator may want to
        publish ``voice_state_changed`` while we're recording).
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._record_sync)

    # ------------------------------------------------------------------
    # Synchronous worker (runs in executor)
    # ------------------------------------------------------------------

    def _record_sync(self) -> np.ndarray:
        # Lazy import — keeps the module importable without sounddevice
        # installed (Linux CI / tests with mocks).
        import sounddevice as sd  # type: ignore[import-not-found]

        chunks: list[np.ndarray] = []
        max_chunks = (self.max_secs * self.sample_rate) // CHUNK_SAMPLES
        silent_run = 0
        # We require at least one *speech* chunk before we start counting
        # silence — otherwise opening the mic during a quiet room
        # immediately ends the recording.
        saw_speech = False

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=CHUNK_SAMPLES,
                device=self.device,
            ) as stream:
                logger.info(
                    "recording started: rate=%d chunk=%d max_secs=%d device=%s",
                    self.sample_rate,
                    CHUNK_SAMPLES,
                    self.max_secs,
                    self.device,
                )
                for _ in range(max_chunks):
                    chunk, overflowed = stream.read(CHUNK_SAMPLES)
                    if overflowed:
                        logger.warning("audio input overflow — chunk dropped")
                    # sd.InputStream returns shape (frames, channels);
                    # squeeze to 1D for downstream consumers.
                    mono = chunk[:, 0] if chunk.ndim == 2 else chunk
                    chunks.append(mono.copy())

                    if self.vad is not None:
                        try:
                            is_speech = self.vad.is_speech(mono)
                        except Exception as exc:
                            # Treat VAD failure as "always speech" — we
                            # never want to truncate a real utterance
                            # because the VAD glitched.
                            logger.debug("vad failed mid-recording: %s", exc)
                            is_speech = True

                        if is_speech:
                            saw_speech = True
                            silent_run = 0
                        else:
                            if saw_speech:
                                silent_run += 1
                                if silent_run >= self._silence_chunks_needed:
                                    logger.info(
                                        "end-of-speech detected after %d silent chunks",
                                        silent_run,
                                    )
                                    break
                    # VAD-less path: record full max_secs.
        except Exception as exc:
            logger.error("recorder failed: %s", exc)
            return np.array([], dtype=np.float32)

        if not chunks:
            return np.array([], dtype=np.float32)
        return np.concatenate(chunks)


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #


class AudioPlayer:
    """TTS audio playback with stop-for-barge-in support."""

    # Piper's PCM output is at this rate. Edge-TTS WAVs may differ; we
    # read the header to find out.
    PIPER_RATE: int = 22050

    def __init__(self, sample_rate: int = 22050, device: Optional[int] = None) -> None:
        # `sample_rate` is the default for PCM. WAV bytes override.
        self.sample_rate = sample_rate
        self.device = device
        self._stop = False

    async def play(self, audio_bytes: bytes, fmt: str = "pcm") -> None:
        """Play ``audio_bytes`` and return when playback completes.

        ``fmt`` can be ``"pcm"`` (raw 16-bit signed at ``self.sample_rate``)
        or ``"wav"`` (full RIFF/WAVE file — header parsed to find rate).
        Empty bytes return immediately. Errors are logged, never raised
        — playback failure shouldn't crash the voice loop.
        """
        if not audio_bytes:
            return
        # Reset the stop flag for this playback.
        self._stop = False
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._play_sync, audio_bytes, fmt)

    def stop(self) -> None:
        """Request playback to stop ASAP.

        The actual stream tear-down happens inside the executor — this
        just flips a flag the play loop checks between chunks.
        """
        self._stop = True

    # ------------------------------------------------------------------
    # Synchronous worker
    # ------------------------------------------------------------------

    def _play_sync(self, audio_bytes: bytes, fmt: str) -> None:
        import sounddevice as sd  # type: ignore[import-not-found]

        try:
            if fmt == "wav":
                samples, rate = self._decode_wav(audio_bytes)
            elif fmt == "pcm":
                samples = self._decode_pcm(audio_bytes)
                rate = self.sample_rate
            else:
                logger.error("unknown audio format: %r", fmt)
                return
        except Exception as exc:
            logger.error("decode failed (%s): %s", fmt, exc)
            return

        if samples.size == 0:
            return

        # Stream in small chunks so `stop()` has a quick effect.
        chunk = max(rate // 20, 256)  # ~50ms slices
        try:
            with sd.OutputStream(
                samplerate=rate,
                channels=1,
                dtype="float32",
                device=self.device,
            ) as stream:
                idx = 0
                while idx < samples.size and not self._stop:
                    stream.write(samples[idx : idx + chunk])
                    idx += chunk
        except Exception as exc:
            logger.error("playback failed: %s", exc)

    # ------------------------------------------------------------------
    # Decoders (small enough to keep inline)
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_pcm(audio_bytes: bytes) -> np.ndarray:
        """16-bit signed PCM → float32 in [-1, 1]."""
        ints = np.frombuffer(audio_bytes, dtype=np.int16)
        return ints.astype(np.float32) / 32768.0

    @staticmethod
    def _decode_wav(audio_bytes: bytes) -> tuple[np.ndarray, int]:
        """WAV file → (float32 samples, sample_rate). Downmixes to mono."""
        buf = io.BytesIO(audio_bytes)
        with wave.open(buf, "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
        # We support 16-bit only — anything else is a sign the WAV
        # source has changed. Log and bail loudly.
        if width != 2:
            raise ValueError(f"unsupported WAV bit depth: {width * 8}-bit")
        ints = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            ints = ints.reshape(-1, channels).mean(axis=1).astype(np.int16)
        return ints.astype(np.float32) / 32768.0, rate
