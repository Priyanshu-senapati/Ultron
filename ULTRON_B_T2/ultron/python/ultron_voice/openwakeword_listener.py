"""openWakeWord-based always-on wake-word listener.

Drop-in replacement for :class:`WakeWordListener` (Whisper-based). Uses
a custom-trained ONNX model (from ``train_wake_model.py``) to detect
"hey ultron" in real time on a continuous 16 kHz mic stream.

Key differences from the Whisper-based listener:

- **Latency**: ~80 ms per inference (vs. ~300 ms for Whisper base).
- **No transcript**: openWakeWord outputs a score 0–1, not text. When
  the score exceeds the threshold, we fire ``on_wake_word("")`` with an
  empty query, and the engine opens a fresh LISTENING session for the
  command (same path as wake-only hotkey).
- **Shutdown detection**: Since openWakeWord can't detect "bye ultron"
  (it only knows the trained wake phrase), shutdown detection is handled
  by the engine's Whisper-based listener running in parallel with wake
  matching disabled (shutdown-only mode).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import numpy as np

logger = logging.getLogger("ultron.voice.openwakeword")

CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz — openWakeWord's native chunk size

WakeCallback = Callable[[str], Awaitable[None]]
PublishCallback = Callable[[str, dict], Awaitable[None]]
BusyPredicate = Callable[[], bool]


class OpenWakeWordListener:
    """Always-on mic listener using a custom openWakeWord ONNX model."""

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 16000,
        device: Optional[int] = None,
        threshold: float = 0.5,
        patience: int = 3,
        cooldown_secs: float = 2.0,
        on_wake_word: Optional[WakeCallback] = None,
        is_busy: Optional[BusyPredicate] = None,
        publish: Optional[PublishCallback] = None,
    ) -> None:
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.device = device
        self.threshold = threshold
        self.patience = patience
        self.cooldown_secs = cooldown_secs
        self.on_wake_word = on_wake_word
        self.is_busy = is_busy or (lambda: False)
        self.publish = publish

        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._oww_model = None
        self._model_name: str = ""

    def _load_model(self) -> None:
        """Load the openWakeWord model. Called once at start."""
        from openwakeword.model import Model as OWWModel
        import os

        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"Wake model not found: {self.model_path}. "
                "Run train_wake_model.py first."
            )

        self._oww_model = OWWModel(
            wakeword_models=[self.model_path],
            inference_framework="onnx",
        )
        # The model name used as key in predict() results.
        # openWakeWord derives it from the filename (stem).
        self._model_name = os.path.splitext(
            os.path.basename(self.model_path)
        )[0]
        logger.info(
            "openWakeWord model loaded: %s (threshold=%.2f, patience=%d)",
            self._model_name, self.threshold, self.patience,
        )

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._load_model()
        self._stop = False
        self._task = asyncio.create_task(
            self._loop(), name="openwakeword-listener"
        )
        logger.info("openWakeWord listener started")

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        import sounddevice as sd  # type: ignore[import-not-found]

        loop = asyncio.get_event_loop()
        last_fire = 0.0
        consecutive_above = 0

        while not self._stop:
            if self.is_busy():
                await asyncio.sleep(0.25)
                consecutive_above = 0
                continue

            try:
                chunk = await loop.run_in_executor(
                    None, self._read_chunk, sd
                )
            except Exception as exc:
                logger.error("openwakeword: mic read failed: %s", exc)
                await asyncio.sleep(0.5)
                continue

            if chunk is None:
                continue

            try:
                scores = self._oww_model.predict(chunk)
            except Exception as exc:
                logger.error("openwakeword: predict failed: %s", exc)
                continue

            score = scores.get(self._model_name, 0.0)

            if score >= self.threshold:
                consecutive_above += 1
            else:
                consecutive_above = 0

            if consecutive_above >= self.patience:
                now = time.monotonic()
                if (now - last_fire) < self.cooldown_secs:
                    consecutive_above = 0
                    continue

                last_fire = now
                consecutive_above = 0
                logger.info(
                    "openWakeWord: wake detected (score=%.3f)", score
                )

                # Publish wake_word_armed (same event the Whisper listener
                # sends) so HUDs show the listening bar immediately.
                if self.publish is not None:
                    try:
                        await self.publish("wake_word_armed", {
                            "transcript": "(openWakeWord)",
                            "query": "",
                            "has_trailing_query": False,
                            "score": float(score),
                        })
                    except Exception:
                        pass

                # Chime for audible feedback.
                try:
                    from .chime import play_listen_start
                    play_listen_start(device=self.device)
                except Exception:
                    pass

                if self.on_wake_word is not None:
                    try:
                        await self.on_wake_word("")
                    except Exception as exc:
                        logger.error("on_wake_word raised: %s", exc)

                # Reset the model's internal buffers to avoid immediate
                # re-trigger on residual audio.
                if hasattr(self._oww_model, 'reset'):
                    self._oww_model.reset()

    def _read_chunk(self, sd_module) -> Optional[np.ndarray]:
        """Read one 80 ms chunk from the mic. Runs in an executor."""
        try:
            audio = sd_module.rec(
                CHUNK_SAMPLES,
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device,
                blocking=True,
            )
            return audio.flatten()
        except Exception:
            return None
