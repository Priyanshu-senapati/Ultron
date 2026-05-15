"""AgentRunner — drives a single agent's tool-loop to completion.

Loop:
  1. Ask the LLM (via ``llm_call``) with current message list
  2. Parse ``tool`` blocks from the response
  3. If none → final answer; record + return
  4. Otherwise, for each tool call:
     - Check it's in ``agent.allowed_tools`` (else step is an error)
     - Execute via the supplied ``tool_call`` callable
     - If pending_confirm → bail out with AWAITING_CONFIRM state
  5. Feed tool results back as a user-role message, loop
  6. Bound by ``agent.max_tool_rounds`` and ``config.max_total_steps``

The runner is deliberately deps-light: it doesn't import ultron_llm or
ultron_tools. Callers wire those in as callbacks. That makes the runner
trivially unit-testable.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from .agent import Agent
from .config import AgentMeshConfig
from .state import AgentStep, TaskState, TaskStatus

logger = logging.getLogger("ultron.agents.runner")

# Async callable signatures the runner expects:
#   llm_call(system_prompt, messages) -> str  (raw model output, may contain tool blocks)
#   tool_call(name, args, confirm_token=None) -> dict
#     {"ok": bool, "result": Any|None, "error": str|None,
#      "pending_confirm": bool, "confirm_token": str|None,
#      "confirm_reason": str|None}
LlmCall = Callable[[str, list[dict]], Awaitable[str]]
ToolCall = Callable[..., Awaitable[dict[str, Any]]]


class AgentRunner:
    def __init__(
        self,
        agent: Agent,
        llm_call: LlmCall,
        tool_call: ToolCall,
        config: AgentMeshConfig,
    ) -> None:
        self._agent = agent
        self._llm_call = llm_call
        self._tool_call = tool_call
        self._cfg = config

    async def run(self, task: TaskState) -> TaskState:
        from ultron_llm.tool_parser import parse_tool_calls, strip_tool_calls

        task.status = TaskStatus.RUNNING
        messages: list[dict] = [{"role": "user", "content": task.prompt}]
        step_counter = 0

        for round_idx in range(self._agent.max_tool_rounds):
            if step_counter >= self._cfg.max_total_steps:
                task.error = "max_total_steps exceeded"
                task.status = TaskStatus.FAILED
                return task

            try:
                response = await self._llm_call(self._agent.system_prompt, messages)
            except Exception as exc:  # noqa: BLE001
                logger.exception("LLM call failed for role=%s: %s", self._agent.role, exc)
                task.add_step(AgentStep(
                    step_id=step_counter,
                    agent_role=self._agent.role,
                    kind="error",
                    error=f"llm_call: {exc}",
                ))
                task.error = str(exc)
                task.status = TaskStatus.FAILED
                return task

            step_counter += 1
            task.add_step(AgentStep(
                step_id=step_counter,
                agent_role=self._agent.role,
                kind="llm_response",
                content=response,
            ))

            calls = parse_tool_calls(response)
            if not calls:
                # No tools requested → this is the agent's final answer.
                task.final_answer = strip_tool_calls(response).strip() or response.strip()
                task.status = TaskStatus.SUCCEEDED
                return task

            # Execute each tool call sequentially. If any is pending_confirm
            # we stop the whole task and surface that to the caller.
            tool_results_summary: list[str] = []
            for tc in calls:
                step_counter += 1
                if tc.name not in self._agent.allowed_tools:
                    err = (
                        f"tool {tc.name!r} not in allowed_tools for role "
                        f"{self._agent.role}: {sorted(self._agent.allowed_tools)}"
                    )
                    task.add_step(AgentStep(
                        step_id=step_counter,
                        agent_role=self._agent.role,
                        kind="error",
                        tool_name=tc.name,
                        tool_args=tc.args,
                        error=err,
                    ))
                    tool_results_summary.append(
                        f"[tool {tc.name}] denied by role policy: {err}"
                    )
                    continue

                try:
                    tool_resp = await self._tool_call(tc.name, tc.args)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("tool_call failed: %s", exc)
                    task.add_step(AgentStep(
                        step_id=step_counter,
                        agent_role=self._agent.role,
                        kind="error",
                        tool_name=tc.name,
                        tool_args=tc.args,
                        error=str(exc),
                    ))
                    tool_results_summary.append(f"[tool {tc.name}] dispatch error: {exc}")
                    continue

                if tool_resp.get("pending_confirm"):
                    # Park the task; caller will surface the confirm to the user
                    # and later call ``resume_with_confirm``.
                    task.pending_tool_name = tc.name
                    task.pending_confirm_token = tool_resp.get("confirm_token", "")
                    task.pending_confirm_reason = tool_resp.get("confirm_reason", "")
                    task.add_step(AgentStep(
                        step_id=step_counter,
                        agent_role=self._agent.role,
                        kind="tool_call",
                        tool_name=tc.name,
                        tool_args=tc.args,
                        tool_ok=False,
                        tool_request_id=tool_resp.get("request_id", ""),
                    ))
                    task.status = TaskStatus.AWAITING_CONFIRM
                    return task

                task.add_step(AgentStep(
                    step_id=step_counter,
                    agent_role=self._agent.role,
                    kind="tool_result",
                    tool_name=tc.name,
                    tool_args=tc.args,
                    tool_ok=bool(tool_resp.get("ok")),
                    tool_result=tool_resp.get("result"),
                    tool_request_id=tool_resp.get("request_id", ""),
                ))
                tool_results_summary.append(
                    self._format_tool_result(tc.name, tool_resp)
                )

            # Feed all tool outputs back into the conversation so the model
            # can decide the next step. We use a single combined user turn
            # to keep the message list compact.
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": "Tool results:\n\n" + "\n\n".join(tool_results_summary),
            })

        # Exhausted rounds without producing a final answer.
        task.error = (
            f"agent {self._agent.role} exhausted "
            f"{self._agent.max_tool_rounds} tool rounds without a final answer"
        )
        task.status = TaskStatus.FAILED
        return task

    @staticmethod
    def _format_tool_result(name: str, resp: dict[str, Any]) -> str:
        """Render a tool response compactly enough to fit back in the prompt."""
        if not resp.get("ok"):
            return f"[tool {name}] ERROR: {resp.get('error', 'unknown error')}"
        try:
            body = json.dumps(resp.get("result"), default=str)
        except (TypeError, ValueError):
            body = str(resp.get("result"))
        # Hard cap so a giant file read doesn't blow the context window.
        if len(body) > 4000:
            body = body[:4000] + "…[truncated]"
        return f"[tool {name}] OK: {body}"
