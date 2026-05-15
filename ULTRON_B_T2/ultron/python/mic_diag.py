"""Mic + Whisper diagnostic.

Records 10 seconds from the default mic, prints the audio level it
captured, and runs Whisper on it. If the transcript is empty or wrong,
the problem is upstream of the wake-word matcher.

Run:
    python python/mic_diag.py
Then speak 'Hey Ultron, what time is it' clearly into the mic.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ultron_voice.config import load_voice_config
from ultron_voice.stt import WhisperSTT

RECORD_SECS = 10
SAMPLE_RATE = 16000

def main() -> None:
    cfg = load_voice_config()
    print(f"input device : {cfg.audio_input_device or 'default'}")
    print(f"whisper model: {cfg.whisper_model} on {cfg.whisper_device}")
    print()

    print(f"Recording {RECORD_SECS}s... speak NOW:")
    audio = sd.rec(
        RECORD_SECS * SAMPLE_RATE,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=cfg.audio_input_device,
    )
    sd.wait()
    audio = audio[:, 0] if audio.ndim == 2 else audio

    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio**2)))
    print(f"  recorded {audio.size} samples")
    print(f"  peak (raw)     : {peak:.4f}   (>~0.05 = audio captured)")
    print(f"  RMS  (raw)     : {rms:.4f}    (>~0.005 = clear speech)")

    # Show what the new auto-amplifier does to it.
    from ultron_voice.wake_word import amplify_to_peak
    audio = amplify_to_peak(audio)
    boosted_peak = float(np.max(np.abs(audio)))
    boosted_rms = float(np.sqrt(np.mean(audio**2)))
    if boosted_peak > peak * 1.5:
        print(f"  peak (boosted) : {boosted_peak:.4f}")
        print(f"  RMS  (boosted) : {boosted_rms:.4f}")

    if peak < 0.01:
        print()
        print("  -> mic captured almost nothing. Check:")
        print("     - mic not muted in Windows taskbar volume mixer")
        print("     - correct device selected as default INPUT in Sound settings")
        return

    print()
    print("Loading Whisper and transcribing...")
    t0 = time.monotonic()
    stt = WhisperSTT(
        model=cfg.whisper_model,
        device=cfg.whisper_device,
        compute_type=cfg.whisper_compute_type,
        language=cfg.whisper_language,
    )
    stt.load()
    print(f"  model loaded in {time.monotonic()-t0:.1f}s")

    t1 = time.monotonic()
    result = stt.transcribe(audio)
    print(f"  inference: {time.monotonic()-t1:.2f}s")
    print()
    print(f"TRANSCRIPT: {result.text!r}")
    print(f"  duration  : {result.duration_secs:.2f}s")
    print(f"  confidence: {result.confidence:.2f}")

    text = (result.text or "").lower()
    if "ultron" in text:
        print()
        print("  -> wake word found in transcript. Listener should fire.")
    elif text:
        print()
        print("  -> Whisper heard speech but no wake word. Speak 'Hey Ultron' more clearly.")
    else:
        print()
        print("  -> Whisper returned empty. Mic captured audio but model heard no speech.")
        print("     Try speaking louder, closer, or for longer.")

if __name__ == "__main__":
    main()
