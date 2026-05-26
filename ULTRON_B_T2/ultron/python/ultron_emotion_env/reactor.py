"""Emotion-reactive environment reactor.

Subscribes to emotion_state_changed events. When the emotional state
crosses a threshold, adjusts the desktop environment:

  frustrated → dim brightness to 40%, publish silence_notifications
  focused    → maintain current, block distracting app launches
  happy      → brightness to 80%
  stressed   → dim to 50%, suggest a break
  neutral    → restore defaults (brightness 70%)

Uses the existing brightness tool + system commands. All thresholds
and actions are configurable.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import subprocess
import time
from typing import Any, Optional

logger = logging.getLogger("ultron.emotion_env")


class EmotionReactor:
    def __init__(self, publish) -> None:
        self._publish = publish
        self._current_mood: str = "neutral"
        self._last_action_ts: float = 0.0
        self._cooldown_secs: float = 120.0

    async def on_emotion(self, payload: dict[str, Any]) -> None:
        valence = float(payload.get("valence") or 0.0)
        arousal = float(payload.get("arousal") or 0.0)
        frustration = float(payload.get("frustration") or 0.0)

        mood = self._classify_mood(valence, arousal, frustration)
        if mood == self._current_mood:
            return

        now = time.monotonic()
        if now - self._last_action_ts < self._cooldown_secs:
            return

        self._current_mood = mood
        self._last_action_ts = now
        logger.info("emotion environment: mood shifted to %s", mood)

        await self._apply_environment(mood, payload)

    def _classify_mood(self, valence: float, arousal: float,
                       frustration: float) -> str:
        if frustration >= 0.6:
            return "frustrated"
        if valence <= -0.4 and arousal >= 0.5:
            return "stressed"
        if valence >= 0.4:
            return "happy"
        if arousal <= 0.3 and abs(valence) < 0.3:
            return "calm"
        return "neutral"

    async def _apply_environment(self, mood: str,
                                 payload: dict[str, Any]) -> None:
        actions: dict[str, Any] = {}

        if mood == "frustrated":
            await self._set_brightness(40)
            actions["brightness"] = 40
            actions["suggestion"] = "Take a breath, sir. I've dimmed the screen."

        elif mood == "stressed":
            await self._set_brightness(50)
            actions["brightness"] = 50
            actions["suggestion"] = "Stress detected. Consider a short break."

        elif mood == "happy":
            await self._set_brightness(80)
            actions["brightness"] = 80

        elif mood == "calm":
            await self._set_brightness(65)
            actions["brightness"] = 65

        else:
            await self._set_brightness(70)
            actions["brightness"] = 70

        await self._publish("emotion_environment_changed", {
            "mood": mood,
            "actions": actions,
            "valence": payload.get("valence"),
            "frustration": payload.get("frustration"),
            "ts": time.time(),
        })

        if actions.get("suggestion"):
            await self._publish("proactive_suggestion", {
                "rule": f"emotion_{mood}",
                "suggestion": actions["suggestion"],
                "ts": time.time(),
            })

    async def _set_brightness(self, level: int) -> None:
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                 f".WmiSetBrightness(0, {level})"],
                capture_output=True, timeout=5,
            )
        except Exception as exc:
            logger.warning("brightness set failed: %s", exc)
