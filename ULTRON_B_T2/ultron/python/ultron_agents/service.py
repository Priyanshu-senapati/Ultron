"""AgentService — WS-facing owner of the AgentMesh.

Subscribes:
  - ``agent_task_request`` — run a new task
      payload: {prompt, role?, task_id?}
  - ``agent_task_resume``  — approve/reject a parked confirm
      payload: {task_id, approve: bool}

Publishes:
  - ``agent_task_started``
  - ``agent_step``          — every llm_response / tool_call / tool_result
  - ``agent_task_complete``
  - ``agent_task_pending``  — when status == AWAITING_CONFIRM
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ultron_bridge import UltronBridge

from .config import AgentMeshConfig
from .mesh import AgentMesh
from .state import TaskResult, TaskStatus

logger = logging.getLogger("ultron.agents.service")


class AgentService:
    def __init__(self, config: AgentMeshConfig) -> None:
        self._cfg = config
        self._mesh = AgentMesh(config)
        self._bridge: Optional[UltronBridge] = None

    # ── Public Python API ───────────────────────────────────────────────

    @property
    def mesh(self) -> AgentMesh:
        return self._mesh

    async def run_task(
        self,
        prompt: str,
        role: str = "coordinator",
        task_id: Optional[str] = None,
    ) -> TaskResult:
        result = await self._mesh.run_task(prompt=prompt, role=role, task_id=task_id)
        await self._publish_result(result)
        return result

    async def resume(self, task_id: str, approve: bool) -> TaskResult:
        result = await self._mesh.resume_with_confirm(task_id, approve=approve)
        await self._publish_result(result)
        return result

    # ── WS subscriber ───────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._cfg.ws_token:
            raise RuntimeError("bridge.token missing — cannot start agent service")
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["agent_task_request", "agent_task_resume"],
            role="agent-mesh",
        )
        logger.info(
            "AgentService starting — roles=%s",
            self._mesh.roles(),
        )
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        if kind == "agent_task_request":
            await self._on_request(payload)
        elif kind == "agent_task_resume":
            await self._on_resume(payload)

    async def _on_request(self, payload: dict[str, Any]) -> None:
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return
        role = str(payload.get("role", "coordinator")) or "coordinator"
        tid = str(payload.get("task_id", "")) or None
        if self._bridge is not None:
            await self._bridge.publish(
                "agent_task_started",
                {"task_id": tid or "", "role": role, "prompt": prompt[:200]},
            )
        result = await self._mesh.run_task(prompt=prompt, role=role, task_id=tid)
        await self._publish_result(result)

    async def _on_resume(self, payload: dict[str, Any]) -> None:
        tid = str(payload.get("task_id", ""))
        approve = bool(payload.get("approve", False))
        if not tid:
            return
        result = await self._mesh.resume_with_confirm(tid, approve=approve)
        await self._publish_result(result)

    async def _publish_result(self, result: TaskResult) -> None:
        if self._bridge is None:
            return
        if result.status == TaskStatus.AWAITING_CONFIRM:
            await self._bridge.publish("agent_task_pending", result.to_dict())
        else:
            await self._bridge.publish("agent_task_complete", result.to_dict())
