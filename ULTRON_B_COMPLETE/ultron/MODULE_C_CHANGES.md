# Module C Changes Required Before B Activates

Module B (Voice Engine) integrates with Module C (LLM Client) exclusively via the
WS bus. C must be updated to:

1. Include the response text in `llm_response` payloads (B needs it for TTS)
2. Subscribe to `voice_transcript` events and route them into `ask()`

These changes touch a single file: `ultron_llm/service.py` (in C's repo, **not** in
this Rust workspace). Apply them on your box before running the Voice Engine.

Once applied, run C's test suite to confirm nothing regressed, then start
`voice_engine.py` (see `python/voice_engine.py` in this repo) to bring B online.

---

## Change 1 — Add `text` to `llm_response` payload

### File: `ultron_llm/service.py`
### Function: `_publish_response` (~line 130)

**Before:**

```python
async def _publish_response(self, response: str, shard: str) -> None:
    if self._bridge:
        await self._bridge.publish("llm_response", {
            "shard": shard,
            "cognitive_load": self._state.cognitive_load,
            "response_len": len(response),
            "ts_unix_ms": int(time.time() * 1000),
        })
```

**After:**

```python
async def _publish_response(self, response: str, shard: str) -> None:
    if self._bridge:
        await self._bridge.publish("llm_response", {
            "text": response,                         # ← ADD THIS
            "shard": shard,
            "cognitive_load": self._state.cognitive_load,
            "response_len": len(response),
            "ts_unix_ms": int(time.time() * 1000),
        })
```

That's the entire change for this function — one new key in the dict.

---

## Change 2 — Subscribe to and handle `voice_transcript`

### File: `ultron_llm/service.py`
### Three edits in the same file:

### 2a. In `_handle_event`, add an arm for `voice_transcript`:

```python
elif kind == "voice_transcript":
    text = payload.get("text", "").strip()
    if not text:
        return
    # ask() is synchronous to the event loop — schedule as a task
    # so the WS pump isn't blocked during LLM inference.
    asyncio.ensure_future(self._handle_voice_request(text))
```

### 2b. Add the new handler method anywhere in the `LLMService` class:

```python
async def _handle_voice_request(self, text: str) -> None:
    """Called when B publishes a voice_transcript event."""
    try:
        response = await self.ask(text, mode="voice")
        # _publish_response is called inside ask() → _ask_ollama/_ask_claude.
        # Nothing else needed here — response is already on the bus.
        logger.info("voice request handled: %d chars → %d chars",
                    len(text), len(response))
    except Exception as exc:
        logger.error("voice request failed: %s", exc)
        # Publish an error response so B doesn't wait forever.
        if self._bridge:
            await self._bridge.publish("llm_response", {
                "text": "Sorry, I ran into an error. Please try again.",
                "shard": "default",
                "cognitive_load": 0.0,
                "response_len": 0,
                "ts_unix_ms": int(time.time() * 1000),
                "error": True,
            })
```

### 2c. In `run()`, add `"voice_transcript"` to the bridge's `subscribe_to` list:

```python
subscribe_to=[
    "insight_snapshot",
    "productivity_prior_update",
    "patterns_update",
    "voice_transcript",   # ← ADD THIS
],
```

---

## Verification (do after applying)

1. Run C's existing test suite — no tests should fail. The changes are purely
   additive (one new dict key, one new event handler, one new subscription).
2. Start C with `python python/llm_service.py` (or however you currently start it).
3. Manually publish a test `voice_transcript` event via the WS bridge (or use
   `bridge_test.py` with a small helper). Confirm:
   - C logs "voice request handled: N chars → M chars"
   - An `llm_response` event appears on the bus with a non-empty `text` field
4. Now you can start Module B (`python python/voice_engine.py`).

If something goes wrong, Module B's graceful-degradation rules cover it: the
state machine times out after `llm_response_timeout_secs` (default 30s) and
auto-recovers to `IDLE`. So a broken C doesn't break B's process.

---

## Why these changes live in a separate doc

Module C's source isn't in this Rust workspace — it's a separate repo on your
box. Rather than imagine its current state and write speculative diffs, this
document records the exact edits so future-you (and any other reader of this
repo) has the audit trail of what coupling B introduced to C.

When you've applied them, you can delete this file or mark it `[applied]` in
the title. Until then it's the contract Module B depends on.
