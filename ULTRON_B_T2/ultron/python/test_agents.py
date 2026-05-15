"""Tests for Module F (Agent Mesh).

All unit tests. We inject fake llm_call / tool_call so no real LLM or
tool subsystem is touched.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ultron_agents.agent import Agent, AgentRole
from ultron_agents.config import AgentMeshConfig
from ultron_agents.mesh import AgentMesh
from ultron_agents.state import TaskStatus


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path: Path) -> AgentMeshConfig:
    return AgentMeshConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="t",
        max_tool_rounds=4,
        max_total_steps=12,
        task_timeout_seconds=10,
        audit_log_path=tmp_path / "agent_audit.jsonl",
    )


def _make_mesh(
    cfg: AgentMeshConfig,
    llm_responses: list[str],
    tool_responses: dict[str, Any] | None = None,
) -> AgentMesh:
    """Build a mesh wired with deterministic llm/tool stubs."""
    responses = list(llm_responses)
    tool_responses = tool_responses or {}

    async def llm(system_prompt: str, messages: list[dict]) -> str:
        if not responses:
            return "(no more canned responses)"
        return responses.pop(0)

    async def tool(name: str, args: dict[str, Any], confirm_token: str | None = None) -> dict[str, Any]:
        key = name
        return tool_responses.get(
            key,
            {"ok": True, "result": {"echo": args}, "error": None,
             "pending_confirm": False, "confirm_token": None,
             "confirm_reason": None, "request_id": "rid"},
        )

    return AgentMesh(cfg, llm_call=llm, tool_call=tool)


# ── Agent role defaults ──────────────────────────────────────────────────


def test_agent_for_role_has_prompt_and_tools() -> None:
    a = Agent.for_role(AgentRole.RESEARCHER)
    assert a.role == "researcher"
    assert "RESEARCHER" in a.system_prompt
    assert "web_search" in a.allowed_tools
    assert "shell" not in a.allowed_tools


def test_mesh_registers_builtin_roles(cfg: AgentMeshConfig) -> None:
    mesh = _make_mesh(cfg, [])
    roles = mesh.roles()
    for r in ("coordinator", "researcher", "coder", "reviewer", "sysadmin"):
        assert r in roles


# ── Happy path: model gives direct answer, no tools ──────────────────────


def test_run_task_direct_answer(cfg: AgentMeshConfig) -> None:
    mesh = _make_mesh(cfg, ["The answer is 42."])
    result = asyncio.run(mesh.run_task("what is the answer?", role="coordinator"))
    assert result.ok is True
    assert result.status == TaskStatus.SUCCEEDED
    assert "42" in result.final_answer


# ── Tool use: model calls a tool, then gives final answer ────────────────


def test_run_task_with_tool_call(cfg: AgentMeshConfig) -> None:
    llm_responses = [
        "Let me look it up.\n\n```tool\n{\"name\": \"web_search\", \"args\": {\"query\": \"python latest\"}}\n```",
        "Based on the search, Python's latest version is 3.13.",
    ]
    mesh = _make_mesh(cfg, llm_responses)
    result = asyncio.run(mesh.run_task("what's the latest python?", role="coordinator"))
    assert result.ok is True
    assert "3.13" in result.final_answer
    assert result.step_count >= 3  # llm_response + tool_result + llm_response


# ── Tool-allow-list enforcement ──────────────────────────────────────────


def test_disallowed_tool_is_blocked(cfg: AgentMeshConfig) -> None:
    # Researcher must not be able to call shell.
    llm_responses = [
        "```tool\n{\"name\": \"shell\", \"args\": {\"cmd\": \"rm -rf /\"}}\n```",
        "Sorry, I can't run shell. Here's my plain answer.",
    ]
    mesh = _make_mesh(cfg, llm_responses)
    result = asyncio.run(mesh.run_task("clean up the system", role="researcher"))
    # The shell call was denied; the LLM's follow-up gave a final answer.
    assert result.ok is True
    task = mesh.get_task(result.task_id)
    assert task is not None
    assert any(s.kind == "error" and "not in allowed_tools" in s.error for s in task.steps)


# ── Confirm-required tool parks the task ─────────────────────────────────


def test_confirm_required_parks_task(cfg: AgentMeshConfig) -> None:
    llm_responses = [
        "```tool\n{\"name\": \"shell\", \"args\": {\"cmd\": \"dir\"}}\n```",
    ]
    tool_responses = {
        "shell": {
            "ok": False,
            "result": None,
            "error": None,
            "pending_confirm": True,
            "confirm_token": "TOK-123",
            "confirm_reason": "shell needs approval",
            "request_id": "rid",
        }
    }
    mesh = _make_mesh(cfg, llm_responses, tool_responses)
    result = asyncio.run(mesh.run_task("show me the directory", role="sysadmin"))
    assert result.status == TaskStatus.AWAITING_CONFIRM
    assert result.pending_confirm_token == "TOK-123"
    assert result.pending_tool_name == "shell"


def test_resume_with_approve(cfg: AgentMeshConfig) -> None:
    llm_responses = [
        "```tool\n{\"name\": \"shell\", \"args\": {\"cmd\": \"dir\"}}\n```",
    ]
    # First call returns pending_confirm; the resume re-invokes the tool
    # with the saved token. We track the calls so the second invocation
    # sees confirm_token and returns success.
    call_log: list[dict[str, Any]] = []

    async def tool(name: str, args: dict[str, Any], confirm_token: str | None = None) -> dict[str, Any]:
        call_log.append({"name": name, "confirm_token": confirm_token})
        if confirm_token is None:
            return {"ok": False, "result": None, "error": None,
                    "pending_confirm": True, "confirm_token": "TOK-123",
                    "confirm_reason": "shell needs approval", "request_id": "rid"}
        return {"ok": True, "result": {"exit_code": 0, "output": "Volume in drive C\n"},
                "error": None, "pending_confirm": False, "confirm_token": None,
                "confirm_reason": None, "request_id": "rid"}

    async def llm(system_prompt: str, messages: list[dict]) -> str:
        return llm_responses.pop(0) if llm_responses else "(end)"

    mesh = AgentMesh(cfg, llm_call=llm, tool_call=tool)
    parked = asyncio.run(mesh.run_task("show dir", role="sysadmin"))
    assert parked.status == TaskStatus.AWAITING_CONFIRM
    final = asyncio.run(mesh.resume_with_confirm(parked.task_id, approve=True))
    assert final.ok is True
    assert "exit_code" in final.final_answer
    # Verify the resume call used the saved token.
    assert any(c["confirm_token"] == "TOK-123" for c in call_log)


def test_resume_with_reject(cfg: AgentMeshConfig) -> None:
    llm_responses = [
        "```tool\n{\"name\": \"shell\", \"args\": {\"cmd\": \"dir\"}}\n```",
    ]
    tool_responses = {
        "shell": {
            "ok": False, "result": None, "error": None,
            "pending_confirm": True, "confirm_token": "TOK-X",
            "confirm_reason": "needs approval", "request_id": "rid",
        }
    }
    mesh = _make_mesh(cfg, llm_responses, tool_responses)
    parked = asyncio.run(mesh.run_task("show dir", role="sysadmin"))
    assert parked.status == TaskStatus.AWAITING_CONFIRM
    cancelled = asyncio.run(mesh.resume_with_confirm(parked.task_id, approve=False))
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.error == "user_rejected_confirm"


# ── Bounds: max_tool_rounds exhausted ────────────────────────────────────


def test_max_tool_rounds_exhausted(cfg: AgentMeshConfig) -> None:
    # Every response is a tool call → never converges.
    spam = "```tool\n{\"name\": \"web_search\", \"args\": {\"query\": \"q\"}}\n```"
    mesh = _make_mesh(cfg, [spam] * 100)
    result = asyncio.run(mesh.run_task("loop forever", role="researcher"))
    assert result.ok is False
    assert (
        "exhausted" in result.error
        or "max_total_steps" in result.error
    )


# ── Unknown role ─────────────────────────────────────────────────────────


def test_unknown_role_returns_error(cfg: AgentMeshConfig) -> None:
    mesh = _make_mesh(cfg, [])
    result = asyncio.run(mesh.run_task("hi", role="ninja"))
    assert result.ok is False
    assert "unknown agent role" in result.error


# ── Audit log written ────────────────────────────────────────────────────


def test_audit_log_jsonl(cfg: AgentMeshConfig) -> None:
    mesh = _make_mesh(cfg, ["Direct answer."])
    asyncio.run(mesh.run_task("hello", role="coordinator"))
    audit = cfg.audit_log_path
    assert audit.exists()
    lines = [l for l in audit.read_text().splitlines() if l.strip()]
    assert lines, "expected at least one audit line"
    last = json.loads(lines[-1])
    assert last["status"] == "succeeded"
    assert last["role"] == "coordinator"


# ── Singleton ────────────────────────────────────────────────────────────


def test_service_singleton(tmp_path: Path) -> None:
    from ultron_agents import get_service, init
    import ultron_agents
    ultron_agents._service = None  # noqa: SLF001

    cfg = AgentMeshConfig(
        ws_url="ws://127.0.0.1:9420/ws", ws_token="t",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_agents._service = None  # noqa: SLF001
