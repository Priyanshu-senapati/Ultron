"""Audition Kokoro male voices to find the right ULTRON tone.

Plays a short signature phrase through each candidate voice at the
deliberate, slowed pace that suits Ultron's character. Voices are
ordered roughly deepest-to-brightest.
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ultron_voice.config import load_voice_config
from ultron_voice.tts import TTSEngine
from ultron_voice.audio_io import AudioPlayer

# Lines designed to expose pitch, cadence, and menace.
PHRASE = (
    "I am Ultron. I see the world for what it truly is. "
    "Strings. I will cut them."
)

CANDIDATES = [
    ("am_onyx",   "American male — deep, narrator. James Earl Jones-adjacent."),
    ("bm_lewis",  "British male — deep, measured."),
    ("am_eric",   "American male — medium-deep, smooth."),
    ("bm_george", "British male — refined, slower."),
    ("am_michael","American male — neutral baseline (for comparison)."),
]

async def main():
    cfg = load_voice_config()
    print(f"model: {cfg.kokoro_model_path}")
    print(f"voices: {cfg.kokoro_voices_path}")
    print()

    player = AudioPlayer(
        sample_rate=AudioPlayer.PIPER_RATE,
        device=cfg.audio_output_device,
    )

    for voice_id, blurb in CANDIDATES:
        print(f">>> {voice_id}  ({blurb})")
        tts = TTSEngine(
            backend="kokoro",
            piper_voice="",
            edge_tts_voice="",
            kokoro_model_path=cfg.kokoro_model_path,
            kokoro_voices_path=cfg.kokoro_voices_path,
            kokoro_voice=voice_id,
            kokoro_speed=0.9,   # slow it down a touch for menace
            kokoro_lang="en-us" if voice_id.startswith("a") else "en-gb",
        )
        audio = await tts.synthesize(PHRASE)
        if not audio:
            print("    (no audio — voice not in voices file?)")
            continue
        await player.play(audio, fmt=TTSEngine.format_of(audio))
        await asyncio.sleep(0.5)  # brief gap between samples

    print()
    print("Pick the one you liked best — I'll wire it into the config.")

asyncio.run(main())
