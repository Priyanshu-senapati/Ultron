"""Short audible cues so the user can tell when ULTRON is listening
or has processed their request.

Two pieces of feedback that materially refine the voice loop:

- ``play_listen_start`` — a quick rising two-note ping the moment we
  detect the wake word at the *start* of an utterance, BEFORE we
  finish transcribing. Tells the user "yes, I heard you, keep going."
- ``play_acknowledge`` — a short single tone when we have a parsed
  command but processing is still in flight (LLM round-trip). Bridges
  the dead air.

Both are synthesised in-process — no WAV file dependency, no model
load — and played on the configured output device via sounddevice.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger("ultron.voice.chime")

_SAMPLE_RATE = 24000


def _tone(freq_hz: float, duration_s: float, *,
          attack_ms: int = 6, release_ms: int = 30,
          amplitude: float = 0.25) -> np.ndarray:
    """A single tone with a soft attack + release envelope."""
    n = int(_SAMPLE_RATE * duration_s)
    t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
    wave = np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32)
    env = np.ones(n, dtype=np.float32)
    a = int(_SAMPLE_RATE * attack_ms / 1000)
    r = int(_SAMPLE_RATE * release_ms / 1000)
    if a > 0:
        env[:a] *= np.linspace(0.0, 1.0, a, dtype=np.float32)
    if r > 0 and r < n:
        env[-r:] *= np.linspace(1.0, 0.0, r, dtype=np.float32)
    return (wave * env * amplitude).astype(np.float32)


def _two_note_rising() -> np.ndarray:
    """Friendly rising chime: A5 → E6. ~110 ms total."""
    a5 = _tone(880.0, 0.045)
    silence = np.zeros(int(_SAMPLE_RATE * 0.012), dtype=np.float32)
    e6 = _tone(1318.0, 0.055)
    return np.concatenate([a5, silence, e6])


def _single_pip() -> np.ndarray:
    """Single ~60 ms tone at C6, quieter than the listen chime."""
    return _tone(1046.0, 0.060, amplitude=0.18)


# Pre-render once at import — these are tiny.
_LISTEN_AUDIO = _two_note_rising()
_ACK_AUDIO = _single_pip()


def _play_async(audio: np.ndarray, *, device: Optional[int] = None) -> None:
    """Play in a background thread so the caller never blocks."""

    def _runner() -> None:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
            sd.play(audio, samplerate=_SAMPLE_RATE, device=device, blocking=True)
            sd.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("chime playback skipped: %s", exc)

    threading.Thread(target=_runner, name="ultron-chime", daemon=True).start()


def play_listen_start(*, device: Optional[int] = None) -> None:
    """Wake word detected — ULTRON is recording / processing your command."""
    _play_async(_LISTEN_AUDIO, device=device)


def play_acknowledge(*, device: Optional[int] = None) -> None:
    """Command parsed — ULTRON is working on it."""
    _play_async(_ACK_AUDIO, device=device)
