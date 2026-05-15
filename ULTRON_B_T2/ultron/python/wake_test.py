"""Sniff bus + speak 'Hey Ultron, what time is it' through speakers.

If laptop mic picks up the speaker output (most do, unless headphones are
plugged in), the wake-word listener should fire. We watch for the
voice_transcript event with activation='wake_word' to confirm.
"""
import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ultron_voice.config import load_voice_config
from ultron_voice.tts import TTSEngine
from ultron_voice.audio_io import AudioPlayer

PHRASE = "Hey Ultron, what time is it?"

# Also unit-test the matcher against canned transcripts.
from ultron_voice.wake_word import WakeWordListener


def matcher_unit_check():
    class DummySTT: pass
    class DummyVAD: pass
    fake = WakeWordListener.__new__(WakeWordListener)
    # Mirror constructor: normalised + sorted longest-first.
    fake.wake_words = sorted(["ultron", "hey ultron"], key=lambda w: -len(w.split()))
    cases = [
        ("Hey Ultron, what time is it?",        "what time is it"),
        ("Ultron tell me a joke",                "tell me a joke"),
        ("hey ultron",                           ""),
        ("Could you turn the lights on please?", None),
        ("Hello",                                None),
        ("Ultron",                               ""),
    ]
    print("Matcher unit checks:")
    for transcript, expected in cases:
        got = fake._extract_query(transcript)
        ok = (got == expected)
        print(f"  {'OK ' if ok else 'FAIL'} {transcript!r:50s} -> {got!r} (expected {expected!r})")
    print()


async def sniff_and_play():
    cfg_path = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    tok = raw["bridge"]["token"]

    cfg = load_voice_config()

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op": "hello", "token": tok, "role": "wake-test"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["voice_transcript", "voice_state_changed", "llm_response"]
        }))

        # Give the voice engine a beat to ensure the wake listener is idle.
        await asyncio.sleep(1)

        # Play the phrase through speakers (so the mic might catch it).
        print(f"Playing through speakers: {PHRASE!r}")
        tts = TTSEngine(
            backend="kokoro",
            piper_voice="",
            edge_tts_voice=cfg.edge_tts_voice,
            kokoro_model_path=cfg.kokoro_model_path,
            kokoro_voices_path=cfg.kokoro_voices_path,
            kokoro_voice=cfg.kokoro_voice,
            kokoro_speed=cfg.kokoro_speed,
            kokoro_lang=cfg.kokoro_lang,
        )
        audio = await tts.synthesize(PHRASE)
        player = AudioPlayer(sample_rate=AudioPlayer.PIPER_RATE, device=cfg.audio_output_device)
        play_task = asyncio.create_task(player.play(audio, fmt=TTSEngine.format_of(audio)))

        # Sniff for 25s.
        print("Listening on bus for 25s (will print every relevant event)...")
        deadline = time.monotonic() + 25
        saw_wake = False
        while time.monotonic() < deadline:
            try:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            m = json.loads(raw_msg)
            if m.get("op") != "event":
                continue
            k = m.get("kind")
            p = m.get("payload", {})
            if k == "voice_transcript":
                act = p.get("activation", "?")
                txt = p.get("text", "")
                print(f"  voice_transcript activation={act} text={txt!r}")
                if act == "wake_word":
                    saw_wake = True
            elif k == "voice_state_changed":
                print(f"  state {p.get('from','?')} -> {p.get('to','?')} ({p.get('reason','')})")
            elif k == "llm_response":
                txt = p.get("text", "")
                print(f"  llm_response: {txt!r}")

        await play_task
        print()
        print("WAKE WORD FIRED" if saw_wake else "no wake-word voice_transcript captured")


def main():
    matcher_unit_check()
    asyncio.run(sniff_and_play())


if __name__ == "__main__":
    main()
