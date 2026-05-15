"""Probe Windows audio state to find what might be ducking output."""
import sounddevice as sd

print("Available output devices:")
default_out = sd.default.device[1]
for i, d in enumerate(sd.query_devices()):
    if d.get("max_output_channels", 0) > 0:
        marker = "  <- DEFAULT" if i == default_out else ""
        print(f"  [{i}] {d['name']}{marker}")
print()
print(f"Default output sample rate: {sd.query_devices(kind='output')['default_samplerate']:.0f} Hz")
print(f"Default input  device:      {sd.query_devices(kind='input')['name']}")
print(f"Default output device:      {sd.query_devices(kind='output')['name']}")
