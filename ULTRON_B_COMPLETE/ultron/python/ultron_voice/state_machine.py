"""Voice state machine.

Five states (`VoiceState` enum), one source-of-truth for transitions.
Every transition publishes a ``voice_state_changed`` event onto the
bus so the future HUD (Module L) can visualise the lifecycle.

State diagram (from the build prompt):

::

             hotkey_press / clap(1)
    IDLE ──────────────────────────────→ LISTENING
      ↑                                       │
      │                         VAD silence / hotkey release
      │                                       ↓
      │            TTS complete         PROCESSING
      │        (or empty / error)            │
    SPEAKING ←──────────────────────  voice_transcript published
      │                                       │
      │                          C publishes llm_response
      │                                       ↓
      └──────────────── ERROR ←──── TTS engine or C error

Rules (all enforced by ``transition`` / ``cancel``):

- Any state → IDLE on ``cancel()`` (escape key or barge-in).
- SPEAKING → IDLE immediately on new hotkey press (caller decides to
  re-enter LISTENING after).
- ERROR → IDLE auto-recovery after :data:`ERROR_AUTO_RECOVER_SECS`.
- Only one active voice request at a time. The orchestrator enforces
  this by checking ``state`` before starting a recording.

Concurrency
-----------

The orchestrator runs on a single asyncio loop. ``transition`` is
``async`` because it awaits the bus publish — but the in-memory state
flip happens synchronously before the publish, so any code that
checks ``state`` after ``await transition(...)`` sees the new value.

We use an ``asyncio.Lock`` around transitions because the keyboard
listener (running on a separate thread) can schedule cancel() coroutines
concurrently with the recorder finishing. Without the lock, two
near-simultaneous transitions could race and emit out-of-order
``voice_state_changed`` events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Avoid eager import — UltronBridge pulls in websockets. Tests
    # supply a duck-typed mock instead.
    from .audio_io import AudioPlayer

logger = logging.getLogger("ultron.voice.state")


# After ERROR, wait this many seconds before auto-recovering to IDLE.
# Long enough for the user to notice the error log; short enough that a
# stuck error state doesn't permanently block voice.
ERROR_AUTO_RECOVER_SECS: float = 3.0


class VoiceState(str, Enum):
    """Mirrors the wire format — values are exactly what we publish."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    ERROR = "error"


class VoiceStateMachine:
    """Single source of truth for the voice lifecycle.

    Construct with the bridge (anything with an async ``publish(kind, payload)``
    method) and a player (used by ``cancel()`` to stop in-flight audio
    on barge-in). The player is optional — leave None in unit tests.
    """

    def __init__(
        self,
        bridge,
        player: "Optional[AudioPlayer]" = None,
    ) -> None:
        self._state: VoiceState = VoiceState.IDLE
        self._bridge = bridge
        self._player = player
        # Serialise transitions so concurrent cancel() + transition()
        # land in deterministic order on the bus.
        self._lock = asyncio.Lock()
        # Handle for the ERROR → IDLE auto-recovery task. We hold it so
        # cancel() can interrupt the timer if the user does something
        # in the meantime.
        self._auto_recover_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> VoiceState:
        return self._state

    def set_player(self, player: "AudioPlayer") -> None:
        """Late-bind the player. The orchestrator constructs the state
        machine first (so subsystems can be wired into it), then attaches
        the player once it's been initialised."""
        self._player = player

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    async def transition(self, new_state: VoiceState, activation: str = "") -> None:
        """Move to ``new_state`` and publish ``voice_state_changed``.

        ``activation`` describes what caused the transition (`"hotkey"`,
        `"clap"`, `"vad"`, `"timeout"`, etc.). It's informational —
        carried on the wire for HUD/diagnostics, never gates logic here.
        """
        async with self._lock:
            prev = self._state
            if prev == new_state:
                # Idempotent — don't spam the bus with no-op events.
                logger.debug("transition no-op: already %s", new_state.value)
                return

            self._state = new_state
            logger.info(
                "state: %s → %s (activation=%s)",
                prev.value,
                new_state.value,
                activation or "-",
            )

            # Cancel any pending auto-recovery if we're leaving ERROR
            # for any reason.
            if prev == VoiceState.ERROR and self._auto_recover_task is not None:
                self._auto_recover_task.cancel()
                self._auto_recover_task = None

            await self._publish(prev, new_state, activation)

            # Arm the ERROR → IDLE timer when we enter ERROR.
            if new_state == VoiceState.ERROR:
                self._auto_recover_task = asyncio.create_task(
                    self._auto_recover_from_error()
                )

    async def cancel(self) -> None:
        """Barge-in / escape. Stop any playing audio, return to IDLE.

        Safe to call from any state — no-op if already IDLE. The player
        stop happens synchronously (sets a flag the player polls), so
        ``cancel()`` returns once the player has been *told* to stop,
        not once the audio actually finishes draining.
        """
        # Player stop is fire-and-forget — no point making it async.
        # Calling player.stop() outside the lock avoids deadlocking if
        # the player's stop hook tries to publish anything.
        if self._player is not None:
            try:
                self._player.stop()
            except Exception as exc:
                # Stopping the player is best-effort.
                logger.debug("player.stop() failed (ignored): %s", exc)

        await self.transition(VoiceState.IDLE, activation="cancel")

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------

    async def _publish(
        self,
        prev: VoiceState,
        new: VoiceState,
        activation: str,
    ) -> None:
        if self._bridge is None:
            return
        try:
            await self._bridge.publish(
                "voice_state_changed",
                {
                    "state": new.value,
                    "prev_state": prev.value,
                    "activation": activation,
                    "ts_unix_ms": int(time.time() * 1000),
                },
            )
        except Exception as exc:
            # A bus publish failure is non-fatal — the state machine
            # itself has already advanced. Log and continue.
            logger.warning("voice_state_changed publish failed: %s", exc)

    # ------------------------------------------------------------------
    # Auto-recovery
    # ------------------------------------------------------------------

    async def _auto_recover_from_error(self) -> None:
        """Sleep, then transition ERROR → IDLE. Cancelled if the user
        does something first."""
        try:
            await asyncio.sleep(ERROR_AUTO_RECOVER_SECS)
        except asyncio.CancelledError:
            return
        # Only recover if we're still in ERROR — a manual transition
        # may have already moved us elsewhere.
        if self._state == VoiceState.ERROR:
            await self.transition(VoiceState.IDLE, activation="auto_recover")
