"""List every input device + record a 3s test from each to find the live one."""
import time
import numpy as np
import sounddevice as sd

print("All input devices:\n")
devs = sd.query_devices()
default_in = sd.default.device[0]
for i, d in enumerate(devs):
    if d.get("max_input_channels", 0) > 0:
        marker = "  <- DEFAULT" if i == default_in else ""
        print(f"  [{i}] {d['name']}  ({int(d['default_samplerate'])} Hz, {d['max_input_channels']} ch){marker}")

print()
print("Testing each input device for 3 seconds. Speak loudly when prompted.\n")
for i, d in enumerate(devs):
    if d.get("max_input_channels", 0) == 0:
        continue
    name = d["name"]
    print(f"--- [{i}] {name}: speak now (3s)...", end=" ", flush=True)
    try:
        audio = sd.rec(
            3 * 16000,
            samplerate=16000,
            channels=1,
            dtype="float32",
            device=i,
        )
        sd.wait()
        audio = audio[:, 0] if audio.ndim == 2 else audio
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio**2)))
        status = "HOT" if peak > 0.1 else ("warm" if peak > 0.02 else "silent")
        print(f"peak={peak:.4f} rms={rms:.4f}  [{status}]")
    except Exception as exc:
        print(f"ERROR: {exc}")
