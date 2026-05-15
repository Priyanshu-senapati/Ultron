"""ULTRON Module B — Voice Engine.

Subsystems:
    config        — VoiceConfig + load_voice_config()
    stt           — WhisperSTT (faster-whisper, GPU-by-default)
    tts           — TTSEngine (Piper local, Edge-TTS fallback)
    vad           — SileroVAD (end-of-speech detection during recording)
    audio_io      — AudioRecorder + AudioPlayer (sounddevice / WASAPI)
    hotkey        — HotkeyListener (keyboard library, Windows-focused)
    state_machine — VoiceStateMachine (idle/listening/processing/speaking/error)
    clap_handler  — Maps H's clap_count events to voice actions

The orchestrator lives at the workspace root: `python/voice_engine.py`.
Run it directly — there's no module-level entry point here.
"""

# We deliberately do NOT eagerly import every submodule here. Three reasons:
#
# 1. Most submodules pull in heavy deps (faster-whisper, torch, sounddevice).
#    Importing the package for a one-off util shouldn't require GPU drivers.
# 2. The orchestrator imports what it needs explicitly — by-name imports
#    make the dependency graph readable.
# 3. Tests can import individual submodules with mocked deps without
#    triggering torch.hub downloads or PortAudio init.
#
# If you want the convenience name, just do `from ultron_voice.config import VoiceConfig`.

# Only the cheap pure-Python pieces are re-exported here. Anything else
# the caller imports by full path.
from .config import VoiceConfig, load_voice_config

__all__ = [
    "VoiceConfig",
    "load_voice_config",
]
