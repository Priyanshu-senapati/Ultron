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
from ultron_voice.wake_word import WakeWordListener
from ultron_voice.openwakeword_listener import OpenWakeWordListener

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
        self.wake_word_listener: Optional[WakeWordListener] = None
        self.oww_listener: Optional[OpenWakeWordListener] = None

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

        # Monotonic timestamp of when SPEAKING last ended. Used to keep
        # `is_busy()` True for `cfg.post_speak_cooldown_secs` after audio
        # playback finishes — gives speaker leakage time to die and stops
        # the wake listener from picking up its own reply as a new wake.
        self._last_speak_end_ts: float = 0.0

        # Guard against double-firing: serialise on_wake_word so only
        # one can run at a time, and ignore calls within 2s of the last.
        self._last_wake_ts: float = 0.0
        self._wake_lock = asyncio.Lock()

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
            kokoro_model_path=self.cfg.kokoro_model_path,
            kokoro_voices_path=self.cfg.kokoro_voices_path,
            kokoro_voice=self.cfg.kokoro_voice,
            kokoro_speed=self.cfg.kokoro_speed,
            kokoro_lang=self.cfg.kokoro_lang,
        )

        self.recorder = AudioRecorder(
            vad=self.vad,
            sample_rate=self.cfg.sample_rate,
            max_secs=self.cfg.max_record_secs,
            silence_timeout_ms=self.cfg.silence_timeout_ms,
            device=self.cfg.audio_input_device,
            level_callback=lambda peak: self._publish_mic_level(peak, "hotkey"),
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
            subscribe_to=["llm_response", "input_activity", "flow_state_changed",
                          "reentry_brief"],
            role="voice-engine",
        )
        # Tracks whether the user is currently in a sustained flow
        # session. Set/cleared by flow_state_changed events. The voice
        # engine itself only speaks in response to direct user input,
        # so today there's no spontaneous speech to silence — but the
        # flag is kept current for any future consumer that wants it,
        # and for the session-end announcement below.
        self._flow_active: bool = False

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

        # Wake-word listener — two modes:
        #
        # 1. "whisper" (default): Whisper-based loop that records short
        #    segments, transcribes them, and matches wake phrases.
        #    Handles both wake detection AND shutdown phrases.
        #
        # 2. "openwakeword": custom ONNX model for wake detection
        #    (~80 ms latency, much more reliable when trained on the
        #    user's voice). The Whisper-based listener STILL runs in
        #    parallel but with wake matching disabled — it only watches
        #    for shutdown phrases like "bye ultron".
        use_oww = (
            self.cfg.enable_wake_word
            and self.cfg.wake_engine == "openwakeword"
        )
        oww_model_path = self.cfg.wake_model_path
        if use_oww and not oww_model_path:
            import os
            appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
            oww_model_path = os.path.join(
                appdata, "ULTRON", "wake_models", "hey_ultron.onnx"
            )
        if use_oww and not os.path.exists(oww_model_path):
            logger.warning(
                "wake_engine=openwakeword but model not found at %s — "
                "falling back to Whisper-based wake detection",
                oww_model_path,
            )
            use_oww = False

        if use_oww:
            # Primary: openWakeWord for "hey ultron" detection.
            try:
                self.oww_listener = OpenWakeWordListener(
                    model_path=oww_model_path,
                    sample_rate=self.cfg.sample_rate,
                    device=self.cfg.audio_input_device,
                    threshold=self.cfg.wake_threshold,
                    patience=self.cfg.wake_patience,
                    cooldown_secs=self.cfg.post_speak_cooldown_secs,
                    on_wake_word=self._on_wake_word,
                    is_busy=self._is_busy,
                    publish=self.bridge.publish,
                )
                self.oww_listener.start()
                logger.info("openWakeWord listener active (model=%s)", oww_model_path)
            except Exception as exc:
                logger.error("openWakeWord listener failed to start (%s)", exc)
                self.oww_listener = None
                use_oww = False

            # Secondary: Whisper listener in shutdown-only mode.
            # Pass an empty wake_words list so _extract_query never
            # matches, but shutdown-phrase detection still runs.
            if self.stt is not None:
                try:
                    self.wake_word_listener = WakeWordListener(
                        stt=self.stt,
                        vad=self.vad,
                        sample_rate=self.cfg.sample_rate,
                        segment_max_secs=self.cfg.wake_segment_max_secs,
                        silence_timeout_ms=self.cfg.silence_timeout_ms,
                        device=self.cfg.audio_input_device,
                        wake_words=[],
                        on_wake_word=self._on_wake_word,
                        is_busy=self._is_busy,
                        publish=self.bridge.publish,
                        on_shutdown_phrase=self._on_shutdown_phrase,
                    )
                    self.wake_word_listener.start()
                except Exception as exc:
                    logger.error("shutdown-phrase listener failed (%s)", exc)

        if not use_oww and self.cfg.enable_wake_word and self.cfg.wake_words and self.stt is not None:
            # Whisper-only mode — handles both wake + shutdown.
            try:
                self.wake_word_listener = WakeWordListener(
                    stt=self.stt,
                    vad=self.vad,
                    sample_rate=self.cfg.sample_rate,
                    segment_max_secs=self.cfg.wake_segment_max_secs,
                    silence_timeout_ms=self.cfg.silence_timeout_ms,
                    device=self.cfg.audio_input_device,
                    wake_words=self.cfg.wake_words,
                    on_wake_word=self._on_wake_word,
                    is_busy=self._is_busy,
                    publish=self.bridge.publish,
                    on_shutdown_phrase=self._on_shutdown_phrase,
                )
                self.wake_word_listener.start()
            except Exception as exc:
                logger.error("wake word listener failed to start (%s)", exc)
                self.wake_word_listener = None

        wake_info = (
            f"openwakeword ({oww_model_path})" if self.oww_listener
            else (self.cfg.wake_words if self.wake_word_listener else "[disabled]")
        )
        logger.info(
            "voice engine ready: state=%s hotkey=%s tts=%s wake=%s",
            self.state_machine.state.value,
            self.cfg.hotkey,
            self.cfg.tts_backend,
            wake_info,
        )
        await self.bridge.run_forever()

    async def shutdown(self) -> None:
        """Graceful shutdown — stop hotkey, cancel any in-flight task."""
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        if self.oww_listener is not None:
            await self.oww_listener.stop()
        if self.wake_word_listener is not None:
            await self.wake_word_listener.stop()
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
    # Predicates / publishers used by subsystems running on other threads
    # ------------------------------------------------------------------

    def _is_busy(self) -> bool:
        """True if the engine should NOT accept a new wake-word trigger.

        We're busy when:
        - the state machine is in any non-IDLE state, OR
        - we're inside the post-SPEAKING cooldown window (speaker echo
          would otherwise re-trigger the wake listener on ULTRON's own
          voice).
        """
        if self.state_machine is None:
            return True
        if self.state_machine.state != VoiceState.IDLE:
            return True
        if self._last_speak_end_ts <= 0:
            return False
        return (time.monotonic() - self._last_speak_end_ts) < self.cfg.post_speak_cooldown_secs

    def _publish_mic_level(self, peak: float, source: str) -> None:
        """Thread-safe mic-level publisher.

        Called from the executor thread that owns the sounddevice loop.
        Bridges back onto the asyncio loop via run_coroutine_threadsafe.
        """
        if self.loop is None or self.bridge is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.bridge.publish(
                    "voice_mic_level",
                    {"peak": float(peak), "source": source},
                ),
                self.loop,
            )
        except RuntimeError:
            # Loop is shutting down; drop silently.
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
        elif kind == "flow_state_changed":
            await self._handle_flow_state_changed(payload)
        elif kind == "reentry_brief":
            await self._handle_reentry_brief(payload)
        # Anything else: ignored.

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

    async def _handle_flow_state_changed(self, payload: dict) -> None:
        """Track flow state + announce session end if substantial.

        We deliberately keep this terse: a 1-sentence summary, only
        when the session was long enough to be worth interrupting for.
        Short sessions (< 5 min) get logged silently — the HUD still
        shows them via flow_query.
        """
        state = str(payload.get("state") or "")
        prev = str(payload.get("prev_state") or "")
        self._flow_active = (state == "active")
        if state == "broken" and prev == "active":
            minutes = float(payload.get("duration_minutes") or 0.0)
            reason = (payload.get("reason") or "").strip()
            if minutes >= 5.0:
                # Keep it short so it doesn't disrupt the next task.
                # Reason translates the regex tag into something
                # readable; falls back to the raw tag for unknowns.
                pretty = {
                    "app_switch": "an app switch",
                    "idle": "stepping away",
                    "tension_spike": "rising tension",
                    "cognitive_overload": "cognitive overload",
                    "backspace_burst": "getting stuck",
                    "unproductive_app": "switching apps",
                    "disengaged": "disengaging",
                }.get(reason, reason or "a context shift")
                line = f"Flow session: {int(round(minutes))} minutes, broken by {pretty}."
                try:
                    await self._speak_directly(line)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("flow announce failed: %s", exc)

    async def _handle_reentry_brief(self, payload: dict) -> None:
        """Speak the re-entry brief composed by the reentry service.

        The reentry service handles all threshold/cooldown gating, so
        any brief that lands here is meant to be spoken. We refuse only
        if the voice engine is mid-request (don't talk over the user).
        """
        text = (payload.get("text") or "").strip()
        if not text:
            return
        if self.state_machine is not None and self.state_machine.state in (
            VoiceState.LISTENING, VoiceState.PROCESSING, VoiceState.SPEAKING,
        ):
            logger.info("reentry_brief: voice busy, skipping speak")
            return
        try:
            await self._speak_directly(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reentry brief speak failed: %s", exc)

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

    # ------------------------------------------------------------------
    # Wake-word callback
    # ------------------------------------------------------------------

    async def _on_wake_word(self, query: str) -> None:
        """Wake-word fired; ``query`` is whatever followed the wake word.

        If empty, we treat it as a hotkey-style "now listening" prompt and
        record the follow-up utterance. If non-empty, we skip recording
        and go straight to PROCESSING with the captured query.
        """
        if self._wake_lock.locked():
            return
        async with self._wake_lock:
            await self._on_wake_word_inner(query)

    async def _on_wake_word_inner(self, query: str) -> None:
        if self.state_machine is None or self.bridge is None:
            return
        if self.state_machine.state != VoiceState.IDLE:
            return
        if self._current_request is not None and not self._current_request.done():
            return
        now = time.monotonic()
        if (now - self._last_wake_ts) < 3.0:
            return
        self._last_wake_ts = now

        # Publish wake_word_armed HERE (inside the dedup guard) so the
        # HUD only sees one event per wake session. Previously this was
        # in the wake listener which fires before dedup can reject.
        if self.bridge is not None:
            try:
                await self.bridge.publish("wake_word_armed", {
                    "transcript": query or "(wake)",
                    "query": query or "",
                    "has_trailing_query": bool(query),
                })
            except Exception:
                pass

        query = query.strip()
        if not query:
            logger.info("wake word with no query -- starting LISTENING")
            await self.state_machine.transition(VoiceState.LISTENING, "wake_word")
            self._current_request = asyncio.create_task(
                self._record_and_transcribe("wake_word")
            )
            return

        # Query captured in the same utterance -- short-circuit to PROCESSING.
        logger.info("wake word + query -- forwarding %r to module C", query[:80])
        self._llm_response_event.clear()
        self._pending_response_text = ""
        self._pending_response_error = False
        await self.state_machine.transition(VoiceState.PROCESSING, "wake_word_query")
        await self.bridge.publish("voice_transcript", {
            "text": query,
            "duration_secs": 0.0,
            "confidence": 0.95,
            "activation": "wake_word",
            "ts_unix_ms": int(time.time() * 1000),
        })
        try:
            await asyncio.wait_for(
                self._llm_response_event.wait(),
                timeout=self.cfg.llm_response_timeout_secs,
            )
        except asyncio.TimeoutError:
            logger.warning("llm_response timeout after wake word")
            await self.state_machine.transition(VoiceState.ERROR, "llm_timeout")
            return

        if self._pending_response_error:
            if self._pending_response_text:
                await self._synthesize_and_play(self._pending_response_text)
            await self.state_machine.transition(VoiceState.ERROR, "llm_error")
            return
        if not self._pending_response_text:
            await self.state_machine.transition(VoiceState.IDLE, "empty_response")
            return
        await self._synthesize_and_play(self._pending_response_text)
        await self.state_machine.transition(VoiceState.IDLE, "wake_word_done")

    async def _on_shutdown_phrase(self) -> None:
        """User said 'bye ultron' / 'shutdown ultron' / etc.

        Speak a canned farewell (no LLM round-trip — the model is about to
        be killed), then spawn `ultron.ps1 stop` as a detached process and
        exit this voice_engine. The launcher's stop command will tear down
        every sidecar including this process's parent terminal.
        """
        import os
        import subprocess
        import sys as _sys

        farewell = (
            "Goodbye, sir. I'll be here when you return. "
            "Shutting down the stack now."
        )
        logger.info("shutdown phrase: speaking farewell + scheduling stack stop")

        if self.bridge is not None:
            try:
                await self.bridge.publish("voice_shutdown_initiated", {
                    "farewell": farewell,
                    "ts_unix_ms": int(time.time() * 1000),
                })
            except Exception:
                pass

        try:
            if self.state_machine is not None:
                await self.state_machine.transition(VoiceState.SPEAKING, "shutdown_phrase")
            await self._synthesize_and_play(farewell)
        except Exception as exc:
            logger.warning("farewell playback failed: %s", exc)

        # Spawn a VISIBLE (minimized) powershell that actually runs
        # `ultron.ps1 stop`. Previous version used WindowStyle Hidden +
        # DETACHED_PROCESS, which (a) hid every failure and (b) could
        # get orphan-killed when our own process exited before the
        # nested PowerShell started. Going via `cmd /c start /MIN` is
        # the most reliable detachment Windows offers — the new console
        # outlives this process, and the user can see in the taskbar
        # what's happening if anything goes wrong.
        try:
            # `cmd /c start "title" /MIN powershell -File <ps1> stop`
            # — start fully detaches; /MIN keeps the window out of the
            # foreground; the nested PowerShell runs the script and
            # exits when done, closing the window.
            inner = (
                "Write-Host 'ULTRON: stopping the stack...' "
                "-ForegroundColor Cyan; "
                "Start-Sleep -Milliseconds 1500; "
                "& 'C:\\dev\\ultron.ps1' stop; "
                "Write-Host 'ULTRON: stack stopped.' -ForegroundColor Green; "
                "Start-Sleep -Seconds 3"
            )
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "ULTRON shutdown", "/MIN",
                 "powershell.exe", "-NoProfile",
                 "-ExecutionPolicy", "Bypass",
                 "-Command", inner],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, close_fds=True,
            )
            logger.info("ultron.ps1 stop scheduled in a minimized window — exiting")
        except Exception as exc:
            logger.error("failed to schedule stop: %s", exc)

        # Best-effort: stop our own wake listener first (avoid mic contention
        # during the brief overlap before ultron.ps1 stop kills us).
        try:
            if self.wake_word_listener is not None:
                await self.wake_word_listener.stop()
        except Exception:
            pass
        # Exit cleanly. The detached stop script will kill anything that
        # didn't exit on its own.
        os._exit(0)

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
            self._last_speak_end_ts = time.monotonic()
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
            # Mark the moment playback ended so the wake listener's
            # post-SPEAKING cooldown starts ticking. Caller handles
            # the SPEAKING → next-state transition itself.
            self._last_speak_end_ts = time.monotonic()


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
