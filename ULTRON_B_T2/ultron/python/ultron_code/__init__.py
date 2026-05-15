"""Module G — Code Intelligence.

Per-repo source index. Scans the configured root, parses files by
language to extract symbols (functions, classes, methods), and stores
them in a small SQLite database. Agents and the LLM query this index
via tools registered in Module E.

Public entry::

    from ultron_code import init, get_service
    svc = init()
    await svc.run()
"""
from __future__ import annotations

from typing import Optional

from .config import CodeIntelConfig, load_code_config
from .index import CodeIndex
from .service import CodeService

_service: Optional[CodeService] = None


def init(config: Optional[CodeIntelConfig] = None) -> CodeService:
    global _service
    if _service is None:
        cfg = config or load_code_config()
        _service = CodeService(cfg)
    return _service


def get_service() -> Optional[CodeService]:
    return _service


__all__ = [
    "CodeIndex",
    "CodeIntelConfig",
    "CodeService",
    "get_service",
    "init",
    "load_code_config",
]
