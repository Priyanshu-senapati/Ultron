"""Record wake-word voice samples for custom openWakeWord model training.

Run interactively::

    python record_wake_samples.py

You'll be prompted to say "hey ultron" 100 times, then 20 negative
samples (say ANYTHING ELSE — random words, sentences, counting). Each
clip is saved as a 16 kHz mono 16-bit WAV under
``%APPDATA%/ULTRON/wake_training/{positive,negative}/``.

After recording, run ``python train_wake_model.py`` to train the model
locally using the collected samples.
"""
from __future__ import annotations

import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

# sounddevice may not be installed outside the venv — lazy import so
# the module can be imported for tests without it.
try:
    import sounddevice as sd  # type: ignore[import-not-found]
except ImportError:
    sd = None  # type: ignore[assignment]

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
CLIP_SECS = 2.0
POSITIVE_COUNT = 100
NEGATIVE_COUNT = 20

# ANSI
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _output_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON" / "wake_training"


def _save_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())


def _record_clip(secs: float = CLIP_SECS) -> np.ndarray:
    """Record a single clip and return int16 numpy array."""
    if sd is None:
        raise RuntimeError("sounddevice not installed")
    frames = int(secs * SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
                   dtype=DTYPE, blocking=True)
    return audio.flatten()


def _countdown(label: str, secs: int = 3) -> None:
    for i in range(secs, 0, -1):
        sys.stdout.write(f"\r  {DIM}{label} in {i}...{RESET}  ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write(f"\r  {GREEN}{BOLD}▶ NOW{RESET}                    \n")
    sys.stdout.flush()


def _record_set(out_dir: Path, count: int, prompt: str, label: str) -> int:
    """Record ``count`` clips, return how many succeeded."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(out_dir.glob("*.wav")))
    recorded = 0

    for i in range(1, count + 1):
        idx = existing + i
        print(f"\n  {CYAN}{BOLD}[{i}/{count}]{RESET} {prompt}")
        _countdown("Recording", secs=2)
        try:
            audio = _record_clip()
        except Exception as exc:
            print(f"  {RED}recording failed: {exc}{RESET}")
            continue

        peak = float(np.max(np.abs(audio))) / 32768.0
        if peak < 0.005:
            print(f"  {YELLOW}very quiet (peak={peak:.4f}) — saving anyway{RESET}")

        path = out_dir / f"sample_{idx:04d}.wav"
        _save_wav(path, audio)
        recorded += 1
        bar_len = min(40, int(peak * 120))
        bar = "█" * bar_len + "░" * (40 - bar_len)
        print(f"  {GREEN}saved{RESET} {path.name}  {DIM}{bar} peak={peak:.3f}{RESET}")

    return recorded


def main() -> None:
    if sd is None:
        print(f"{RED}sounddevice is not installed. Run:  pip install sounddevice{RESET}")
        sys.exit(1)

    base = _output_dir()
    pos_dir = base / "positive"
    neg_dir = base / "negative"

    print()
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print(f"{BOLD}{CYAN}   ULTRON — Wake-Word Voice Sample Recorder{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print()
    print(f"  Output:  {base}")
    print(f"  Format:  {SAMPLE_RATE} Hz, mono, 16-bit PCM, {CLIP_SECS}s per clip")
    print()
    print(f"  {BOLD}Phase 1:{RESET} {POSITIVE_COUNT} positive samples")
    print(f"    Say {BOLD}{GREEN}\"hey ultron\"{RESET} clearly each time.")
    print(f"    Vary your tone, speed, and distance from the mic.")
    print(f"    Include some whispered / tired / casual variations.")
    print()
    print(f"  {BOLD}Phase 2:{RESET} {NEGATIVE_COUNT} negative samples")
    print(f"    Say {BOLD}{YELLOW}anything EXCEPT{RESET} \"hey ultron\".")
    print(f"    Random words, counting, humming, coughing, silence.")
    print()
    input(f"  Press {BOLD}Enter{RESET} to start phase 1 (positive samples)... ")

    # ── Phase 1: Positive ──────────────────────────────────────────────
    print(f"\n{BOLD}{GREEN}Phase 1: Positive samples{RESET}")
    print(f"  Say {BOLD}\"hey ultron\"{RESET} after each ▶ NOW prompt.\n")

    pos_count = _record_set(
        pos_dir, POSITIVE_COUNT,
        prompt=f'Say {BOLD}{GREEN}"hey ultron"{RESET}',
        label="positive",
    )

    print(f"\n  {GREEN}✓ {pos_count} positive samples recorded to {pos_dir}{RESET}")

    # ── Phase 2: Negative ──────────────────────────────────────────────
    print()
    input(f"  Press {BOLD}Enter{RESET} to start phase 2 (negative samples)... ")
    print(f"\n{BOLD}{YELLOW}Phase 2: Negative samples{RESET}")
    print(f"  Say {BOLD}anything EXCEPT{RESET} \"hey ultron\".\n")

    neg_count = _record_set(
        neg_dir, NEGATIVE_COUNT,
        prompt=f'Say {BOLD}{YELLOW}anything else{RESET} (not "hey ultron")',
        label="negative",
    )

    print(f"\n  {GREEN}✓ {neg_count} negative samples recorded to {neg_dir}{RESET}")

    # ── Summary ────────────────────────────────────────────────────────
    total_pos = len(list(pos_dir.glob("*.wav"))) if pos_dir.exists() else 0
    total_neg = len(list(neg_dir.glob("*.wav"))) if neg_dir.exists() else 0

    print()
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print(f"  {GREEN}Recording complete!{RESET}")
    print(f"  Positive: {total_pos} clips in {pos_dir}")
    print(f"  Negative: {total_neg} clips in {neg_dir}")
    print()
    print(f"  Next step: {BOLD}python train_wake_model.py{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print()


if __name__ == "__main__":
    main()
