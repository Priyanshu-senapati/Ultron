"""
Inject a fake voice_transcript and wait for llm_response.
Bypasses STT/microphone — tests Module C end-to-end.

Usage:
    python python/test_voice_pipeline.py
    python python/test_voice_pipeline.py "what time is it"
"""
import asyncio
import sys
import time
import tomllib
import os
from pathlib import Path
import websockets
import json

APPDATA = Path(os.environ.get("APPDATA", Path.home()))
CONFIG  = APPDATA / "ULTRON" / "config.toml"

def load_bridge():
    with open(CONFIG, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    return f"ws://{bridge['bind']}/ws", bridge["token"]

async def main(prompt: str) -> None:
    url, token = load_bridge()
    print(f"Connecting to {url} ...")

    async with websockets.connect(url) as ws:
        # Handshake (hello → welcome)
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "voice-pipeline-test"}))
        ack = json.loads(await ws.recv())
        if ack.get("op") != "welcome":
            sys.exit(f"Handshake failed: {ack}")
        print(f"Connected — server={ack.get('server_version')} session={ack.get('session_id')}")

        # Subscribe to llm_response
        await ws.send(json.dumps({"op": "subscribe", "kinds": ["llm_response"]}))

        # Inject voice_transcript
        payload = {
            "op": "publish",
            "kind": "voice_transcript",
            "payload": {
                "text": prompt,
                "duration_secs": 2.0,
                "confidence": 0.99,
                "activation": "test",
                "ts_unix_ms": int(time.time() * 1000),
            }
        }
        await ws.send(json.dumps(payload))
        print(f"Published voice_transcript: {prompt!r}")
        print("Waiting for llm_response (up to 30s)...")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                print("TIMEOUT — Module C did not respond within 30s.")
                print("Check that llm_service.py is running and Ollama is up.")
                return

            msg = json.loads(raw)
            if msg.get("op") == "event" and msg.get("kind") == "llm_response":
                p = msg.get("payload", {})
                text = p.get("text", "")
                error = p.get("error", False)
                shard = p.get("shard", "?")
                print(f"\n{'ERROR' if error else 'OK'} [{shard}]: {text}")
                return
            # ignore other events (heartbeats, etc.)

        print("No llm_response received.")

if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "what are you and what can you do"
    asyncio.run(main(prompt))
