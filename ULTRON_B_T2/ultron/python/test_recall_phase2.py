"""Phase 2 unit tests for ultron_recall — extractor + reflector.

We don't hit a real Ollama in unit tests. Each test installs a fake
OllamaClient by monkey-patching the ``chat`` method on the instance
the extractor / reflector built.
"""
from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from ultron_recall.extractor import FactExtractor, _parse_facts
from ultron_recall.reflector import Reflector
from ultron_recall.store import EMBEDDING_DIM, RecallStore


def _unit_vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def _seed_turns(store: RecallStore, turns: list[tuple[str, str]]) -> list[int]:
    ids: list[int] = []
    for i, (role, content) in enumerate(turns):
        ids.append(store.insert_turn(
            ts=1000.0 + i, role=role, content=content,
            conv_id="c1", embedding=_unit_vec(i),
        ))
    return ids


# ── _parse_facts ───────────────────────────────────────────────────────


def test_parse_facts_clean_json():
    text = ('[{"subject":"user","predicate":"works_on","object":"ULTRON"},'
            '{"subject":"user","predicate":"has","object":"dog Rex"}]')
    out = _parse_facts(text)
    assert len(out) == 2
    assert out[0]["object"] == "ULTRON"


def test_parse_facts_handles_code_fence():
    text = '```json\n[{"subject":"a","predicate":"b","object":"c"}]\n```'
    out = _parse_facts(text)
    assert out == [{"subject": "a", "predicate": "b", "object": "c"}]


def test_parse_facts_handles_prose_wrapper():
    text = ("Sure, here are the facts:\n"
            "[{\"subject\":\"user\",\"predicate\":\"is\",\"object\":\"a dev\"}]\n"
            "Hope that helps.")
    out = _parse_facts(text)
    assert out == [{"subject": "user", "predicate": "is", "object": "a dev"}]


def test_parse_facts_rejects_malformed():
    assert _parse_facts("nope") == []
    assert _parse_facts("[{not json}]") == []
    # Missing required keys → filtered out.
    assert _parse_facts('[{"subject":"a","object":"b"}]') == []


def test_parse_facts_caps_field_lengths():
    huge_obj = "X" * 500
    text = f'[{{"subject":"a","predicate":"b","object":"{huge_obj}"}}]'
    assert _parse_facts(text) == []


# ── FactExtractor ──────────────────────────────────────────────────────


def test_extractor_skips_when_not_enough_new(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [("user", "hello"), ("assistant", "hi")])
    ex = FactExtractor(store, ollama_url="http://nope",
                       ollama_model="x", max_turns_per_pass=10)
    out = loop.run_until_complete(ex.extract_pass(min_new_turns=4))
    assert out.get("skipped") == "not_enough_new_turns"


def test_extractor_inserts_parsed_facts(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [
        ("user", "my dog is Rex"),
        ("assistant", "got it"),
        ("user", "I work on ULTRON"),
        ("assistant", "noted"),
        ("user", "I prefer dark mode"),
    ])
    ex = FactExtractor(store, ollama_url="http://nope", ollama_model="x")

    async def fake_chat(**kwargs):
        return ('[{"subject":"user\'s dog","predicate":"is named",'
                '"object":"Rex"},'
                '{"subject":"user","predicate":"works on","object":"ULTRON"},'
                '{"subject":"user","predicate":"prefers","object":"dark mode"}]')

    ex._client.chat = fake_chat   # type: ignore[assignment]
    out = loop.run_until_complete(ex.extract_pass(min_new_turns=3))
    assert out["facts_parsed"] == 3
    assert out["facts_inserted"] == 3
    assert out["high_water_turn_id"] > 0
    facts = store.all_facts()
    assert {f["object"] for f in facts} == {"Rex", "ULTRON", "dark mode"}


def test_extractor_advances_high_water_even_on_empty(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    ids = _seed_turns(store, [
        ("user", "what's the weather"),
        ("assistant", "raining"),
        ("user", "cool"),
        ("assistant", "yep"),
    ])
    ex = FactExtractor(store, ollama_url="http://nope", ollama_model="x")

    async def fake_chat(**kwargs):
        return "[]"

    ex._client.chat = fake_chat   # type: ignore[assignment]
    out = loop.run_until_complete(ex.extract_pass(min_new_turns=3))
    assert out["facts_parsed"] == 0
    assert out["high_water_turn_id"] == ids[-1]
    # Re-running should now skip.
    out2 = loop.run_until_complete(ex.extract_pass(min_new_turns=3))
    assert out2.get("skipped") == "not_enough_new_turns"


def test_extractor_dedupes_via_facts_uniqueness(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [
        ("user", "my dog is Rex"),
        ("assistant", "got it"),
        ("user", "my dog is still Rex"),
        ("assistant", "still got it"),
    ])
    ex = FactExtractor(store, ollama_url="http://nope", ollama_model="x")

    async def fake_chat(**kwargs):
        return ('[{"subject":"user\'s dog","predicate":"is named","object":"Rex"},'
                '{"subject":"user\'s dog","predicate":"is named","object":"Rex"}]')

    ex._client.chat = fake_chat   # type: ignore[assignment]
    out = loop.run_until_complete(ex.extract_pass(min_new_turns=3))
    assert out["facts_inserted"] == 1
    assert out["facts_duplicate"] == 1


# ── Reflector ──────────────────────────────────────────────────────────


class _StubEmbedder:
    def encode_one(self, text: str) -> np.ndarray:
        return _unit_vec(hash(text) & 0xFFFF)


def test_reflector_skips_too_few_turns(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [("user", "hi"), ("assistant", "hello")])
    refl = Reflector(store, ollama_url="http://nope", ollama_model="x",
                     embedder=_StubEmbedder(), max_chars=1000)
    out = loop.run_until_complete(refl.reflect_session(conv_id="c1"))
    assert out.get("skipped") == "too_few_turns"


def test_reflector_writes_summary(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [
        ("user", "let's plan the recall service"),
        ("assistant", "OK — Phase 1, then extractor, then reflector"),
        ("user", "great, also wire it into Module C"),
        ("assistant", "yep, auto-inject on past-reference heuristic"),
        ("user", "ship it"),
        ("assistant", "shipping"),
    ])
    refl = Reflector(store, ollama_url="http://nope", ollama_model="x",
                     embedder=_StubEmbedder(), max_chars=1000)

    async def fake_chat(**kwargs):
        return ("The user and assistant planned the recall service, agreed "
                "to wire it into Module C with a past-reference heuristic, "
                "and shipped Phase 1 / Phase 2 / Phase 3 in one session.")

    refl._client.chat = fake_chat   # type: ignore[assignment]
    out = loop.run_until_complete(refl.reflect_session(conv_id="c1"))
    assert out.get("reflection_id", 0) > 0
    assert "recall service" in out["summary_preview"]
    counts = store.counts()
    assert counts["reflections"] == 1


def test_reflector_clamps_long_summary(tmp_path, loop):
    store = RecallStore(tmp_path / "r.db")
    _seed_turns(store, [
        ("user", "a"), ("assistant", "b"),
        ("user", "c"), ("assistant", "d"),
        ("user", "e"), ("assistant", "f"),
    ])
    refl = Reflector(store, ollama_url="http://nope", ollama_model="x",
                     embedder=_StubEmbedder(), max_chars=50)

    async def fake_chat(**kwargs):
        return "x" * 500

    refl._client.chat = fake_chat   # type: ignore[assignment]
    out = loop.run_until_complete(refl.reflect_session(conv_id="c1"))
    # Stored summary length should be clamped at max_chars (+ellipsis).
    assert out["summary_chars"] <= 51
