"""Tool executor — runs a single tool call end-to-end.

Pipeline per call::

    1. Look up tool in registry
    2. Validate args against tool.args_schema
    3. Run privacy gate (N) on args (best-effort — if N unavailable, log)
    4. If tool.confirm_required and no valid confirm token → return
       a PENDING_CONFIRM result; caller (C) must surface to user, then
       re-issue with confirm_token.
    5. Execute handler with timeout
    6. Append audit record to JSONL
    7. Return ExecutionResult

Confirm tokens are short, single-use strings produced by ``request_confirm
_token`` and consumed by ``execute`` (one shot). They expire after
``ToolsConfig.confirm_timeout_seconds``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import ToolsConfig
from .registry import Tool, ToolRegistry
from .schema import validate

logger = logging.getLogger("ultron.tools.executor")


@dataclass
class ExecutionResult:
    ok: bool
    name: str
    request_id: str

    # On success
    result: Any = None

    # On failure
    error: Optional[str] = None
    error_code: str = ""

    # Confirmation path
    pending_confirm: bool = False
    confirm_token: Optional[str] = None
    confirm_reason: str = ""
    confirm_expires_at: float = 0.0

    # Timing
    started_at_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Don't bloat audit logs with handler-return objects we can't JSON
        # — coerce to a safe shape.
        if d["result"] is not None:
            try:
                json.dumps(d["result"])
            except TypeError:
                d["result"] = str(d["result"])[:2000]
        return d


@dataclass
class _PendingConfirm:
    token: str
    name: str
    args: dict[str, Any]
    request_id: str
    expires_at: float


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, config: ToolsConfig) -> None:
        self._registry = registry
        self._cfg = config
        # token → pending call
        self._pending: dict[str, _PendingConfirm] = {}

    # ── Public ──────────────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        request_id: Optional[str] = None,
        confirm_token: Optional[str] = None,
    ) -> ExecutionResult:
        """Run a single tool call. See module docstring for pipeline."""
        rid = request_id or secrets.token_hex(8)
        t0 = time.monotonic()
        tool = self._registry.get(name)
        if tool is None:
            return self._fail(rid, name, "no_such_tool", f"tool {name!r} not registered", t0)

        # 1. Schema validation
        errors = validate(args, tool.args_schema)
        if errors:
            return self._fail(rid, name, "bad_args", "; ".join(errors), t0)

        # 2. Privacy gate (best-effort)
        await self._gate(tool, args)

        # 3. confirm_required check
        if tool.confirm_required:
            valid = self._consume_confirm(confirm_token, name, args)
            if not valid:
                return self._issue_confirm(rid, tool, args, t0)

        # 4. Execute with timeout
        try:
            result = await asyncio.wait_for(
                tool.handler(args),
                timeout=self._cfg.shell_timeout_seconds * 2,
            )
        except asyncio.TimeoutError:
            return self._fail(rid, name, "timeout", "handler exceeded timeout", t0)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s handler raised: %s", name, exc)
            return self._fail(rid, name, "handler_error", str(exc), t0)

        duration = int((time.monotonic() - t0) * 1000)
        out = ExecutionResult(
            ok=True,
            name=name,
            request_id=rid,
            result=result,
            duration_ms=duration,
        )
        self._audit(out)
        return out

    def request_confirm_token(self, name: str, args: dict[str, Any], request_id: str) -> _PendingConfirm:
        token = secrets.token_urlsafe(12)
        expires = time.time() + self._cfg.confirm_timeout_seconds
        pending = _PendingConfirm(
            token=token, name=name, args=args, request_id=request_id, expires_at=expires
        )
        self._pending[token] = pending
        self._sweep_expired()
        return pending

    def list_pending(self) -> list[_PendingConfirm]:
        self._sweep_expired()
        return list(self._pending.values())

    def cancel_pending(self, token: str) -> bool:
        return self._pending.pop(token, None) is not None

    # ── Internals ───────────────────────────────────────────────────────

    async def _gate(self, tool: Tool, args: dict[str, Any]) -> None:
        """Best-effort privacy gate. Doesn't block; if N's redaction
        rejects, we just log — the actual outbound block is N's job at
        the destination (Claude, ghost). Tools are local execution."""
        try:
            from ultron_privacy import get_service as _priv  # type: ignore[import]
            svc = _priv()
            if svc is None:
                return
            decision = await svc.gate_generic(f"tool:{tool.name}", args)
            if decision.redaction_count:
                logger.info(
                    "tool %s args had %d LOCAL_ONLY hits (run continues — local exec)",
                    tool.name,
                    decision.redaction_count,
                )
        except ImportError:
            return

    def _consume_confirm(
        self, token: Optional[str], name: str, args: dict[str, Any]
    ) -> bool:
        if not token:
            return False
        self._sweep_expired()
        pending = self._pending.pop(token, None)
        if pending is None:
            return False
        if pending.name != name:
            logger.warning(
                "confirm token %r reused for wrong tool: had=%s, want=%s",
                token, pending.name, name,
            )
            return False
        return True

    def _issue_confirm(
        self,
        request_id: str,
        tool: Tool,
        args: dict[str, Any],
        t0: float,
    ) -> ExecutionResult:
        pending = self.request_confirm_token(tool.name, args, request_id)
        return ExecutionResult(
            ok=False,
            name=tool.name,
            request_id=request_id,
            pending_confirm=True,
            confirm_token=pending.token,
            confirm_reason=tool.confirm_reason or "this tool requires explicit user approval",
            confirm_expires_at=pending.expires_at,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

    def _sweep_expired(self) -> None:
        now = time.time()
        expired = [tok for tok, p in self._pending.items() if p.expires_at < now]
        for tok in expired:
            self._pending.pop(tok, None)

    def _fail(
        self,
        request_id: str,
        name: str,
        code: str,
        msg: str,
        t0: float,
    ) -> ExecutionResult:
        out = ExecutionResult(
            ok=False,
            name=name,
            request_id=request_id,
            error=msg,
            error_code=code,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        self._audit(out)
        return out

    def _audit(self, result: ExecutionResult) -> None:
        path: Path = self._cfg.audit_log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("audit log write failed: %s", exc)
