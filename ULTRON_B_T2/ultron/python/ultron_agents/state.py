"""Shared state structures for a multi-step agent task."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CONFIRM = "awaiting_confirm"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass
class AgentStep:
    """One step in an agent's tool-loop. Either an LLM response or a tool
    result (or an error)."""

    step_id: int
    agent_role: str
    kind: str  # "llm_response" | "tool_call" | "tool_result" | "error"

    # On llm_response
    content: str = ""

    # On tool_call / tool_result
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    tool_ok: Optional[bool] = None
    tool_request_id: str = ""

    # On error
    error: str = ""

    ts_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tool_result might not be JSON-friendly — coerce defensively
        if d["tool_result"] is not None:
            try:
                import json
                json.dumps(d["tool_result"])
            except TypeError:
                d["tool_result"] = str(d["tool_result"])[:2000]
        return d


@dataclass
class TaskState:
    task_id: str
    prompt: str
    role: str
    status: TaskStatus = TaskStatus.PENDING
    steps: list[AgentStep] = field(default_factory=list)
    started_at_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    finished_at_unix_ms: int = 0
    final_answer: str = ""
    error: str = ""

    # If status==AWAITING_CONFIRM, this points to the pending tool call.
    pending_tool_name: str = ""
    pending_confirm_token: str = ""
    pending_confirm_reason: str = ""

    def add_step(self, step: AgentStep) -> None:
        self.steps.append(step)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["steps"] = [s.to_dict() for s in self.steps]
        return d


@dataclass
class TaskResult:
    """Public, compact result returned to callers and published over WS."""

    task_id: str
    ok: bool
    status: TaskStatus
    final_answer: str
    step_count: int
    duration_ms: int
    error: str = ""
    pending_confirm_token: str = ""
    pending_tool_name: str = ""
    pending_confirm_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
