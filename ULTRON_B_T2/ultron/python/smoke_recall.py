"""Live smoke for the Recall service.

End-to-end: indexes a known set of synthetic turns over the bus, then
issues a recall query whose embedding has no exact match in the corpus
but is semantically close to one specific turn. Asserts that turn comes
back in the top-3, and that the formatted prompt block contains it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CORPUS = [
    ("user", "I prefer dark mode in every IDE I use"),
    ("assistant", "Got it sir, I'll remember that you prefer dark themes."),
    ("user", "my dog's name is Rex and he's a golden retriever"),
    ("assistant", "Rex the golden — noted."),
    ("user", "I'm building ULTRON, a multi-process cognitive twin"),
    ("assistant", "ULTRON's voice engine runs locally; the LLM is Ollama-first."),
    ("user", "what's the capital of France"),
    ("assistant", "Paris, sir."),
]


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-recall"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["recall_indexed", "recall_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_indexed: list[dict] = []
        seen_q: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "recall_indexed":
                    seen_indexed.append(p)
                    print(f"  indexed: +{p.get('count')} (conv={p.get('conv_id')})")
                elif k == "recall_query_result":
                    seen_q.append(p)
                    if "bundle" in p:
                        b = p["bundle"]
                        c = b.get("counts") or {}
                        print(f"  query: turns={c.get('turns')}"
                              f" reflections={c.get('reflections')}"
                              f" facts={c.get('facts')}")
                    elif "counts" in p:
                        print(f"  counts: {p['counts']}")

        rt = asyncio.create_task(reader())

        # Feed the corpus via direct index requests so the smoke is
        # deterministic regardless of who else is publishing turns.
        for role, text in CORPUS:
            await ws.send(json.dumps({"op": "publish",
                                      "kind": "recall_index_request",
                                      "payload": {"role": role,
                                                  "content": text}}))
            await asyncio.sleep(0.05)
        # Allow time for the embedder to warm up + flush.
        await asyncio.sleep(15.0)

        # Force a flush to be safe.
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_query_request",
                                  "payload": {"kind": "flush"}}))
        await asyncio.sleep(2.0)

        # Issue a semantic query — should find Rex.
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_query_request",
                                  "payload": {"kind": "search",
                                              "query": "what is the name of my pet",
                                              "top_k": 3}}))
        await asyncio.sleep(4.0)

        # Also ask for counts.
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_query_request",
                                  "payload": {"kind": "counts"}}))
        await asyncio.sleep(2.0)
        rt.cancel()

    # Validate.
    search_results = [q for q in seen_q if q.get("kind") == "search"]
    if not search_results:
        print(f"FAIL  no search result received  events={len(seen_q)}")
        return 1
    bundle = (search_results[-1].get("bundle") or {})
    turn_hits = bundle.get("turn_hits") or []
    print(f"--- top {len(turn_hits)} hits for 'what is the name of my pet' ---")
    for h in turn_hits:
        c = (h.get("content") or "")[:100]
        print(f"  score={h.get('score'):.3f}  role={h.get('role')}  '{c}'")
    rex_hit = any("Rex" in (h.get("content") or "") for h in turn_hits[:3])
    prompt_block = (search_results[-1].get("prompt_block") or "")
    if rex_hit and "Rex" in prompt_block:
        print(f"PASS  Rex turn surfaced, prompt block built ({len(prompt_block)} chars)")
        return 0
    print(f"FAIL  rex_hit={rex_hit}  prompt_has_rex={'Rex' in prompt_block}")
    print("--- prompt block ---")
    print(prompt_block[:600])
    return 1


sys.exit(asyncio.run(main()))
