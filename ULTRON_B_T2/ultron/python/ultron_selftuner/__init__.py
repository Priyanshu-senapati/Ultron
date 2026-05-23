"""Upgrade #77 — Self-Improvement Closed Loop.

The selftuner watches the bus for tool_call_audit + emotion_state_changed
events while reading the existing SQLite stores (flow, interrupts,
readiness, recall). Every 24 hours (and on demand) it writes a
dated markdown reflection summarising the day plus actionable tuning
suggestions (e.g. "tool X is failing too often", "flow keeps breaking
on app_switch — raise the threshold", "frustration baseline is high
today — investigate").

The reflections are read-only by design — we surface suggestions, never
auto-mutate config.toml. The user opts in by editing config or asking
ULTRON to do so explicitly.

Output:
  - %APPDATA%/ULTRON/self_reflections/YYYY-MM-DD.md      (human read)
  - %APPDATA%/ULTRON/self_reflections/YYYY-MM-DD.json    (machine)
  - %APPDATA%/ULTRON/self_reflections/latest.md          (quick read)

Public entry::

    from ultron_selftuner import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import SelfTunerConfig, load_selftuner_config
from .observer import EmotionObserver, ToolUsageObserver
from .reflector import gather_facts, render_markdown
from .service import SelfTunerService
from .tuner import suggest

_service: Optional[SelfTunerService] = None


def init(config: Optional[SelfTunerConfig] = None) -> SelfTunerService:
    global _service
    if _service is None:
        cfg = config or load_selftuner_config()
        _service = SelfTunerService(cfg)
    return _service


def get_service() -> Optional[SelfTunerService]:
    return _service


__all__ = [
    "EmotionObserver",
    "SelfTunerConfig",
    "SelfTunerService",
    "ToolUsageObserver",
    "gather_facts",
    "get_service",
    "init",
    "load_selftuner_config",
    "render_markdown",
    "suggest",
]
