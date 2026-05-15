"""Voice activity detection — Silero VAD.

Used **only** for end-of-speech detection while recording. We are not
running always-on VAD (that's a Phase-2 feature when wake words land).

Why Silero?
-----------

- Small (1.5 MB), runs fast on CPU, no GPU dependency.
- Better at distinguishing speech from background hum / typing than
  energy-based VAD.
- Used by faster-whisper too — keeping the same model upstream and
  downstream means the recorder won't accidentally feed silence to
  Whisper that Whisper's internal VAD would just drop.

Hardware
--------

CPU only — we don't want to fight Whisper for GPU memory. The model is
small enough that inference cost is in the microseconds.

Chunk size
----------

Silero expects exactly **512 samples at 16kHz** (= 32ms windows). The
AudioRecorder is configured to feed chunks of this size — don't change
``sample_rate`` independently of the recorder.

Graceful degradation
--------------------

If ``torch.hub.load`` fails (no internet on first run, torch missing,
proxy issues), :meth:`load` raises. The orchestrator catches and falls
back to fixed-duration recording without VAD — see the docstring in
``audio_io.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger("ultron.voice.vad")

# Silero is hard-pinned to 512 samples per inference at 16kHz. Don't
# change this number without updating the recorder's chunk size to match.
CHUNK_SAMPLES: int = 512


class SileroVAD:
    """Tiny torch-hub-based VAD.

    Single model instance shared across the lifetime of the engine. The
    forward pass is stateful in the upstream model but we treat each
    chunk independently — for end-of-speech detection that's accurate
    enough and means we don't need to thread state through the recorder.
    """

    def __init__(self, threshold: float, sample_rate: int) -> None:
        if sample_rate != 16000:
            # Silero ships variants for 8kHz too, but we never use them.
            # Fail loudly so misconfiguration is obvious.
            raise ValueError(
                f"SileroVAD only supports 16kHz; got {sample_rate}"
            )
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._model = None  # lazy
        self._torch = None  # lazy

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the model from torch hub.

        Caches on disk under ``~/.cache/torch/hub/`` after first run.
        Subsequent loads are local (< 100 ms).
        """
        # Lazy imports keep this module importable without torch
        # available (e.g. when running unit tests that mock the VAD).
        import torch  # type: ignore[import-not-found]

        self._torch = torch
        try:
            model, _utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"silero-vad failed to load via torch.hub: {exc}. "
                "The engine will continue without VAD; recordings will use "
                "fixed-duration mode."
            ) from exc
        self._model = model
        logger.info("silero-vad loaded")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """Return True if ``audio_chunk`` contains speech.

        Chunk MUST be 512 float32 samples at 16kHz. Caller is responsible
        for slicing — we don't pad or truncate to keep the contract sharp
        (mismatched chunk sizes give garbage scores).
        """
        if self._model is None or self._torch is None:
            raise RuntimeError("SileroVAD.is_speech called before load()")
        if audio_chunk.shape[0] != CHUNK_SAMPLES:
            raise ValueError(
                f"silero-vad expects {CHUNK_SAMPLES} samples, got "
                f"{audio_chunk.shape[0]}"
            )
        torch = self._torch
        # Convert numpy → torch float32 tensor on CPU. Avoids surprises
        # if the caller hands us int16 by accident.
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)
        # Silero expects values in [-1, 1] — if the caller forgot to
        # normalise int16 → float, the inference still runs but gives
        # high-confidence false positives. Cheap safety clamp.
        if audio_chunk.max(initial=0.0) > 1.5 or audio_chunk.min(initial=0.0) < -1.5:
            audio_chunk = audio_chunk / 32768.0

        tensor = torch.from_numpy(audio_chunk)
        try:
            with torch.no_grad():
                score = float(self._model(tensor, self.sample_rate).item())
        except Exception as exc:
            # One bad chunk shouldn't take down the recorder.
            logger.warning("silero forward pass failed: %s — assuming speech", exc)
            return True
        return score >= self.threshold
