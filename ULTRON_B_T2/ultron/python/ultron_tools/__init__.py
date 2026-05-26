"""Module E — Tool Registry.

The single catalog of every tool ULTRON can invoke. Module C (LLM) parses
``tool`` blocks from model output, then routes them through here. Every
call goes through:

  1. Registry lookup — does the tool exist?
  2. Schema validation — are the args well-formed?
  3. Privacy gate (N) — is the payload safe to act on?
  4. confirm_required check — does the user need to OK this first?
  5. Handler execution — actually run it.
  6. Audit publish — Z (quantum log) records every call.

The registry is initialised once at process start with built-in tools, and
new tools can be registered at runtime by callers holding the singleton.

Public entry points::

    from ultron_tools import init, get_service

    svc = init()             # idempotent
    await svc.run()          # WS subscriber loop
"""
from __future__ import annotations

from typing import Optional

from .config import ToolsConfig, load_tools_config
from .registry import Tool, ToolRegistry, register_builtin_tools
from .service import ToolService

_service: Optional[ToolService] = None


def init(config: Optional[ToolsConfig] = None) -> ToolService:
    """Initialise the singleton ToolService. Idempotent."""
    global _service
    if _service is None:
        cfg = config or load_tools_config()
        registry = ToolRegistry()
        register_builtin_tools(registry, cfg)
        _service = ToolService(cfg, registry)
    return _service


def get_service() -> Optional[ToolService]:
    """Return the live ToolService or None if not initialised."""
    return _service


def get_registry() -> Optional[ToolRegistry]:
    """Return the live ToolRegistry or None if not initialised."""
    if _service is not None:
        return _service.registry
    return None


__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolService",
    "ToolsConfig",
    "get_service",
    "init",
    "load_tools_config",
]
