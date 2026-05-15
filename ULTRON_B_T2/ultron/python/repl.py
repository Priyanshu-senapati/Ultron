"""Text REPL for ULTRON — talk to Module C via the bus.

Type a message, hit Enter, get ULTRON's reply. Same path the voice engine uses
(voice_transcript → Module C → llm_response), just without mic/STT/TTS.

Commands:
    :quit       exit
    :state      print Module C's current state summary (cognitive load, etc.)
    :clear      reset conversation history
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import websockets

APPDATA = Path(os.environ.get("APPDATA", Path.home()))
CONFIG  = APPDATA / "ULTRON" / "config.toml"


def load_bridge() -> tuple[str, str]:
    with open(CONFIG, "rb") as f:
        raw = tomllib.load(f)
    bridge = raw["bridge"]
    return f"ws://{bridge['bind']}/ws", bridge["token"]


async def receive_loop(ws, response_q: asyncio.Queue) -> None:
    """Background task — funnel llm_response events into the queue."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("op") == "event" and msg.get("kind") == "llm_response":
            await response_q.put(msg.get("payload") or {})


async def send_prompt(ws, text: str) -> None:
    await ws.send(json.dumps({
        "op": "publish",
        "kind": "voice_transcript",
        "payload": {
            "text": text,
            "duration_secs": 0.0,
            "confidence": 1.0,
            "activation": "repl",
            "ts_unix_ms": int(time.time() * 1000),
        }
    }))


async def main() -> None:
    url, token = load_bridge()
    print(f"connecting to {url}...")

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "role": "repl"}))
        welcome = json.loads(await ws.recv())
        if welcome.get("op") != "welcome":
            sys.exit(f"handshake failed: {welcome}")

        await ws.send(json.dumps({"op": "subscribe", "kinds": ["llm_response"]}))

        print("ULTRON is online. Type a message, or :quit to exit.\n")

        response_q: asyncio.Queue = asyncio.Queue()
        recv_task = asyncio.create_task(receive_loop(ws, response_q))

        try:
            while True:
                try:
                    line = await asyncio.to_thread(input, "you > ")
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if line.lower() in (":quit", ":q", ":exit"):
                    break

                # Drain any stale responses before sending a new prompt.
                while not response_q.empty():
                    response_q.get_nowait()

                await send_prompt(ws, line)

                # Wait for the reply (up to 60s — Ollama can be slow on cold cache).
                try:
                    payload = await asyncio.wait_for(response_q.get(), timeout=60)
                except asyncio.TimeoutError:
                    print("ultron > [timeout — no response in 60s]\n")
                    continue

                text = payload.get("text", "").strip()
                shard = payload.get("shard", "?")
                error = payload.get("error", False)
                tag = f"[{shard}]" + (" ERR" if error else "")
                print(f"ultron {tag}> {text}\n")
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
