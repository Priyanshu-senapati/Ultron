"""PrivacyService — the runtime owner of the OutboundGate.

Two roles:

1. **Python API** for callers in-process (Module C calls
   `gate_claude_call`; Q calls `gate_ghost_export`).

2. **WS subscriber** for cross-process gating. Subscribes to:
     - `claude_request_pending` — C publishes before each Claude call
     - `ghost_export_candidate` — Q publishes before each peer broadcast
   And publishes `gate_decision` events for the HUD / audit.

The in-process API path is the primary one. The WS path exists so
future Rust sidecars can route through N without depending on Python.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ultron_bridge import UltronBridge

from .anonymizer import HashAnonymizer
from .classifier import DataClassifier
from .config import PrivacyConfig
from .gate import GateDecision, OutboundGate

logger = logging.getLogger("ultron.privacy.service")


class PrivacyService:
    def __init__(self, config: PrivacyConfig) -> None:
        self._cfg = config
        self._classifier = DataClassifier(config.local_only_patterns)
        self._anonymizer = HashAnonymizer(config.anonymizer_salt)
        self._gate = OutboundGate(
            classifier=self._classifier,
            anonymizer=self._anonymizer,
            audit_log_path=config.audit_log_path,
            log_every_n=config.log_every_n_gates,
        )
        self._bridge: UltronBridge | None = None

    # ── Public Python API ────────────────────────────────────────────────

    async def gate_claude_call(
        self, system_prompt: str, messages: list[dict]
    ) -> tuple[bool, str, list[dict]]:
        """Run a Claude API call through the gate.

        Returns (allowed, cleaned_system_prompt, cleaned_messages).
        If `allowed` is False, callers MUST NOT make the upstream call.
        """
        decision = self._gate.approve_claude_call(system_prompt, messages)
        await self._publish_decision(decision)
        if not decision.allowed:
            return (False, "", [])
        payload = decision.redacted_payload or {}
        return (
            True,
            payload.get("system_prompt", system_prompt),
            payload.get("messages", messages),
        )

    async def gate_ghost_export(
        self, kind: str, payload: dict
    ) -> tuple[bool, dict]:
        """Run a Ghost Network export through the gate.

        Returns (allowed, redacted_payload). Always allowed currently —
        we redact rather than refuse to maximise ghost sync usefulness.
        """
        decision = self._gate.approve_ghost_export(kind, payload)
        await self._publish_decision(decision)
        return (decision.allowed, decision.redacted_payload or {})

    async def gate_generic(self, destination: str, payload: dict) -> GateDecision:
        """Generic gate for arbitrary destinations. Returns full decision."""
        decision = self._gate.check(destination, payload)
        await self._publish_decision(decision)
        return decision

    # ── WS subscriber ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the WS subscriber. Idempotent — multiple calls are no-ops
        because UltronBridge.run_forever is the single source of truth."""
        if not self._cfg.ws_token:
            raise RuntimeError(
                "bridge.token not set in config.toml — cannot start privacy service"
            )
        self._bridge = UltronBridge(
            url=self._cfg.ws_url,
            token=self._cfg.ws_token,
            on_event=self._handle_event,
            subscribe_to=["claude_request_pending", "ghost_export_candidate"],
            role="privacy",
        )
        logger.info(
            "PrivacyService starting — patterns=%d audit=%s log_every=%d",
            len(self._cfg.local_only_patterns),
            self._cfg.audit_log_path,
            self._cfg.log_every_n_gates,
        )
        await self._bridge.run_forever()

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload") or {}
        if kind == "claude_request_pending":
            await self.gate_claude_call(
                str(payload.get("system_prompt", "")),
                payload.get("messages") or [],
            )
        elif kind == "ghost_export_candidate":
            await self.gate_ghost_export(
                str(payload.get("kind", "unknown")),
                payload.get("payload") or {},
            )

    # ── Internal ─────────────────────────────────────────────────────────

    async def _publish_decision(self, decision: GateDecision) -> None:
        """Emit a `gate_decision` event for the HUD / audit consumers.

        Only publishes if a live WS bridge exists — otherwise the
        decision still went through the in-process API path and was
        recorded in the audit JSONL by the gate itself.
        """
        if self._bridge is None:
            return
        d = decision.to_dict()
        # Drop the redacted_payload — consumers (HUD, audit) only need metadata.
        d.pop("redacted_payload", None)
        try:
            await self._bridge.publish("gate_decision", d)
        except Exception as exc:  # noqa: BLE001
            logger.debug("gate_decision publish dropped: %s", exc)
