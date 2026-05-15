"""Live monitor: speak 'Hey Ultron, ...' and watch the pipeline fire.

Subscribes to voice_transcript, voice_state_changed, llm_response and
prints each one as it crosses the bus. Run for 60s, then exit.
"""
import asyncio, json, os, time, tomllib
from pathlib import Path
import websockets

cfg = tomllib.load(open(Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml", "rb"))
url = f"ws://{cfg['bridge']['bind']}/ws"
tok = cfg["bridge"]["token"]

async def main():
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"op":"hello","token":tok,"role":"wake-watch"}))
        await ws.recv()
        await ws.send(json.dumps({"op":"subscribe","kinds":[
            "voice_transcript","voice_state_changed","llm_response"
        ]}))
        print("Speak 'Hey Ultron, what time is it' into your mic.")
        print("Listening for 60 seconds...")
        print()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("op") != "event":
                continue
            k = m.get("kind"); p = m.get("payload", {})
            ts = time.strftime("%H:%M:%S")
            if k == "voice_transcript":
                print(f"[{ts}] voice_transcript  activation={p.get('activation')}  text={p.get('text')!r}")
            elif k == "voice_state_changed":
                print(f"[{ts}] state             {p.get('from')} -> {p.get('to')}   ({p.get('reason','')})")
            elif k == "llm_response":
                txt = p.get("text", "")
                print(f"[{ts}] llm_response      {txt!r}")
        print("\n(done)")

asyncio.run(main())
