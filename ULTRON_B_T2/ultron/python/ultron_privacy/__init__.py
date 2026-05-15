"""ultron_privacy — Module N (Privacy Router).

Every byte that leaves the machine — Claude API, Ghost Network, future
cloud services — passes through this module. N classifies data, blocks
LOCAL_ONLY, anonymises where possible.

Public API:
    from ultron_privacy import init, get_service
    svc = init(cfg)                      # done by privacy_service.py
    svc = get_service()                  # used by C and Q
    ok, sys, msgs = await svc.gate_claude_call(system_prompt, messages)
    ok, redacted   = await svc.gate_ghost_export(kind, payload)

If `get_service()` returns None, N is not running — callers must treat
that as "don't send anything outbound".
"""
from __future__ import annotations

from typing import Optional

from .classifier import DataClass, DataClassifier
from .config import PrivacyConfig, load_privacy_config
from .gate import GateDecision, OutboundGate
from .anonymizer import HashAnonymizer
from .service import PrivacyService

_service: Optional[PrivacyService] = None


def init(config: Optional[PrivacyConfig] = None) -> PrivacyService:
    """Initialise the singleton service. Safe to call multiple times — the
    first call wins; later calls return the same instance regardless of any
    `config` they pass."""
    global _service
    if _service is None:
        cfg = config or load_privacy_config()
        _service = PrivacyService(cfg)
    return _service


def get_service() -> Optional[PrivacyService]:
    """Return the initialised service, or None if N isn't running."""
    return _service


__all__ = [
    "DataClass",
    "DataClassifier",
    "GateDecision",
    "HashAnonymizer",
    "OutboundGate",
    "PrivacyConfig",
    "PrivacyService",
    "get_service",
    "init",
    "load_privacy_config",
]
