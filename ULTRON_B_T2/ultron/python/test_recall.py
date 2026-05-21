"""Unit tests for ultron_recall (Phase 1).

These tests bypass sentence-transformers by feeding pre-built unit
vectors through ``RecallStore`` and ``RecallRetriever`` directly. The
real Embedder is exercised in the live smoke instead — keeps unit
tests fast (no model download) and deterministic.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from ultron_recall import (
    RecallBundle,
    RecallConfig,
    RecallRetriever,
    RecallStore,
    StoredTurn,
)
from ultron_recall.store import EMBEDDING_DIM


def _cfg(tmp_path, **overrides) -> RecallConfig:
    defaults = dict(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test-token",
        db_path=tmp_path / "recall.db",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        min_content_chars=8,
        max_indexed_chars=1500,
        embed_batch_size=8,
        embed_flush_interval_secs=5.0,
        default_top_k=6,
        max_top_k=30,
        min_score=0.30,
        neighbour_window=1,
        enable_reflections=False,
        reflection_chars=1200,
        enable_fact_extraction=False,
    )
    defaults.update(overrides)
    return RecallConfig(**defaults)


def _unit_vec(seed: int) -> np.ndarray:
    """Deterministic unit-norm vector keyed by seed."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


# ── Store: insert + counts ─────────────────────────────────────────────


def test_store_insert_and_counts(tmp_path):
    store = RecallStore(_cfg(tmp_path).db_path)
    tid = store.insert_turn(ts=1000.0, role="user",
                            content="my dog's name is Rex",
                            conv_id="conv-1", embedding=_unit_vec(1))
    assert tid > 0
    counts = store.counts()
    assert counts["turns"] == 1
    assert counts["reflections"] == 0
    assert counts["facts"] == 0


def test_store_bulk_insert(tmp_path):
    store = RecallStore(_cfg(tmp_path).db_path)
    rows = [
        {"ts": 1000.0 + i, "role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i}", "conv_id": "conv-1",
         "embedding": _unit_vec(i)}
        for i in range(5)
    ]
    ids = store.insert_turns_bulk(rows)
    assert len(ids) == 5
    assert all(i > 0 for i in ids)
    assert store.counts()["turns"] == 5


# ── Store: search ──────────────────────────────────────────────────────


def test_search_returns_closest_first(tmp_path):
    cfg = _cfg(tmp_path, min_score=0.0)   # disable filtering for this test
    store = RecallStore(cfg.db_path)
    base = _unit_vec(42)
    # Three near-orthogonal entries plus one near-match to the query.
    store.insert_turn(ts=1000, role="user", content="orthogonal A",
                      conv_id="c", embedding=_unit_vec(1))
    store.insert_turn(ts=1001, role="user", content="orthogonal B",
                      conv_id="c", embedding=_unit_vec(2))
    matching_id = store.insert_turn(
        ts=1002, role="user", content="the matching turn",
        conv_id="c", embedding=base,
    )
    store.insert_turn(ts=1003, role="user", content="orthogonal C",
                      conv_id="c", embedding=_unit_vec(3))
    hits = store.search_turns(base, top_k=3, min_score=0.0)
    assert hits[0][0].id == matching_id
    assert hits[0][1] > 0.99


def test_search_respects_min_score(tmp_path):
    cfg = _cfg(tmp_path)
    store = RecallStore(cfg.db_path)
    store.insert_turn(ts=1000, role="user", content="x",
                      conv_id="c", embedding=_unit_vec(1))
    # Query that's orthogonal to the stored vector — below min_score.
    query = _unit_vec(2)
    hits = store.search_turns(query, top_k=5, min_score=0.99)
    assert hits == []


def test_turns_around_returns_neighbours(tmp_path):
    cfg = _cfg(tmp_path)
    store = RecallStore(cfg.db_path)
    ids = []
    for i in range(5):
        tid = store.insert_turn(
            ts=1000.0 + i, role="user", content=f"t{i}",
            conv_id="conv-A", embedding=_unit_vec(10 + i),
        )
        ids.append(tid)
    # A different conversation interleaved — should NOT show as neighbour.
    store.insert_turn(ts=1002.5, role="user", content="other",
                      conv_id="conv-B", embedding=_unit_vec(99))
    middle_id = ids[2]
    neighbours = store.turns_around(middle_id, window=2)
    contents = [t.content for t in neighbours]
    assert "t0" in contents and "t1" in contents
    assert "t3" in contents and "t4" in contents
    assert "other" not in contents


# ── Reflections ────────────────────────────────────────────────────────


def test_reflection_insert_and_search(tmp_path):
    cfg = _cfg(tmp_path, min_score=0.0)
    store = RecallStore(cfg.db_path)
    base = _unit_vec(7)
    rid = store.insert_reflection(
        period_start_ts=1000.0, period_end_ts=2000.0,
        period_kind="session",
        summary="Shipped roadmaps 1-5 and built the recall service.",
        embedding=base,
    )
    assert rid > 0
    hits = store.search_reflections(base, top_k=3, min_score=0.0)
    assert hits
    assert hits[0][0].summary.startswith("Shipped roadmaps")


# ── Facts ──────────────────────────────────────────────────────────────


def test_fact_insert_dedup(tmp_path):
    store = RecallStore(_cfg(tmp_path).db_path)
    fid = store.insert_fact(subject="user", predicate="works_on", object_="ULTRON")
    assert fid is not None
    dup = store.insert_fact(subject="user", predicate="works_on", object_="ULTRON")
    assert dup is None
    facts = store.all_facts()
    assert len(facts) == 1


# ── Retriever ──────────────────────────────────────────────────────────


def test_retriever_builds_bundle_with_neighbours(tmp_path):
    cfg = _cfg(tmp_path, min_score=0.0, neighbour_window=1)
    store = RecallStore(cfg.db_path)
    base = _unit_vec(50)
    # Conversation of 3 turns; middle one is the match.
    store.insert_turn(ts=1000, role="user", content="what's the plan",
                      conv_id="c1", embedding=_unit_vec(60))
    match_id = store.insert_turn(
        ts=1001, role="assistant",
        content="we'll ship recall and then context preserver",
        conv_id="c1", embedding=base,
    )
    store.insert_turn(ts=1002, role="user", content="sounds good",
                      conv_id="c1", embedding=_unit_vec(61))
    r = RecallRetriever(store, cfg)
    bundle = r.search("recall plan", base, top_k=1)
    assert bundle.turn_hits
    hit = bundle.turn_hits[0]
    assert hit.turn.id == match_id
    assert hit.neighbours_before and "what's the plan" in hit.neighbours_before[0].content
    assert hit.neighbours_after and "sounds good" in hit.neighbours_after[0].content


def test_retriever_format_for_prompt(tmp_path):
    cfg = _cfg(tmp_path, min_score=0.0)
    store = RecallStore(cfg.db_path)
    base = _unit_vec(70)
    store.insert_turn(ts=time.time() - 3600, role="user",
                      content="my dog's name is Rex",
                      conv_id="c", embedding=base)
    store.insert_fact(subject="dog", predicate="named", object_="Rex")
    r = RecallRetriever(store, cfg)
    bundle = r.search("Rex", base, top_k=3)
    block = r.format_for_prompt(bundle)
    assert "Long-term memory" in block
    assert "Rex" in block


def test_retriever_facts_substring_match(tmp_path):
    cfg = _cfg(tmp_path)
    store = RecallStore(cfg.db_path)
    store.insert_fact(subject="user", predicate="prefers", object_="dark mode")
    store.insert_fact(subject="user", predicate="lives_in", object_="India")
    r = RecallRetriever(store, cfg)
    # No turn embeddings — only facts should match.
    bundle = r.search("dark mode", _unit_vec(1), top_k=5)
    assert len(bundle.fact_hits) == 1
    assert "dark mode" in bundle.fact_hits[0].fact["object"]


# ── Reload semantics ───────────────────────────────────────────────────


def test_insert_invalidates_search_cache(tmp_path):
    cfg = _cfg(tmp_path, min_score=0.0)
    store = RecallStore(cfg.db_path)
    base = _unit_vec(100)
    # Search empty store — primes the cache.
    assert store.search_turns(base, top_k=5, min_score=0.0) == []
    # Insert after — cache should invalidate so the new row is found.
    store.insert_turn(ts=1000, role="user", content="new thing",
                      conv_id="c", embedding=base)
    hits = store.search_turns(base, top_k=5, min_score=0.0)
    assert hits and hits[0][0].content == "new thing"
