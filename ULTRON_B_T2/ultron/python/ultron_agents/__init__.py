"""Module F — Agent Mesh.

Multi-agent orchestrator. A top-level Task is run by a coordinator agent
that may delegate sub-tasks to specialised worker agents (researcher,
coder, reviewer). Each agent has:

  - A role and a system prompt
  - An allow-list of tools from Module E
  - A bounded tool-loop (parse model output → execute tools → feed back)

All LLM calls go through Module C (so privacy gating + persona apply
once, in one place). All tool calls go through Module E (so confirm +
audit + sandbox apply once, in one place). Z observes the resulting
``agent_step`` and ``agent_task_complete`` events.

Public entry points::

    from ultron_agents import init, get_service
    svc = init()
    result = await svc.run_task("write a python hello world", role="coder")
"""
from __future__ import annotations

from typing import Optional

from .agent import Agent, AgentRole
from .config import AgentMeshConfig, load_agent_mesh_config
from .mesh import AgentMesh
from .service import AgentService
from .state import AgentStep, TaskResult, TaskState, TaskStatus

_service: Optional[AgentService] = None


def init(config: Optional[AgentMeshConfig] = None) -> AgentService:
    """Initialise the singleton AgentService. Idempotent."""
    global _service
    if _service is None:
        cfg = config or load_agent_mesh_config()
        _service = AgentService(cfg)
    return _service


def get_service() -> Optional[AgentService]:
    """Return the live AgentService or None if not initialised."""
    return _service


__all__ = [
    "Agent",
    "AgentMesh",
    "AgentMeshConfig",
    "AgentRole",
    "AgentService",
    "AgentStep",
    "TaskResult",
    "TaskState",
    "TaskStatus",
    "get_service",
    "init",
    "load_agent_mesh_config",
]
