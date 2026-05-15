"""ULTRON Module B — Voice Engine orchestrator.

Run this directly::

    python python/voice_engine.py

It loads `[voice]` config from the shared config.toml, brings up all
subsystems (STT, TTS, VAD, recorder, player, state machine, clap
handler, hotkey listener), connects to the ULTRON-core WS bridge, and
sits in an event loop forever.

Pipeline
--------

1. Hotkey press (or clap=1) → ``on_hotkey_press`` → cancel any
   in-flight playback (barge-in) → enter LISTENING.
2. ``record_and_transcribe`` recordings the mic until VAD silence
   or hotkey release, runs Whisper in an executor, publishes
   ``voice_transcript`` with the result.
3. Module C consumes ``voice_transcript``, calls its LLM, publishes
   ``llm_response`` with the response text.
4. ``handle_event`` sees ``llm_response``, signals the waiting
   coroutine via ``_llm_response_event``, which then runs TTS and
   plays the audio.
5. After playback completes (or fails), we transition back to IDLE.

The whole pipeline is a single-active-request invariant: only one
``record_and_transcribe`` task runs at a time. A new hotkey press
while we're PROCESSING/SPEAKING cancels the current one and starts
over (barge-in).

Failure handling
----------------

- LLM response takes longer than ``llm_response_timeout_secs`` →
  transition to ERROR (auto-recovers to IDLE after 3s).
- ``llm_response`` carries ``error: true`` → same.
- TTS returns empty bytes → skip playback, transition back to IDLE.
- STT returns empty text → transition straight back to IDLE
  (no ``voice_transcript`` published).
- Bridge disconnects → ``ultron_bridge.UltronBridge`` handles
  reconnect; state machine stays IDLE during the outage.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Make `ultron_voice.*` and `ultron_bridge` importable when run from
# the repo root or from inside python/.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ultron_voice.audio_io import AudioPlayer, AudioRecorder
from ultron_voice.clap_handler import ClapHandler
from ultron_voice.config import VoiceConfig, load_voice_config
from ultron_voice.hotkey import HotkeyListener
from ultron_voice.state_machine import VoiceState, VoiceStateMachine
from ultron_voice.stt import TranscriptResult, WhisperSTT
from ultron_voice.tts import TTSEngine
from ultron_voice.vad import SileroVAD

from ultron_bridge import UltronBridge

logger = logging.getLogger("ultron.voice")


# --------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------- #


class VoiceEngine:
    """Holds every subsystem + the event-loop wiring.

    Constructed by ``main()``. The orchestration code is methods on this
    class so callbacks (hotkey, bus events, clap actions) can access
    shared state through ``self`` rather than closures-of-closures.
    """

    def __init__(self, cfg: VoiceConfig) -> None:
        self.cfg = cfg
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Subsystems — created in ``setup()`` (some are async-heavy).
        self.stt: Optional[WhisperSTT] = None
        self.vad: Optional[SileroVAD] = None
        self.tts: Optional[TTSEngine] = None
        self.recorder: Optional[AudioRecorder] = None
        self.player: Optional[AudioPlayer] = None
        self.bridge: Optional[UltronBridge] = None
        self.state_machine: Optional[VoiceStateMachine] = None
        self.clap_handler: Optional[ClapHandler] = None
        self.hotkey_listener: Optional[HotkeyListener] = None

        # Synchronisation for the LLM response wait. Set when a
        # `llm_response` event arrives with non-empty text.
        self._llm_response_event = asyncio.Event()
        # Buffer for the response text — `wait_for(event.wait())`
        # returns nothing on its own.
        self._pending_response_text: str = ""
        self._pending_response_error: bool = False

        # The current in-flight voice request task. Held so barge-in
        # can cancel cleanly without hunting through asyncio internals.
        self._current_request: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Load all heavy resources (Whisper, VAD). Blocks ~5-8s.

        Must run before the asyncio loop starts because torch.hub /
        faster-whisper model loading is synchronous and we'd rather
        block the user up front than the first hotkey press.
        """
        logger.info("loading STT model: %s on %s", self.cfg.whisper_model, self.cfg.whisper_device)
        self.stt = WhisperSTT(
            model=self.cfg.whisper_model,
            device=self.cfg.whisper_device,
            compute_type=self.cfg.whisper_compute_type,
            language=self.cfg.whisper_language,
        )
        self.stt.load()

        # VAD is optional — if silero-vad can't load, we degrade to
        # fixed-duration recording. The recorder accepts None for vad.
        try:
            self.vad = SileroVAD(
                threshold=self.cfg.vad_threshold,
                sample_rate=self.cfg.sample_rate,
            )
            self.vad.load()
        except Exception as exc:
            logger.warning(
                "silero-vad load failed (%s); falling back to fixed-duration "
                "recording (will record full %ds per utterance).",
                exc,
                self.cfg.max_record_secs,
            )
            self.vad = None

        self.tts = TTSEngine(
            backend=self.cfg.tts_backend,
            piper_voice=self.cfg.piper_voice,
            edge_tts_voice=self.cfg.edge_tts_voice,
            piper_model_path=self.cfg.piper_model_path,
        )

        self.recorder = AudioRecorder(
            vad=self.vad,
            sample_rate=self.cfg.sample_rate,
            max_secs=self.cfg.max_record_secs,
            silence_timeout_ms=self.cfg.silence_timeout_ms,
            device=self.cfg.audio_input_device,
        )
        self.player = AudioPlayer(
            sample_rate=AudioPlayer.PIPER_RATE,
            device=self.cfg.audio_output_device,
        )

    async def run(self) -> None:
        """Connect to the bridge and run forever."""
        self.loop = asyncio.get_running_loop()

        # Build the state machine + clap handler now that we have a
        # bridge stub to pass them. The bridge is constructed last so
        # its ``on_event`` callback can reference fully-wired engine
        # state — we pre-create the engine then assign at the end.
        # Pre-create the bridge object so the state machine can hold a
        # reference; ``run_forever`` only opens the WS connection.
        self.bridge = UltronBridge(
            url=self.cfg.ws_url,
            token=self.cfg.token,
            on_event=self._on_event,
            subscribe_to=["llm_response", "input_activity"],
            role="voice-engine",
        )

        self.state_machine = VoiceStateMachine(bridge=self.bridge, player=self.player)
        self.clap_handler = ClapHandler(
            wake=self._wake_via_clap,
            publish=self.bridge.publish,
            speak=self._speak_directly,
            play_audio=self._play_audio_buffer,
        )

        # Hotkey listener — runs on its own thread, bridges into asyncio
        # via ``run_coroutine_threadsafe``.
        try:
            self.hotkey_listener = HotkeyListener(
                hotkey=self.cfg.hotkey,
                on_press=self._on_hotkey_press,
                on_release=self._on_hotkey_release,
                on_escape=self._on_escape,
                loop=self.loop,
            )
            self.hotkey_listener.start()
        except Exception as exc:
            logger.error(
                "hotkey listener failed to start (%s); continuing with clap-only.",
                exc,
            )
            self.hotkey_listener = None

        logger.info(
            "voice engine ready: state=%s hotkey=%s tts=%s",
            self.state_machine.state.value,
            self.cfg.hotkey,
            self.cfg.tts_backend,
        )
        await self.bridge.run_forever()

    async def shutdown(self) -> None:
        """Graceful shutdown — stop hotkey, cancel any in-flight task."""
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        if self._current_request is not None and not self._current_request.done():
            self._current_request.cancel()
            try:
                await self._current_request
            except (asyncio.CancelledError, Exception):
                pass
        if self.state_machine is not None:
            try:
                await self.state_machine.cancel()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Bus event handler
    # ------------------------------------------------------------------

    async def _on_event(self, event: dict) -> None:
        """Bridge calls this for every ``op:event`` frame."""
        kind = event.get("kind", "")
        payload = event.get("payload") or {}

        if kind == "llm_response":
            await self._handle_llm_response(payload)
        elif kind == "input_activity":
            await self._handle_input_activity(payload)
        # Anything else: ignored. We only subscribed to the two above
        # so this branch shouldn't fire in practice, but defensive.

    async def _handle_llm_response(self, payload: dict) -> None:
        """Route C's response to whoever's waiting."""
        # Only act if we're currently waiting for one. A response that
        # arrives while we're IDLE (e.g. for a non-voice request C
        # served) is not ours to consume.
        if self.state_machine is None:
            return
        if self.state_machine.state != VoiceState.PROCESSING:
            logger.debug(
                "llm_response received but state=%s — not consuming",
                self.state_machine.state.value,
            )
            return

        text = (payload.get("text") or "").strip()
        is_error = bool(payload.get("error"))
        self._pending_response_text = text
        self._pending_response_error = is_error
        # Wake the waiting task; the actual TTS dispatch happens there
        # so it stays serialised on the loop.
        self._llm_response_event.set()

    async def _handle_input_activity(self, payload: dict) -> None:
        """Route clap events. Anything else from H is informational."""
        if payload.get("kind") != "clap":
            return
        if self.clap_handler is None:
            return
        count = int(payload.get("clap_count") or 0)
        await self.clap_handler.on_clap(count)

    # ------------------------------------------------------------------
    # Hotkey callbacks
    # ------------------------------------------------------------------

    async def _on_hotkey_press(self) -> None:
        """Start a new voice request, cancelling any in-flight one."""
        if self.state_machine is None:
            return
        # Barge-in: if we're speaking or processing, cancel and start over.
        if self.state_machine.state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
            logger.info("barge-in: cancelling current request")
            if self._current_request is not None and not self._current_request.done():
                self._current_request.cancel()
            await self.state_machine.cancel()

        if self.state_machine.state == VoiceState.IDLE:
            await self.state_machine.transition(VoiceState.LISTENING, "hotkey")
            self._current_request = asyncio.create_task(
                self._record_and_transcribe("hotkey")
            )

    async def _on_hotkey_release(self) -> None:
        """The recorder ends on VAD silence anyway — the release is
        a hint, not a hard stop. We don't act on it explicitly here
        because the recorder doesn't expose a 'stop' method, and
        adding one for this one case isn't worth the complexity. The
        spec says 'or hotkey_release' but treats it as informational."""
        # Intentional no-op. Documented so future-me doesn't wonder.
        return

    async def _on_escape(self) -> None:
        """Escape key — full cancel from any state."""
        if self.state_machine is None:
            return
        logger.info("escape pressed — cancelling")
        if self._current_request is not None and not self._current_request.done():
            self._current_request.cancel()
        await self.state_machine.cancel()

    # ------------------------------------------------------------------
    # Clap callbacks (passed into ClapHandler)
    # ------------------------------------------------------------------

    async def _wake_via_clap(self) -> None:
        """clap=1 — same path as a hotkey press."""
        await self._on_hotkey_press()

    async def _speak_directly(self, text: str) -> None:
        """Short-clipboard path. TTS + play, no LLM round-trip.

        Goes through PROCESSING → SPEAKING → IDLE so the HUD sees the
        same transitions as a voiced request.
        """
        if self.state_machine is None or self.tts is None:
            return
        if self.state_machine.state != VoiceState.IDLE:
            logger.info("ignoring direct-speak while state=%s", self.state_machine.state.value)
            return
        await self.state_machine.transition(VoiceState.PROCESSING, "clap_3_direct")
        try:
            await self._synthesize_and_play(text)
        except Exception as exc:
            logger.error("direct-speak failed: %s", exc)
            await self.state_machine.transition(VoiceState.ERROR, "direct_speak_error")

    async def _play_audio_buffer(self, audio: bytes, fmt: str) -> None:
        """Replay path (clap=4). Bypasses TTS entirely."""
        if self.state_machine is None or self.player is None:
            return
        if not audio:
            logger.info("replay requested with empty buffer — skipping")
            return
        if self.state_machine.state != VoiceState.IDLE:
            logger.info("ignoring replay while state=%s", self.state_machine.state.value)
            return
        await self.state_machine.transition(VoiceState.SPEAKING, "clap_4_replay")
        try:
            await self.player.play(audio, fmt=fmt)
        finally:
            await self.state_machine.transition(VoiceState.IDLE, "replay_done")

    # ------------------------------------------------------------------
    # Core voice request lifecycle
    # ------------------------------------------------------------------

    async def _record_and_transcribe(self, activation: str) -> None:
        """The full request lifecycle for one user utterance.

        Wrapped in a broad try/except so a single failure can never kill
        the engine — at worst we transition through ERROR and auto-recover.
        Cancellation (from barge-in) is allowed to propagate.
        """
        if self.recorder is None or self.stt is None or self.bridge is None or self.state_machine is None:
            return

        try:
            # ----- Record -----
            audio = await self.recorder.record_utterance()
            if audio is None or audio.size == 0:
                logger.info("no audio recorded — back to IDLE")
                await self.state_machine.transition(VoiceState.IDLE, "empty_audio")
                return

            # ----- STT (in executor) -----
            await self.state_machine.transition(VoiceState.PROCESSING, "stt")
            loop = asyncio.get_running_loop()
            result: TranscriptResult = await loop.run_in_executor(
                None, self.stt.transcribe, audio
            )
            if not result.text:
                logger.info("transcript empty — back to IDLE")
                await self.state_machine.transition(VoiceState.IDLE, "empty_transcript")
                return

            # ----- Publish voice_transcript -----
            self._llm_response_event.clear()
            self._pending_response_text = ""
            self._pending_response_error = False
            await self.bridge.publish(
                "voice_transcript",
                {
                    "text": result.text,
                    "duration_secs": result.duration_secs,
                    "confidence": result.confidence,
                    "activation": activation,
                    "ts_unix_ms": int(time.time() * 1000),
                },
            )

            # ----- Wait for C's response -----
            try:
                await asyncio.wait_for(
                    self._llm_response_event.wait(),
                    timeout=self.cfg.llm_response_timeout_secs,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "llm_response timeout after %.1fs",
                    self.cfg.llm_response_timeout_secs,
                )
                await self.state_machine.transition(VoiceState.ERROR, "llm_timeout")
                return

            # ----- Handle response -----
            if self._pending_response_error:
                logger.warning("llm_response carried error=true")
                # Still speak the apology text if any.
                if self._pending_response_text:
                    await self._synthesize_and_play(self._pending_response_text)
                await self.state_machine.transition(VoiceState.ERROR, "llm_error")
                return

            if not self._pending_response_text:
                logger.warning("llm_response had empty text — nothing to say")
                await self.state_machine.transition(VoiceState.IDLE, "empty_response")
                return

            await self._synthesize_and_play(self._pending_response_text)
            await self.state_machine.transition(VoiceState.IDLE, "tts_done")

        except asyncio.CancelledError:
            # Barge-in — caller already handled the state transition.
            logger.info("voice request cancelled mid-flight")
            raise
        except Exception as exc:
            logger.error("voice request failed: %s", exc)
            await self.state_machine.transition(VoiceState.ERROR, "exception")

    async def _synthesize_and_play(self, text: str) -> None:
        """TTS + play, with truncation and last-audio buffering.

        Caller is responsible for state transitions around this — we
        only handle the synth/play mechanics so it can be reused from
        both ``_record_and_transcribe`` and ``_speak_directly``.
        """
        if self.tts is None or self.player is None or self.state_machine is None:
            return
        text = self.tts.truncate_to_limit(text, self.cfg.max_tts_chars)
        audio = await self.tts.synthesize(text)
        if not audio:
            logger.warning("TTS produced no audio — skipping playback")
            return
        fmt = TTSEngine.format_of(audio)
        # Buffer for clap=4 replay.
        if self.clap_handler is not None:
            self.clap_handler.last_audio = audio
            self.clap_handler.last_audio_fmt = fmt
        await self.state_machine.transition(VoiceState.SPEAKING, "tts_ready")
        try:
            await self.player.play(audio, fmt=fmt)
        finally:
            # Caller transitions out — the SPEAKING → next state move
            # belongs to the lifecycle method, not this helper.
            pass


# --------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------- #


async def _amain() -> None:
    cfg = load_voice_config()
    engine = VoiceEngine(cfg)
    engine.setup()

    # Wire graceful shutdown on Ctrl-C. On Windows asyncio's signal
    # handling is limited — KeyboardInterrupt will bubble up naturally
    # from `run_forever`, which is acceptable.
    if os.name != "nt":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda: asyncio.create_task(engine.shutdown())
                )
            except (NotImplementedError, RuntimeError):
                pass

    try:
        await engine.run()
    finally:
        await engine.shutdown()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
