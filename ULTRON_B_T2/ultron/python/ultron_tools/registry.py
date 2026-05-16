"""Tool registry — the catalog of every callable tool.

A ``Tool`` is just a metadata record + an async handler. Handlers receive
``args: dict`` and return a JSON-serialisable result (or raise to signal
failure).

The registry exposes::

    registry.register(tool)
    registry.get(name) -> Tool | None
    registry.list() -> list[Tool]   # for the LLM system prompt

``register_builtin_tools`` is the one-stop wire-up — every shipped tool is
imported and registered there. Adding a new built-in means: write the
handler in ``builtin/``, then add one line here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import ToolsConfig

logger = logging.getLogger("ultron.tools.registry")

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    args_schema: dict[str, Any]
    handler: ToolHandler

    # Confirmation policy
    confirm_required: bool = False
    confirm_reason: str = ""

    # Categorical tag (helps the LLM pick the right one)
    category: str = "general"

    # Whether this tool should be exposed to the LLM system prompt.
    # Some internal tools (audit/diagnostic) are registered but hidden.
    visible_to_llm: bool = True

    def to_descriptor(self) -> dict[str, Any]:
        """Compact JSON for the LLM system prompt — no handler, no
        Python objects."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "confirm_required": self.confirm_required,
            "args_schema": self.args_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name is required")
        if tool.name in self._tools:
            logger.warning("tool %r already registered — overwriting", tool.name)
        self._tools[tool.name] = tool
        logger.info(
            "registered tool name=%s confirm=%s category=%s",
            tool.name,
            tool.confirm_required,
            tool.category,
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self, visible_only: bool = True) -> list[Tool]:
        if visible_only:
            return [t for t in self._tools.values() if t.visible_to_llm]
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def register_builtin_tools(registry: ToolRegistry, config: ToolsConfig) -> None:
    """Register every shipped tool. Order matters only for log readability."""
    # Local imports — keeps the registry module light-weight and lets
    # the singleton be importable from contexts where the builtin
    # dependencies (psutil, ddgs) aren't available.
    from .builtin import (
        brightness,
        code_query,
        delete_file,
        dopamine_query,
        kg_query,
        knowledge_search,
        media_control,
        memory_query,
        money_query,
        open_app,
        plan_query,
        read_file,
        screenshot,
        shell,
        web_search,
        wellness_query,
        write_file,
    )

    for tool in (
        read_file.build(config),
        write_file.build(config),
        delete_file.build(config),
        shell.build(config),
        web_search.build(config),
        screenshot.build(config),
        memory_query.build(config),
        knowledge_search.build(config),
        code_query.build(config),
        money_query.build(config),
        wellness_query.build(config),
        plan_query.build(config),
        kg_query.build(config),
        dopamine_query.build(config),
        open_app.build(config),
        media_control.build(config),
        brightness.build(config),
    ):
        # The config-level confirm list is a strict additive override.
        if tool.name in config.confirm_required_tools and not tool.confirm_required:
            tool.confirm_required = True
            tool.confirm_reason = tool.confirm_reason or "listed in [tools].confirm_required_tools"
        registry.register(tool)
