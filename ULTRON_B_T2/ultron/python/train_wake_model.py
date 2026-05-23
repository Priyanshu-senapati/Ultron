"""Train a custom openWakeWord model for "hey ultron" from voice samples.

Run after ``record_wake_samples.py``::

    python train_wake_model.py

Reads clips from ``%APPDATA%/ULTRON/wake_training/{positive,negative}/``,
augments + extracts features via openwakeword's embedding pipeline, trains
a small DNN classifier, and exports an ONNX model to
``%APPDATA%/ULTRON/wake_models/hey_ultron.onnx``.

Once the model file exists, set ``wake_engine = "openwakeword"`` in
``config.toml`` and restart ULTRON.
"""
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path
from typing import Optional

import numpy as np

SAMPLE_RATE = 16000
CLIP_SAMPLES = SAMPLE_RATE * 2  # 2 seconds per clip

BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _training_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON" / "wake_training"


def _model_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON" / "wake_models"


def _load_wav(path: Path) -> Optional[np.ndarray]:
    """Load a WAV file as int16 mono at SAMPLE_RATE. Returns None on error."""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getframerate() != SAMPLE_RATE:
                return None
            raw = wf.readframes(wf.getnframes())
            audio = np.frombuffer(raw, dtype=np.int16)
            if wf.getnchannels() > 1:
                audio = audio[::wf.getnchannels()]
            return audio
    except Exception:
        return None


def _load_clips(folder: Path) -> list[np.ndarray]:
    """Load all WAV files from folder, pad/trim to CLIP_SAMPLES."""
    clips = []
    if not folder.exists():
        return clips
    for wav in sorted(folder.glob("*.wav")):
        audio = _load_wav(wav)
        if audio is None:
            continue
        if len(audio) < CLIP_SAMPLES:
            audio = np.pad(audio, (0, CLIP_SAMPLES - len(audio)))
        else:
            audio = audio[:CLIP_SAMPLES]
        clips.append(audio)
    return clips


def _augment(clips: list[np.ndarray], factor: int = 4) -> list[np.ndarray]:
    """Create augmented copies with speed, volume, shift, and noise."""
    rng = np.random.default_rng(42)
    augmented: list[np.ndarray] = []
    for clip in clips:
        for _ in range(factor):
            aug = clip.astype(np.float64)
            # Volume perturbation (0.5x – 1.5x)
            aug *= rng.uniform(0.5, 1.5)
            # Time shift (shift up to 10% of clip length)
            shift = rng.integers(-CLIP_SAMPLES // 10, CLIP_SAMPLES // 10)
            aug = np.roll(aug, shift)
            # Add noise overlay
            noise_level = rng.uniform(0.0, 0.05) * 32768
            aug += rng.normal(0, noise_level, len(aug))
            aug = np.clip(aug, -32768, 32767).astype(np.int16)
            # Pad/trim
            if len(aug) < CLIP_SAMPLES:
                aug = np.pad(aug, (0, CLIP_SAMPLES - len(aug)))
            else:
                aug = aug[:CLIP_SAMPLES]
            augmented.append(aug)
    return augmented


def _generate_synthetic_negatives(count: int) -> list[np.ndarray]:
    """Generate synthetic negative clips: silence, noise, random patterns."""
    rng = np.random.default_rng(123)
    clips = []
    for i in range(count):
        kind = i % 4
        if kind == 0:
            # Silence with minimal noise
            clip = (rng.normal(0, 50, CLIP_SAMPLES)).astype(np.int16)
        elif kind == 1:
            # White noise
            clip = (rng.normal(0, 2000, CLIP_SAMPLES)).astype(np.int16)
        elif kind == 2:
            # Low-frequency hum (simulates fan/AC)
            t = np.arange(CLIP_SAMPLES) / SAMPLE_RATE
            clip = (np.sin(2 * np.pi * 60 * t) * 1000 +
                    rng.normal(0, 300, CLIP_SAMPLES)).astype(np.int16)
        else:
            # Random speech-like noise (modulated noise)
            t = np.arange(CLIP_SAMPLES) / SAMPLE_RATE
            envelope = np.abs(np.sin(2 * np.pi * rng.uniform(1, 5) * t))
            clip = (rng.normal(0, 3000, CLIP_SAMPLES) * envelope).astype(np.int16)
        clips.append(clip)
    return clips


def _compute_features(clips: list[np.ndarray], batch_size: int = 32) -> np.ndarray:
    """Convert int16 clips to openwakeword embedding features."""
    from openwakeword.utils import AudioFeatures
    F = AudioFeatures(inference_framework="onnx")

    all_features = []
    for i in range(0, len(clips), batch_size):
        batch = np.stack(clips[i:i + batch_size])
        feats = F.embed_clips(batch, ncpu=1)
        all_features.append(feats)
    return np.concatenate(all_features, axis=0)


def _train_model(pos_features: np.ndarray, neg_features: np.ndarray,
                 epochs: int = 80, lr: float = 0.001) -> "torch.nn.Module":
    """Train a small DNN classifier on the embeddings."""
    import torch
    from torch import nn, optim

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  Training on: {BOLD}{device}{RESET}")

    input_shape = pos_features.shape[1:]  # (16, 96)
    flat_dim = input_shape[0] * input_shape[1]

    class WakeNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.net = nn.Sequential(
                nn.Flatten(),
                nn.Linear(flat_dim, 128),
                nn.LayerNorm(128),
                nn.ReLU(),
                nn.Linear(128, 128),
                nn.LayerNorm(128),
                nn.ReLU(),
                nn.Linear(128, 1),
                nn.Sigmoid(),
            )

        def forward(self, x):
            return self.net(x)

    model = WakeNet().to(device)

    X_pos = torch.tensor(pos_features, dtype=torch.float32)
    X_neg = torch.tensor(neg_features, dtype=torch.float32)
    y_pos = torch.ones(len(X_pos), 1)
    y_neg = torch.zeros(len(X_neg), 1)
    X = torch.cat([X_pos, X_neg]).to(device)
    y = torch.cat([y_pos, y_neg]).to(device)

    # Shuffle
    perm = torch.randperm(len(X))
    X, y = X[perm], y[perm]

    # Train/val split (90/10)
    split = int(len(X) * 0.9)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    print(f"  Train: {len(X_train)} samples, Val: {len(X_val)} samples")
    print(f"  Positive: {len(pos_features)}, Negative: {len(neg_features)}")

    optimizer = optim.Adam(model.parameters(), lr=lr)
    criterion = nn.BCELoss()
    best_val_loss = float("inf")
    best_state = None

    for epoch in range(1, epochs + 1):
        model.train()
        # Mini-batch training
        batch_size = 64
        epoch_loss = 0.0
        n_batches = 0
        for bi in range(0, len(X_train), batch_size):
            xb = X_train[bi:bi + batch_size]
            yb = y_train[bi:bi + batch_size]
            out = model(xb)
            loss = criterion(out, yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            n_batches += 1

        model.eval()
        with torch.no_grad():
            val_out = model(X_val)
            val_loss = criterion(val_out, y_val).item()
            val_pred = (val_out > 0.5).float()
            val_acc = (val_pred == y_val).float().mean().item()
            # Recall on positives
            val_pos_mask = y_val == 1
            if val_pos_mask.sum() > 0:
                val_recall = (val_pred[val_pos_mask] == 1).float().mean().item()
            else:
                val_recall = 0.0

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if epoch % 10 == 0 or epoch == 1:
            bar_len = min(40, int(val_acc * 40))
            bar = "█" * bar_len + "░" * (40 - bar_len)
            print(
                f"  epoch {epoch:3d}/{epochs}  "
                f"loss {epoch_loss / n_batches:.4f}  "
                f"val_loss {val_loss:.4f}  "
                f"val_acc {val_acc:.3f}  "
                f"recall {val_recall:.3f}  "
                f"{DIM}{bar}{RESET}"
            )

    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    model.to("cpu")
    return model


def _export_onnx(model: "torch.nn.Module", out_path: Path,
                 input_shape: tuple = (16, 96)) -> None:
    """Export the trained PyTorch model to ONNX."""
    import torch
    dummy = torch.randn(1, *input_shape)
    torch.onnx.export(
        model, dummy, str(out_path),
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=13,
    )


def main() -> None:
    training_dir = _training_dir()
    pos_dir = training_dir / "positive"
    neg_dir = training_dir / "negative"
    model_out_dir = _model_dir()

    if not pos_dir.exists() or not list(pos_dir.glob("*.wav")):
        print(f"{RED}No positive samples found in {pos_dir}{RESET}")
        print(f"Run {BOLD}python record_wake_samples.py{RESET} first.")
        sys.exit(1)

    print()
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print(f"{BOLD}{CYAN}   ULTRON — Wake-Word Model Trainer{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print()

    # ── Load clips ─────────────────────────────────────────────────────
    print(f"{BOLD}Loading clips...{RESET}")
    pos_clips = _load_clips(pos_dir)
    neg_clips = _load_clips(neg_dir)
    print(f"  Positive clips:  {GREEN}{len(pos_clips)}{RESET}")
    print(f"  Negative clips:  {YELLOW}{len(neg_clips)}{RESET}")

    # ── Augment positive clips ─────────────────────────────────────────
    print(f"\n{BOLD}Augmenting positive clips (4x)...{RESET}")
    aug_pos = _augment(pos_clips, factor=4)
    all_pos = pos_clips + aug_pos
    print(f"  Augmented total: {GREEN}{len(all_pos)}{RESET}")

    # ── Generate synthetic negatives ───────────────────────────────────
    synth_count = max(200, len(all_pos) - len(neg_clips))
    print(f"\n{BOLD}Generating {synth_count} synthetic negatives...{RESET}")
    synth_neg = _generate_synthetic_negatives(synth_count)
    all_neg = neg_clips + synth_neg
    print(f"  Negative total:  {YELLOW}{len(all_neg)}{RESET}")

    # ── Extract features ───────────────────────────────────────────────
    print(f"\n{BOLD}Extracting features (melspec → embeddings)...{RESET}")
    print(f"  Processing {len(all_pos)} positive clips...")
    pos_features = _compute_features(all_pos)
    print(f"  Processing {len(all_neg)} negative clips...")
    neg_features = _compute_features(all_neg)
    print(f"  Positive features: {pos_features.shape}")
    print(f"  Negative features: {neg_features.shape}")

    # ── Train ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}Training classifier...{RESET}")
    model = _train_model(pos_features, neg_features, epochs=80)

    # ── Export ──────────────────────────────────────────────────────────
    model_out_dir.mkdir(parents=True, exist_ok=True)
    out_path = model_out_dir / "hey_ultron.onnx"
    print(f"\n{BOLD}Exporting to ONNX...{RESET}")
    _export_onnx(model, out_path)
    print(f"  {GREEN}✓ Model saved to {out_path}{RESET}")

    print()
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print(f"  {GREEN}Training complete!{RESET}")
    print()
    print(f"  Model: {out_path}")
    print(f"  Size:  {out_path.stat().st_size / 1024:.1f} KB")
    print()
    print(f"  To activate, add to config.toml [voice] section:")
    print(f'    {BOLD}wake_engine = "openwakeword"{RESET}')
    print()
    print(f"  Then restart ULTRON: {BOLD}ultron restart{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 56}{RESET}")
    print()


if __name__ == "__main__":
    main()
