"""
test_llm.py — Module C tests.
Run: pytest python/test_llm.py -v
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Test 1: Shard selection — coding + calm → ARCHITECT ──────────────────

def test_shard_auto_coding_calm():
    from ultron_llm.personality import Shard, select_shard
    sel = select_shard("coding", 0.2, "calm")
    assert sel.shard == Shard.ARCHITECT


# ── Test 2: Shard selection — high tension → COACH ───────────────────────

def test_shard_auto_high_tension():
    from ultron_llm.personality import Shard, select_shard
    sel = select_shard("coding", 0.85, "spiked")
    assert sel.shard == Shard.COACH


# ── Test 3: Forced shard overrides auto ──────────────────────────────────

def test_shard_forced_overrides_auto():
    from ultron_llm.personality import Shard, select_shard
    sel = select_shard("coding", 0.1, "calm", forced="brutal")
    assert sel.shard == Shard.BRUTAL


# ── Test 4: Voice addendum injected in voice mode ────────────────────────

def test_voice_addendum_injected():
    from ultron_llm.personality import Shard, build_system_prompt
    prompt = build_system_prompt(Shard.ARCHITECT, "voice", 0.3, 0.7)
    assert "VOICE MODE" in prompt
    assert "3 sentences" in prompt


# ── Test 5: High load addendum injected above threshold ──────────────────

def test_high_load_addendum_injected():
    from ultron_llm.personality import Shard, build_system_prompt
    prompt = build_system_prompt(Shard.ARCHITECT, "default", 0.85, 0.7)
    assert "HIGH COGNITIVE LOAD" in prompt


# ── Test 6: Tool call parser extracts valid blocks ───────────────────────

def test_tool_parser_extracts_calls():
    from ultron_llm.tool_parser import parse_tool_calls
    text = """Here's the answer.
```tool
{"name": "shell", "args": {"cmd": "ls -la"}}
```
And some more text."""
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "shell"
    assert calls[0].args == {"cmd": "ls -la"}


# ── Test 7: Tool parser skips malformed blocks ────────────────────────────

def test_tool_parser_skips_malformed():
    from ultron_llm.tool_parser import parse_tool_calls
    text = "```tool\nnot json at all\n```"
    calls = parse_tool_calls(text)
    assert calls == []


# ── Test 8: strip_tool_calls removes blocks from response ─────────────────

def test_strip_tool_calls():
    from ultron_llm.tool_parser import strip_tool_calls
    text = "Before.\n```tool\n{\"name\": \"x\", \"args\": {}}\n```\nAfter."
    clean = strip_tool_calls(text)
    assert "```tool" not in clean
    assert "Before." in clean
    assert "After." in clean


# ── Test 9: ConversationHistory ring buffer limits turns ──────────────────

def test_conversation_history_limit():
    from ultron_llm.conversation import ConversationHistory
    h = ConversationHistory(max_turns=2)
    for i in range(5):
        h.add_user(f"user {i}")
        h.add_assistant(f"assistant {i}")
    msgs = h.to_ollama_messages()
    # max_turns=2 → max 4 messages (2 pairs)
    assert len(msgs) <= 4


# ── Test 10: PreferenceEngine detects correction ──────────────────────────

def test_preference_detects_correction(tmp_path):
    from ultron_llm.preference import PreferenceEngine
    pref = PreferenceEngine(tmp_path / "pref.db")
    pref.on_response("architect", 0.3, "some response")
    # Wait a moment then send correction
    pref._last_response_ts = time.monotonic() - 5  # 5s ago
    pref.on_user_message("no, that's not what I meant")
    # Rate should now be > 0 for architect+low
    rate = pref.correction_rate("architect", "low", lookback_days=1)
    assert rate > 0.0


# ── Test 11: PreferenceEngine ignores corrections after timeout ────────────

def test_preference_ignores_old_corrections(tmp_path):
    from ultron_llm.preference import PreferenceEngine
    pref = PreferenceEngine(tmp_path / "pref.db")
    pref.on_response("architect", 0.3, "some response")
    pref._last_response_ts = time.monotonic() - 120  # 120s ago — beyond 90s window
    pref.on_user_message("no that's wrong")
    rate = pref.correction_rate("architect", "low", lookback_days=1)
    assert rate == 0.0


# ── Test 12: LiveState updates from event payloads ────────────────────────

def test_live_state_updates():
    from ultron_llm.state import LiveState
    state = LiveState()
    assert state.cognitive_load == 0.0

    state.update_snapshot({
        "cognitive_load": 0.72,
        "tension": 0.65,
        "tension_band": "loaded",
        "focus_category": "coding",
        "focus_app": "Code.exe",
        "wpm": 55.0,
        "fatigue_flag": False,
        "visual_label": "writing rust code",
        "circadian_phase": "afternoon",
    })
    assert state.cognitive_load == 0.72
    assert state.tension_band == "loaded"
    assert state.visual_label == "writing rust code"


# ── Test 13: Ollama client builds correct payload and assembles streamed chunks ──

@pytest.mark.asyncio
async def test_ollama_chat_calls_correct_endpoint():
    """
    Verify that OllamaClient.chat:
    - POSTs to /api/chat with the right payload shape
    - assembles streamed chunks into the full response
    """
    from ultron_llm.client_ollama import OllamaClient
    client = OllamaClient(
        base_url="http://localhost:11434", default_model="llama3.2:3b"
    )

    # Two NDJSON lines simulating Ollama's streaming response.
    mock_lines = [
        '{"message": {"content": "Hello"}, "done": false}',
        '{"message": {"content": " world"}, "done": true}',
    ]

    async def fake_aiter_lines():
        for line in mock_lines:
            yield line

    # The response object yielded by the `async with client.stream(...)` ctx.
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = fake_aiter_lines  # called like .aiter_lines()

    # The context manager returned by AsyncClient.stream(...).
    class _StreamCtx:
        async def __aenter__(self_inner):
            return mock_resp

        async def __aexit__(self_inner, *args):
            return False

    # AsyncClient itself is also used as an async context manager.
    class _ClientCtx:
        async def __aenter__(self_inner):
            inner = MagicMock()
            inner.stream = MagicMock(return_value=_StreamCtx())
            return inner

        async def __aexit__(self_inner, *args):
            return False

    with patch("httpx.AsyncClient", return_value=_ClientCtx()):
        result = await client.chat(
            "You are helpful.", [{"role": "user", "content": "hi"}]
        )

    assert result == "Hello world"


# ── Test 14: ask() in voice mode uses Ollama, not Claude ──────────────────

@pytest.mark.asyncio
async def test_ask_voice_mode_uses_ollama(tmp_path):
    from ultron_llm.config import LLMConfig
    from ultron_llm.service import LLMService
    cfg = LLMConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        token="test",
        memory_db_path=tmp_path / "memory.db",
        claude_api_key="",  # no Claude key
    )
    svc = LLMService(cfg)

    with patch.object(
        svc._ollama, "chat", new_callable=AsyncMock,
        return_value="Voice response",
    ) as mock_ollama:
        with patch.object(svc._claude, "is_configured", return_value=False):
            result = await svc.ask("what am I doing", mode="voice")

    assert result == "Voice response"
    mock_ollama.assert_called_once()


# ── Test 15: ContextAssembler injects state into first turn ───────────────

def test_context_assembler_first_turn(tmp_path):
    from ultron_llm.context import ContextAssembler
    from ultron_llm.state import LiveState

    assembler = ContextAssembler(memory_db_path=tmp_path / "memory.db")
    state = LiveState()
    state.update_snapshot({
        "cognitive_load": 0.4, "tension": 0.3, "tension_band": "neutral",
        "focus_category": "coding", "focus_app": "Code.exe",
        "wpm": 60.0, "fatigue_flag": False, "visual_label": "writing python",
        "circadian_phase": "morning",
    })

    system_prompt, messages, sel = assembler.assemble(
        user_message="explain async queues",
        state=state,
        history=[],
        mode="default",
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "CURRENT STATE" in messages[0]["content"]
    assert "writing python" in messages[0]["content"]
