"""Live smoke for Phase 2 — extractor + reflector via the live Ollama
backend.

Slow (~30-60s) because each LLM call takes 3-10s on CPU. Skip on CI.

Sequence:
  1. Index ~6 user/assistant turns about Rex the dog + ULTRON.
  2. Fire recall_extract_request, wait for facts_extracted.
  3. Fire recall_reflect_request (kind: session), wait for reflection_written.
  4. Search the corpus with "what's my dog's name" and assert Rex
     surfaces via either a turn hit or a fact hit.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CORPUS = [
    ("user", "my dog's name is Rex, he's a five-year-old golden retriever"),
    ("assistant", "Rex the golden — got it, sir."),
    ("user", "I'm building ULTRON, a multi-process cognitive twin"),
    ("assistant", "ULTRON: voice engine local, LLM via Ollama, modules over WS."),
    ("user", "I prefer dark mode in every IDE I use"),
    ("assistant", "Noted — dark themes everywhere."),
    ("user", "I live in India and the timezone is IST"),
    ("assistant", "IST it is, sir."),
]


async def main() -> int:
    cfg = Path(os.environ["APPDATA"]) / "ULTRON" / "config.toml"
    with open(cfg, "rb") as f:
        raw = tomllib.load(f)
    url = f"ws://{raw['bridge']['bind']}/ws"
    token = raw["bridge"]["token"]

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token,
                                  "role": "smoke-recall-phase2"}))
        await ws.recv()
        await ws.send(json.dumps({
            "op": "subscribe",
            "kinds": ["recall_indexed", "facts_extracted",
                      "reflection_written", "recall_query_result"],
        }))
        await asyncio.sleep(0.4)

        seen_indexed: list[dict] = []
        seen_facts: list[dict] = []
        seen_refl: list[dict] = []
        seen_q: list[dict] = []

        async def reader() -> None:
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                if m.get("op") != "event":
                    continue
                k = m.get("kind"); p = m.get("payload") or {}
                if k == "recall_indexed":
                    seen_indexed.append(p)
                elif k == "facts_extracted":
                    seen_facts.append(p)
                    print(f"  facts_extracted: parsed={p.get('facts_parsed')}"
                          f" inserted={p.get('facts_inserted')}"
                          f" duplicate={p.get('facts_duplicate')}")
                elif k == "reflection_written":
                    seen_refl.append(p)
                    if p.get("reflection_id"):
                        print(f"  reflection_written: id={p['reflection_id']}"
                              f" chars={p.get('summary_chars')}")
                        print(f"    preview: {p.get('summary_preview', '')[:200]}")
                    else:
                        print(f"  reflection_written: {p}")
                elif k == "recall_query_result":
                    seen_q.append(p)

        rt = asyncio.create_task(reader())

        # 1) Index corpus.
        for role, text in CORPUS:
            await ws.send(json.dumps({"op": "publish",
                                      "kind": "recall_index_request",
                                      "payload": {"role": role,
                                                  "content": text}}))
            await asyncio.sleep(0.05)
        # Allow time for embedder warmup + flush.
        await asyncio.sleep(12.0)

        # 2) Trigger fact extraction.
        print("  triggering extract...")
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_extract_request",
                                  "payload": {}}))
        # Wait up to 60s for Ollama.
        for _ in range(60):
            if seen_facts:
                break
            await asyncio.sleep(1.0)

        # 3) Trigger session reflection.
        print("  triggering reflect...")
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_reflect_request",
                                  "payload": {"period": "session"}}))
        for _ in range(60):
            if seen_refl:
                break
            await asyncio.sleep(1.0)

        # 4) Query for Rex.
        print("  querying for Rex...")
        await ws.send(json.dumps({"op": "publish",
                                  "kind": "recall_query_request",
                                  "payload": {"kind": "search",
                                              "query": "what is my dog's name",
                                              "top_k": 5,
                                              "include_reflections": True,
                                              "include_facts": True}}))
        await asyncio.sleep(4.0)
        rt.cancel()

    ok_facts = bool(seen_facts) and not (seen_facts[0].get("error")
                                          or seen_facts[0].get("skipped"))
    ok_refl = bool(seen_refl) and bool(
        (seen_refl[0] or {}).get("reflection_id")
    )
    search_results = [q for q in seen_q if q.get("kind") == "search"]
    rex_found = False
    if search_results:
        bundle = search_results[-1].get("bundle") or {}
        for h in (bundle.get("turn_hits") or []):
            if "Rex" in (h.get("content") or ""):
                rex_found = True
                break
        if not rex_found:
            for h in (bundle.get("fact_hits") or []):
                if "Rex" in str(h):
                    rex_found = True
                    break

    print("---")
    print(f"  indexed events    : {len(seen_indexed)}")
    print(f"  facts_extracted   : {len(seen_facts)} (ok={ok_facts})")
    print(f"  reflection_written: {len(seen_refl)} (ok={ok_refl})")
    print(f"  search hits Rex   : {rex_found}")

    if ok_facts and ok_refl and rex_found:
        print("PASS")
        return 0
    print("FAIL")
    return 1


sys.exit(asyncio.run(main()))
