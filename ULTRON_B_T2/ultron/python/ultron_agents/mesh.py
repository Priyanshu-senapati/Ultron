"""AgentMesh — registry + dispatcher of agents.

The mesh owns:
  - The set of registered Agents (by role string)
  - A live map of task_id → TaskState for inspection and resume
  - The bindings to ultron_llm (C) and ultron_tools (E)

Public API::

    mesh.register_agent(agent)
    await mesh.run_task(prompt, role="coordinator") -> TaskResult
    await mesh.resume_with_confirm(task_id, approve=True) -> TaskResult

When ``ultron_llm.get_service`` and ``ultron_tools.get_service`` are
unavailable (e.g. during tests), the mesh accepts injected callables in
``__init__``. That's the seam our unit tests use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .agent import Agent, AgentRole
from .config import AgentMeshConfig
from .runner import AgentRunner
from .state import AgentStep, TaskResult, TaskState, TaskStatus

logger = logging.getLogger("ultron.agents.mesh")

LlmCall = Callable[[str, list[dict]], Awaitable[str]]
ToolCall = Callable[..., Awaitable[dict[str, Any]]]


class AgentMesh:
    def __init__(
        self,
        config: AgentMeshConfig,
        llm_call: Optional[LlmCall] = None,
        tool_call: Optional[ToolCall] = None,
    ) -> None:
        self._cfg = config
        self._llm_call = llm_call or self._default_llm_call
        self._tool_call = tool_call or self._default_tool_call
        self._agents: dict[str, Agent] = {}
        self._tasks: dict[str, TaskState] = {}
        # Wire built-ins
        for role in AgentRole:
            self.register_agent(Agent.for_role(role, max_tool_rounds=config.max_tool_rounds))

    # ── Public ──────────────────────────────────────────────────────────

    def register_agent(self, agent: Agent) -> None:
        if not agent.role:
            raise ValueError("agent.role required")
        if agent.role in self._agents:
            logger.warning("agent role %r already registered — overwriting", agent.role)
        self._agents[agent.role] = agent

    def get_agent(self, role: str) -> Optional[Agent]:
        return self._agents.get(role)

    def roles(self) -> list[str]:
        return sorted(self._agents.keys())

    def get_task(self, task_id: str) -> Optional[TaskState]:
        return self._tasks.get(task_id)

    async def run_task(
        self,
        prompt: str,
        role: str = AgentRole.COORDINATOR.value,
        task_id: Optional[str] = None,
    ) -> TaskResult:
        agent = self._agents.get(role)
        if agent is None:
            return self._error_result(
                task_id or _new_task_id(),
                prompt,
                role,
                f"unknown agent role {role!r}; known: {self.roles()}",
            )

        tid = task_id or _new_task_id()
        state = TaskState(task_id=tid, prompt=prompt, role=role)
        self._tasks[tid] = state

        runner = AgentRunner(
            agent=agent,
            llm_call=self._llm_call,
            tool_call=self._tool_call,
            config=self._cfg,
        )
        try:
            await asyncio.wait_for(
                runner.run(state),
                timeout=self._cfg.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            state.status = TaskStatus.TIMED_OUT
            state.error = f"task exceeded {self._cfg.task_timeout_seconds}s timeout"
        state.finished_at_unix_ms = int(time.time() * 1000)
        self._append_audit(state)
        return self._finalize(state)

    async def resume_with_confirm(
        self,
        task_id: str,
        approve: bool,
    ) -> TaskResult:
        """Resume a parked AWAITING_CONFIRM task. If ``approve`` is True, we
        re-issue the pending tool with the saved confirm_token; if False we
        cancel and surface a failure result."""
        state = self._tasks.get(task_id)
        if state is None:
            return self._error_result(task_id, "", "", f"no such task: {task_id}")
        if state.status != TaskStatus.AWAITING_CONFIRM:
            return self._finalize(state)  # already done — return current
        if not approve:
            state.status = TaskStatus.CANCELLED
            state.error = "user_rejected_confirm"
            state.finished_at_unix_ms = int(time.time() * 1000)
            self._append_audit(state)
            return self._finalize(state)

        # Approve path: re-issue the pending tool call with the saved token.
        last_step = next(
            (s for s in reversed(state.steps) if s.kind == "tool_call"),
            None,
        )
        if last_step is None or not state.pending_confirm_token:
            state.status = TaskStatus.FAILED
            state.error = "no pending tool call to resume"
            state.finished_at_unix_ms = int(time.time() * 1000)
            self._append_audit(state)
            return self._finalize(state)

        try:
            tool_resp = await self._tool_call(
                last_step.tool_name,
                last_step.tool_args,
                confirm_token=state.pending_confirm_token,
            )
        except Exception as exc:  # noqa: BLE001
            state.status = TaskStatus.FAILED
            state.error = f"resume dispatch error: {exc}"
            state.finished_at_unix_ms = int(time.time() * 1000)
            self._append_audit(state)
            return self._finalize(state)

        state.add_step(AgentStep(
            step_id=len(state.steps) + 1,
            agent_role=state.role,
            kind="tool_result",
            tool_name=last_step.tool_name,
            tool_args=last_step.tool_args,
            tool_ok=bool(tool_resp.get("ok")),
            tool_result=tool_resp.get("result"),
            tool_request_id=tool_resp.get("request_id", ""),
        ))
        state.pending_tool_name = ""
        state.pending_confirm_token = ""
        state.pending_confirm_reason = ""

        # Now continue the agent loop with the new tool result fed back in.
        # We re-enter the runner with a fresh state but pre-seeded steps.
        # For simplicity we don't re-call the LLM here — we mark the task
        # SUCCEEDED if the confirmed tool returned ok, otherwise FAILED.
        # Multi-step continuation after confirm is intentionally not part
        # of this milestone; the next user prompt re-invokes the agent.
        if tool_resp.get("ok"):
            try:
                state.final_answer = json.dumps(tool_resp.get("result"), default=str)
            except (TypeError, ValueError):
                state.final_answer = str(tool_resp.get("result"))
            state.status = TaskStatus.SUCCEEDED
        else:
            state.error = str(tool_resp.get("error", "tool failed after confirm"))
            state.status = TaskStatus.FAILED
        state.finished_at_unix_ms = int(time.time() * 1000)
        self._append_audit(state)
        return self._finalize(state)

    # ── Default bindings to C and E ──────────────────────────────────────

    async def _default_llm_call(self, system_prompt: str, messages: list[dict]) -> str:
        """Call Module C's underlying Ollama client directly. We don't go
        through ``LLMService.ask`` because that drives its own conversation
        / persona / vision routing — for agents we want the raw chat
        primitive with our own system prompt."""
        try:
            from ultron_llm import get_service as _llm_get_service  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "ultron_llm not importable; cannot run agents without LLM"
            ) from exc
        svc = _llm_get_service()
        client = getattr(svc, "_ollama", None)
        if client is None or not hasattr(client, "chat"):
            raise RuntimeError("LLMService has no _ollama client attached")
        return await client.chat(system_prompt=system_prompt, messages=messages)

    async def _default_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        confirm_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Dispatch to Module E's executor in-process."""
        try:
            from ultron_tools import get_service as _tool_get_service  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("ultron_tools not importable") from exc
        svc = _tool_get_service()
        if svc is None:
            raise RuntimeError("ultron_tools not initialised")
        result = await svc.execute(
            name=name,
            args=args,
            confirm_token=confirm_token,
        )
        return {
            "ok": result.ok,
            "result": result.result,
            "error": result.error,
            "pending_confirm": result.pending_confirm,
            "confirm_token": result.confirm_token,
            "confirm_reason": result.confirm_reason,
            "request_id": result.request_id,
        }

    # ── Internals ────────────────────────────────────────────────────────

    def _finalize(self, state: TaskState) -> TaskResult:
        duration = max(0, state.finished_at_unix_ms - state.started_at_unix_ms)
        return TaskResult(
            task_id=state.task_id,
            ok=state.status == TaskStatus.SUCCEEDED,
            status=state.status,
            final_answer=state.final_answer,
            step_count=len(state.steps),
            duration_ms=duration,
            error=state.error,
            pending_confirm_token=state.pending_confirm_token,
            pending_tool_name=state.pending_tool_name,
            pending_confirm_reason=state.pending_confirm_reason,
        )

    def _error_result(self, task_id: str, prompt: str, role: str, err: str) -> TaskResult:
        return TaskResult(
            task_id=task_id,
            ok=False,
            status=TaskStatus.FAILED,
            final_answer="",
            step_count=0,
            duration_ms=0,
            error=err,
        )

    def _append_audit(self, state: TaskState) -> None:
        path: Path = self._cfg.audit_log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                # Don't write all step contents — keep audit terse.
                summary = {
                    "task_id": state.task_id,
                    "role": state.role,
                    "status": state.status.value,
                    "prompt": state.prompt[:400],
                    "final_answer": state.final_answer[:400],
                    "error": state.error,
                    "step_count": len(state.steps),
                    "started_at_unix_ms": state.started_at_unix_ms,
                    "finished_at_unix_ms": state.finished_at_unix_ms,
                }
                f.write(json.dumps(summary) + "\n")
        except OSError as exc:
            logger.warning("agent audit log write failed: %s", exc)


def _new_task_id() -> str:
    return secrets.token_hex(8)
