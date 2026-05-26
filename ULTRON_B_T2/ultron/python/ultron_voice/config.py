"""Voice Engine configuration.

The voice sidecar reads the same ``%APPDATA%/ULTRON/config.toml`` that every
other ULTRON process reads. The ``[voice]`` section is the only block we own;
``[bridge]`` is shared with the rest of the stack.

Design notes
------------

- All fields have sensible defaults so an existing Phase-1 config.toml (which
  predates ``[voice]``) parses without errors. Missing fields fall back to
  the dataclass defaults.

- Paths are kept as ``str`` rather than ``Path`` so TOML round-trips cleanly
  with the rest of the codebase (mirrors how ``[memory]`` did it).

- The ``audio_input_device`` / ``audio_output_device`` knobs are ``None`` by
  default → use the OS default device. Set a specific index if the user has
  multiple sound cards and wants to pin one.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Python 3.11+ has tomllib in stdlib; older Python uses the tomli backport
# already pinned in requirements.txt.
if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover — covered by stdlib on 3.11+
    import tomli as tomllib  # type: ignore[import-not-found]

logger = logging.getLogger("ultron.voice.config")


@dataclass
class VoiceConfig:
    """Resolved voice-engine configuration.

    Constructed by :func:`load_voice_config` from ``config.toml``. Tests
    instantiate directly with explicit values — no I/O.
    """

    # ── Bridge (shared with all ULTRON processes) ──────────────────────
    ws_url: str = "ws://127.0.0.1:9420/ws"
    token: str = ""

    # ── STT (faster-whisper) ───────────────────────────────────────────
    whisper_model: str = "large-v3-turbo"
    # "cuda" runs on the RTX 5070 Ti; "cpu" is the OOM fallback.
    whisper_device: str = "cuda"
    # int8 keeps VRAM use modest (~2.5 GB) and still hits ~200ms on turbo.
    whisper_compute_type: str = "int8"
    # None = auto-detect language; "en" is faster and matches Priyanshu's use.
    whisper_language: Optional[str] = "en"

    # ── TTS ────────────────────────────────────────────────────────────
    # "kokoro" (local ONNX, ~24kHz, best quality) — primary.
    # "piper" — local CPU, lighter weight.
    # Both fall back to "edge_tts" on failure (needs internet).
    tts_backend: str = "kokoro"
    # Empty = auto-download to %APPDATA%/ULTRON/models/piper/ on first use.
    piper_model_path: str = ""
    piper_voice: str = "en_US-lessac-medium"
    # Kokoro model files in %APPDATA%/ULTRON/models/kokoro/ — resolved at load.
    kokoro_model_path: str = ""
    kokoro_voices_path: str = ""
    kokoro_voice: str = "af_heart"     # warm female. See voices-v1.0.bin manifest.
    kokoro_speed: float = 1.0
    kokoro_lang: str = "en-us"
    # IST-friendly default voice for Edge-TTS fallback.
    edge_tts_voice: str = "en-IN-NeerjaNeural"

    # ── Activation ─────────────────────────────────────────────────────
    # "hotkey" only / "clap" only / "both".
    activation_mode: str = "hotkey"
    hotkey: str = "ctrl+shift+space"

    # Wake word: continuously listen for one of these phrases at the start
    # of a spoken utterance; the rest of the utterance is sent as the query.
    # Empty list = wake word disabled.
    wake_words: list[str] = field(default_factory=lambda: ["hey ultron", "hey altron"])
    # Wake-hunt segments are short on purpose. The user gets feedback the
    # instant the wake phrase is recognised; the COMMAND that follows is
    # captured in a fresh recording session driven by the engine
    # (silence_timeout_ms governs that one). A short cap means a bare
    # "hey ultron" doesn't wait through a 60s buffer + VAD timeout before
    # we fire — it caps quickly and processes. Long single-breath
    # utterances ("hey ultron play music") still work: the wake hunt
    # captures whatever fits in the cap, the matcher extracts the
    # trailing query, and we forward it straight to the LLM.
    wake_segment_max_secs: int = 5
    enable_wake_word: bool = True
    # "whisper" = existing Whisper-based wake (works out of the box).
    # "openwakeword" = custom ONNX model trained by train_wake_model.py.
    # openWakeWord is faster (~80 ms vs ~300 ms latency) and more reliable
    # when a custom model is trained on the user's voice.
    wake_engine: str = "whisper"
    # Path to the trained .onnx model. Empty = auto-resolve to
    # %APPDATA%/ULTRON/wake_models/hey_ultron.onnx.
    wake_model_path: str = ""
    # openWakeWord score threshold (0-1). Higher = fewer false positives
    # but may miss quiet utterances. 0.5 is a safe default.
    wake_threshold: float = 0.5
    # How many consecutive 80 ms chunks above threshold before firing.
    # 3 = ~240 ms of sustained high score. Reduces single-frame spikes.
    wake_patience: int = 3
    # After ULTRON finishes speaking, suppress the wake listener for this
    # many seconds. Stops it from transcribing speaker leakage or the user's
    # immediate follow-up chatter as a new wake-word trigger.
    post_speak_cooldown_secs: float = 2.5

    # ── Recording ──────────────────────────────────────────────────────
    # Slightly looser VAD so quiet phrasing isn't classified as silence.
    vad_threshold: float = 0.4
    # End-of-speech threshold. Earlier values (1500, 3000, 4500) still
    # cut the user off mid-thought on long requests with natural pauses
    # ("Ultron, … [thinking] … remind me to …"). 7000 ms is generous
    # enough to bridge a long pause; you can still "send it" early by
    # releasing the hotkey.
    silence_timeout_ms: int = 7000
    # Lifted from 60 → 180 s so longer dictations (a journal entry or a
    # multi-step instruction) can run without being clipped.
    max_record_secs: int = 180
    # Whisper's native sample rate. Don't change unless you also change
    # SileroVAD's chunk size — they're coupled (512 samples = 32ms at 16kHz).
    sample_rate: int = 16000
    audio_input_device: Optional[int] = None
    audio_output_device: Optional[int] = None

    # ── Response handling ──────────────────────────────────────────────
    # 600 was way too tight — clipped mid-sentence on any real answer.
    # The LLM can produce ~3000 chars at 1024 tokens; let TTS speak them.
    max_tts_chars: int = 4000
    llm_response_timeout_secs: float = 30.0

    # ── Internal: paths derived at load time, not configurable ─────────
    data_dir: str = field(default="")

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def piper_models_dir(self) -> Path:
        """Where Piper voice models live. Auto-created on first synthesis."""
        return Path(self.data_dir) / "models" / "piper"


def _default_config_path() -> Path:
    """Resolve the config.toml path the same way every other sidecar does.

    Env override > %APPDATA% (Windows) > ~/.config (everything else).
    """
    if env := os.environ.get("ULTRON_CONFIG"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        )
    return base / "ULTRON" / "config.toml"


def load_voice_config(path: Optional[Path] = None) -> VoiceConfig:
    """Load and parse the ``[voice]`` section + bridge details.

    Missing ``[voice]`` section is fine — every field has a default. Missing
    bridge details are NOT fine — we can't run without them. We bubble those
    up as a hard error.
    """
    cfg_path = path or _default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config.toml not found at {cfg_path}. "
            "Start ultron-core at least once to bootstrap the file, "
            "or set ULTRON_CONFIG to point at an existing one."
        )
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    bridge = raw.get("bridge", {})
    if "bind" not in bridge or "token" not in bridge:
        raise ValueError(
            f"{cfg_path}: [bridge] must contain 'bind' and 'token' — "
            "is this a complete ULTRON config?"
        )
    ws_url = f"ws://{bridge['bind']}/ws"
    token = bridge["token"]

    general = raw.get("general", {})
    data_dir = str(general.get("data_dir", ""))

    voice_section = raw.get("voice", {})

    # Construct by passing only the keys we recognise — unknown keys in
    # the file are silently ignored to keep forward-compat clean.
    known = {f.name for f in VoiceConfig.__dataclass_fields__.values()}
    overrides = {k: v for k, v in voice_section.items() if k in known}

    cfg = VoiceConfig(
        ws_url=ws_url,
        token=token,
        data_dir=data_dir,
        **overrides,
    )
    # Resolve Kokoro paths if not explicitly set in [voice].
    if data_dir and not cfg.kokoro_model_path:
        cfg.kokoro_model_path = str(Path(data_dir).parent / "models" / "kokoro" / "kokoro-v1.0.onnx")
    if data_dir and not cfg.kokoro_voices_path:
        cfg.kokoro_voices_path = str(Path(data_dir).parent / "models" / "kokoro" / "voices-v1.0.bin")
    logger.info(
        "voice config loaded: model=%s device=%s tts=%s hotkey=%s",
        cfg.whisper_model,
        cfg.whisper_device,
        cfg.tts_backend,
        cfg.hotkey,
    )
    return cfg
