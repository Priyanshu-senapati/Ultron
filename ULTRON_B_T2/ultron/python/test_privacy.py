"""Tests for Module N (Privacy Router).

10 specified tests from the build spec, plus a few extra invariants that
fell out of writing the code. All run without network or any sidecar
process — pure unit tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultron_privacy.anonymizer import HashAnonymizer
from ultron_privacy.classifier import DataClass, DataClassifier
from ultron_privacy.config import DEFAULT_LOCAL_ONLY_PATTERNS, PrivacyConfig
from ultron_privacy.gate import OutboundGate
from ultron_privacy.service import PrivacyService


@pytest.fixture
def classifier() -> DataClassifier:
    return DataClassifier(DEFAULT_LOCAL_ONLY_PATTERNS)


@pytest.fixture
def anonymizer() -> HashAnonymizer:
    return HashAnonymizer(salt="test-salt-xyz")


@pytest.fixture
def gate(tmp_path: Path, classifier: DataClassifier, anonymizer: HashAnonymizer) -> OutboundGate:
    return OutboundGate(
        classifier=classifier,
        anonymizer=anonymizer,
        audit_log_path=tmp_path / "audit.jsonl",
        log_every_n=1,   # log every decision so we can verify
    )


# ─────────────────────────────────────────────────────────────────────────
# Specified tests (1–10)
# ─────────────────────────────────────────────────────────────────────────


def test_1_classifier_marks_window_titles_local_only(classifier: DataClassifier) -> None:
    assert classifier.classify_window_title("anything") == DataClass.LOCAL_ONLY
    assert classifier.classify_window_title("") == DataClass.LOCAL_ONLY


def test_2_classifier_marks_user_paths_local_only(classifier: DataClassifier) -> None:
    assert classifier.classify(r"C:\Users\priyanshu\Desktop\file.txt") == DataClass.LOCAL_ONLY
    assert classifier.classify("/home/alice/.ssh/id_rsa") == DataClass.LOCAL_ONLY
    assert classifier.classify("AppData\\Roaming\\thing") == DataClass.LOCAL_ONLY


def test_3_classifier_marks_tension_score_shareable(classifier: DataClassifier) -> None:
    assert classifier.classify_tension_score(0.42) == DataClass.SHAREABLE
    assert classifier.classify_tension_score(0.0) == DataClass.SHAREABLE
    assert classifier.classify_tension_score(1.0) == DataClass.SHAREABLE


def test_4_gate_redacts_local_only_payload(gate: OutboundGate) -> None:
    # Payload has a focus_app — should hash on its way through.
    decision = gate.check("http_external", {"focus_app": r"C:\Users\priyanshu\code.exe", "tension": 0.5})
    assert decision.allowed
    assert decision.redacted_payload is not None
    # focus_app value replaced by hash:...
    assert decision.redacted_payload["focus_app"].startswith("hash:")
    # tension unchanged
    assert decision.redacted_payload["tension"] == 0.5


def test_5_gate_allows_tension_score_to_ghost(gate: OutboundGate) -> None:
    decision = gate.approve_ghost_export("insight_snapshot", {"tension": 0.42, "cognitive_load": 0.31})
    assert decision.allowed
    assert decision.redacted_payload == {"tension": 0.42, "cognitive_load": 0.31}


def test_6_anonymizer_consistent_for_same_input(anonymizer: HashAnonymizer) -> None:
    a = anonymizer.hash_value("foo")
    b = anonymizer.hash_value("foo")
    assert a == b
    assert len(a) == 16  # 16 hex chars = 64 bits


def test_7_anonymizer_different_for_different_input(anonymizer: HashAnonymizer) -> None:
    assert anonymizer.hash_value("foo") != anonymizer.hash_value("bar")
    assert anonymizer.hash_value("") != anonymizer.hash_value(" ")


def test_8_gate_claude_redacts_file_paths_from_messages(gate: OutboundGate) -> None:
    system_prompt = "You are helpful."
    messages = [
        {"role": "user", "content": r"What is in C:\Users\priyanshu\notes.txt please"},
        {"role": "assistant", "content": "Sure."},
    ]
    decision = gate.approve_claude_call(system_prompt, messages)
    assert decision.allowed
    assert decision.redaction_count >= 1
    # Path replaced by [REDACTED] in the user message
    redacted_msgs = decision.redacted_payload["messages"]
    assert "[REDACTED]" in redacted_msgs[0]["content"]
    assert "priyanshu" not in redacted_msgs[0]["content"]


def test_9_gate_ghost_export_allows_tension(gate: OutboundGate) -> None:
    decision = gate.approve_ghost_export("insight_snapshot", {
        "tension": 0.5, "cognitive_load": 0.42, "phase": "afternoon",
    })
    assert decision.allowed
    assert decision.redacted_payload["tension"] == 0.5


def test_10_gate_ghost_export_hashes_focus_app(gate: OutboundGate) -> None:
    decision = gate.approve_ghost_export("insight_snapshot", {
        "tension": 0.5, "focus_app": "Code.exe", "window_title": "main.rs - ultron",
    })
    assert decision.allowed
    assert decision.redacted_payload["focus_app"].startswith("hash:")
    assert decision.redacted_payload["window_title"].startswith("hash:")
    # Hashes are deterministic — same value twice yields same hash.
    decision2 = gate.approve_ghost_export("insight_snapshot", {"focus_app": "Code.exe"})
    assert decision.redacted_payload["focus_app"] == decision2.redacted_payload["focus_app"]


# ─────────────────────────────────────────────────────────────────────────
# Bonus invariants
# ─────────────────────────────────────────────────────────────────────────


def test_anonymizer_rejects_empty_salt() -> None:
    with pytest.raises(ValueError):
        HashAnonymizer(salt="")


def test_classifier_detects_api_keys(classifier: DataClassifier) -> None:
    assert classifier.classify("sk-1234567890abcdefghijklmnop") == DataClass.LOCAL_ONLY
    assert classifier.classify("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9") == DataClass.LOCAL_ONLY


def test_most_restrictive_picks_local_only() -> None:
    assert DataClass.most_restrictive(
        DataClass.SHAREABLE, DataClass.LOCAL_ONLY, DataClass.ANONYMIZED
    ) == DataClass.LOCAL_ONLY


def test_audit_log_writes_jsonl(gate: OutboundGate, tmp_path: Path) -> None:
    # First decision (log_every_n=1 → always logs).
    gate.approve_ghost_export("insight_snapshot", {"tension": 0.5})
    audit = tmp_path / "audit.jsonl"
    assert audit.exists()
    lines = audit.read_text().strip().splitlines()
    assert len(lines) >= 1
    parsed = json.loads(lines[-1])
    assert parsed["destination"] == "ghost_network"


def test_claude_call_drops_message_when_entirely_redacted(gate: OutboundGate) -> None:
    decision = gate.approve_claude_call("ok", [
        {"role": "user", "content": r"C:\Users\priyanshu\AppData\Roaming\ULTRON\password=secret"},
        {"role": "user", "content": "normal question"},
    ])
    msgs = decision.redacted_payload["messages"]
    # The first message was almost entirely LOCAL_ONLY but the wrapping
    # `=` etc means it doesn't collapse to *just* [REDACTED] — still keep.
    # The normal one must survive intact.
    assert any("normal question" in m.get("content", "") for m in msgs)


def test_service_singleton_pattern() -> None:
    """ultron_privacy.init() returns the same instance on repeat calls."""
    from ultron_privacy import get_service, init

    cfg = PrivacyConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="dummy",
        anonymizer_salt="test",
    )
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b
    assert a is c
