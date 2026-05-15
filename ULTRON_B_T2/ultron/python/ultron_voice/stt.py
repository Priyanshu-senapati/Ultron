"""Speech-to-text via faster-whisper.

Two responsibilities:

1. **Model lifecycle.** Loading `large-v3-turbo` onto an RTX 5070 Ti at
   `int8` quantisation takes ~5s. We do it once at startup, hold the model
   in memory, reuse it for every transcription. If CUDA goes OOM during
   load, we retry on CPU once and log a warning — the engine continues
   to run, just slower.

2. **Per-utterance inference.** Synchronous (faster-whisper has no async
   API). Callers MUST invoke `transcribe()` via
   `loop.run_in_executor(None, stt.transcribe, audio)` to avoid blocking
   the asyncio event loop.

Quality filter
--------------

faster-whisper returns *something* for almost any audio — including
breath sounds, room tone, button clicks. We treat results as empty when:

- The text is whitespace-only
- The text is a single token AND mean segment probability is below
  ``CONFIDENCE_FLOOR``

This keeps the voice pipeline from publishing accidental `voice_transcript`
events that fire C unnecessarily.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _register_pip_cuda_dlls() -> None:
    """Make nvidia-*-cu12 pip wheels findable at runtime on Windows.

    pip installs DLLs into site-packages/nvidia/<pkg>/bin/ but never adds
    those paths to the Windows DLL search list. ctranslate2 then fails
    with 'Library cublas64_12.dll is not found' even though the file
    physically exists.

    We register paths two ways:
    1. ``os.add_dll_directory`` for Python's own dlopen calls.
    2. Prepend to ``PATH`` for native loaders (ctranslate2's LoadLibraryEx
       falls back to the env, not the Python search list).
    """
    if sys.platform != "win32":
        return
    site_packages = Path(sys.executable).parent.parent / "Lib" / "site-packages"
    extra_paths: list[str] = []
    for sub in ("cublas/bin", "cudnn/bin", "cuda_runtime/bin",
                "cuda_nvrtc/bin", "cusparse/bin", "cufft/bin"):
        bin_dir = site_packages / "nvidia" / sub
        if bin_dir.is_dir():
            extra_paths.append(str(bin_dir))
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass
    if extra_paths:
        os.environ["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + os.environ.get("PATH", "")


_register_pip_cuda_dlls()

import numpy as np

logger = logging.getLogger("ultron.voice.stt")

# Below this mean-segment confidence, single-token transcripts are
# discarded. Calibrated for faster-whisper's typical scores on this model.
CONFIDENCE_FLOOR: float = 0.60


@dataclass
class TranscriptResult:
    """One transcription outcome.

    `text` is the empty string for discarded results — callers should
    treat `bool(result.text)` as the "should I act on this?" gate.
    """

    text: str
    confidence: float  # mean segment probability in [0.0, 1.0]
    duration_secs: float
    language: str


class WhisperSTT:
    """faster-whisper wrapper.

    Construct → call ``load()`` once → call ``transcribe()`` per utterance.
    Thread-safe for concurrent ``transcribe()`` calls in the sense that
    faster-whisper itself is thread-safe, but we have no use case for it —
    the orchestrator serialises requests one at a time.
    """

    def __init__(
        self,
        model: str,
        device: str,
        compute_type: str,
        language: Optional[str],
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        # `language` is Optional in the dataclass; faster-whisper accepts
        # `None` for auto-detect. We store as-is.
        self.language = language
        self._model = None  # set by load()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load model into memory. Blocks ~5s on RTX 5070 Ti at int8.

        On CUDA OOM, falls back to CPU once and logs a warning. Further
        failures bubble up — the engine cannot proceed without STT.
        """
        # Lazy import so unit tests can mock without a real GPU.
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
            logger.info(
                "whisper loaded: model=%s device=%s compute_type=%s",
                self.model_name,
                self.device,
                self.compute_type,
            )
        except Exception as exc:
            # Heuristic: anything mentioning OOM / CUDA / memory → retry
            # on CPU. We don't rely on a specific exception type because
            # faster-whisper surfaces a mix of RuntimeError, ValueError,
            # and CT2 native errors depending on the failure mode.
            msg = str(exc).lower()
            cuda_failure = (
                self.device == "cuda"
                and ("cuda" in msg or "out of memory" in msg or "cublas" in msg)
            )
            if cuda_failure:
                logger.warning(
                    "whisper CUDA load failed (%s); falling back to CPU. "
                    "TTS latency will be noticeably higher.",
                    exc,
                )
                self.device = "cpu"
                # CPU prefers int8 too — keeps the API consistent.
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                )
                logger.info("whisper loaded on CPU fallback")
            else:
                raise

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def transcribe(
        self,
        audio_np: np.ndarray,
        sample_rate: int = 16000,
    ) -> TranscriptResult:
        """Run one transcription. Synchronous; call via executor.

        Parameters
        ----------
        audio_np
            float32 mono numpy array. If passed int16, it's normalised to
            [-1, 1] before inference.
        sample_rate
            Must be 16000 — whisper resamples otherwise but it adds latency
            we'd rather not pay. The recorder is pinned to 16kHz too.

        Returns
        -------
        ``TranscriptResult``. ``result.text == ""`` means the audio was
        unusable (silent, too noisy, or filtered by the confidence gate).
        """
        if self._model is None:
            raise RuntimeError("WhisperSTT.transcribe called before load()")
        if audio_np.size == 0:
            return TranscriptResult(text="", confidence=0.0, duration_secs=0.0, language="")
        # Normalise int16 → float32 if needed. faster-whisper accepts
        # either, but explicit conversion keeps downstream code simple.
        if audio_np.dtype != np.float32:
            audio_np = audio_np.astype(np.float32) / 32768.0

        duration_secs = float(audio_np.shape[0]) / float(sample_rate)
        if duration_secs < 0.2:
            # Sub-200ms clips are almost always button clicks or breaths.
            return TranscriptResult(text="", confidence=0.0, duration_secs=duration_secs, language="")

        t0 = time.perf_counter()
        segments_iter, info = self._model.transcribe(
            audio_np,
            language=self.language,
            vad_filter=True,
            # `vad_parameters` defaults are fine for our recorder which
            # already trims most silence via SileroVAD.
        )
        # faster-whisper yields segments lazily; collect them all so we
        # can compute mean probability.
        segments = list(segments_iter)
        elapsed = time.perf_counter() - t0

        if not segments:
            return TranscriptResult(
                text="",
                confidence=0.0,
                duration_secs=duration_secs,
                language=info.language or "",
            )

        text = "".join(seg.text for seg in segments).strip()
        # `avg_logprob` is in log-space; convert to probability in [0,1].
        # Some segments may report nan/-inf for very short clips — guard.
        probs: list[float] = []
        for seg in segments:
            lp = getattr(seg, "avg_logprob", None)
            if lp is None:
                continue
            try:
                p = float(np.exp(lp))
            except (OverflowError, ValueError):
                continue
            if 0.0 <= p <= 1.0:
                probs.append(p)
        confidence = float(np.mean(probs)) if probs else 0.0

        # Confidence floor: discard single-token noise.
        if text and confidence < CONFIDENCE_FLOOR and len(text.split()) <= 1:
            logger.debug(
                "discarding low-confidence single-token result: '%s' conf=%.2f",
                text,
                confidence,
            )
            text = ""

        logger.info(
            "transcribe: text=%r conf=%.2f duration=%.2fs infer=%.2fs",
            text[:60],
            confidence,
            duration_secs,
            elapsed,
        )
        return TranscriptResult(
            text=text,
            confidence=confidence,
            duration_secs=duration_secs,
            language=info.language or "",
        )
