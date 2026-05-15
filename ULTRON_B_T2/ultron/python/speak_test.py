"""Direct TTS + playback test. Verifies the audio-OUT path end to end.
Runs OUTSIDE the voice engine so it doesn't fight for the device."""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ultron_voice.config import load_voice_config
from ultron_voice.tts import TTSEngine
from ultron_voice.audio_io import AudioPlayer

PHRASE = (
    "Voice output is online, Priyanshu. "
    "If you can hear me, the playback path is working correctly."
)

async def main():
    cfg = load_voice_config()
    print(f"TTS backend  : {cfg.tts_backend}")
    print(f"Piper voice  : {cfg.piper_voice}")
    print(f"Edge voice   : {cfg.edge_tts_voice}")
    print(f"Output device: {cfg.audio_output_device or 'default'}")
    print(f"Synthesizing : {PHRASE!r}")

    tts = TTSEngine(
        backend=cfg.tts_backend,
        piper_voice=cfg.piper_voice,
        edge_tts_voice=cfg.edge_tts_voice,
        piper_model_path=cfg.piper_model_path,
        kokoro_model_path=cfg.kokoro_model_path,
        kokoro_voices_path=cfg.kokoro_voices_path,
        kokoro_voice=cfg.kokoro_voice,
        kokoro_speed=cfg.kokoro_speed,
        kokoro_lang=cfg.kokoro_lang,
    )
    audio = await tts.synthesize(PHRASE)
    if not audio:
        print("ERROR: TTS returned no audio")
        return
    fmt = TTSEngine.format_of(audio)
    print(f"Got {len(audio)} bytes of {fmt} audio. Playing...")

    player = AudioPlayer(
        sample_rate=AudioPlayer.PIPER_RATE,
        device=cfg.audio_output_device,
    )
    await player.play(audio, fmt=fmt)
    print("Playback complete.")

asyncio.run(main())
