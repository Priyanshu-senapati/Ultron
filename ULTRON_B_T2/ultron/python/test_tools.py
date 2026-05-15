"""Tests for Module E (Tool Registry).

All unit tests — no WS bridge, no live network, no shell side-effects on
files outside the temp sandbox.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ultron_tools.config import ToolsConfig
from ultron_tools.executor import ExecutionResult, ToolExecutor
from ultron_tools.registry import Tool, ToolRegistry
from ultron_tools.schema import SchemaError, validate
from ultron_tools.builtin import (
    delete_file as delete_file_mod,
    read_file as read_file_mod,
    shell as shell_mod,
    write_file as write_file_mod,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(sandbox: Path) -> ToolsConfig:
    return ToolsConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="test",
        confirm_required_tools=("shell", "write_file", "delete_file"),
        confirm_timeout_seconds=60,
        audit_log_path=sandbox / "audit.jsonl",
        sandbox_root=sandbox,
        shell_max_output_bytes=4096,
        shell_timeout_seconds=10,
    )


@pytest.fixture
def registry(config: ToolsConfig) -> ToolRegistry:
    r = ToolRegistry()
    for tool in (
        read_file_mod.build(config),
        write_file_mod.build(config),
        delete_file_mod.build(config),
        shell_mod.build(config),
    ):
        r.register(tool)
    return r


@pytest.fixture
def executor(registry: ToolRegistry, config: ToolsConfig) -> ToolExecutor:
    return ToolExecutor(registry, config)


# ── Schema ───────────────────────────────────────────────────────────────


def test_schema_object_valid() -> None:
    s = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    assert validate({"x": 5}, s) == []


def test_schema_object_missing_required() -> None:
    s = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    errs = validate({}, s)
    assert any("missing required field" in e for e in errs)


def test_schema_string_enum() -> None:
    s = {"type": "string", "enum": ["a", "b"]}
    assert validate("a", s) == []
    assert validate("c", s) != []


def test_schema_integer_bounds() -> None:
    s = {"type": "integer", "minimum": 1, "maximum": 10}
    assert validate(5, s) == []
    assert validate(0, s) != []
    assert validate(11, s) != []
    # booleans are not integers per our policy
    assert validate(True, s) != []


# ── Registry ─────────────────────────────────────────────────────────────


def test_registry_register_and_lookup() -> None:
    r = ToolRegistry()
    assert "noop" not in r

    async def h(_args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    r.register(Tool(
        name="noop",
        description="d",
        args_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=h,
    ))
    assert "noop" in r
    assert r.get("noop") is not None
    assert r.get("missing") is None


def test_registry_descriptor_omits_handler(registry: ToolRegistry) -> None:
    descriptors = [t.to_descriptor() for t in registry.list()]
    assert all("handler" not in d for d in descriptors)
    # Each descriptor has the required public fields.
    for d in descriptors:
        assert {"name", "description", "category", "confirm_required", "args_schema"} <= d.keys()


# ── Executor: schema gate ────────────────────────────────────────────────


def test_execute_unknown_tool_returns_no_such_tool(executor: ToolExecutor) -> None:
    res = asyncio.run(executor.execute("does_not_exist", {}))
    assert res.ok is False
    assert res.error_code == "no_such_tool"


def test_execute_bad_args_returns_bad_args(executor: ToolExecutor) -> None:
    res = asyncio.run(executor.execute("read_file", {}))
    assert res.ok is False
    assert res.error_code == "bad_args"


# ── Executor: confirm gate ───────────────────────────────────────────────


def test_confirm_required_tool_returns_pending(executor: ToolExecutor, sandbox: Path) -> None:
    res = asyncio.run(executor.execute(
        "write_file",
        {"path": "a.txt", "content": "hi"},
    ))
    assert res.ok is False
    assert res.pending_confirm is True
    assert res.confirm_token is not None
    assert "approval" in res.confirm_reason.lower() or "approve" in res.confirm_reason.lower() or res.confirm_reason


def test_confirm_token_unlocks_write(executor: ToolExecutor, sandbox: Path) -> None:
    pending = asyncio.run(executor.execute("write_file", {"path": "a.txt", "content": "hi"}))
    assert pending.pending_confirm

    # Re-issue with the same token → must execute.
    final = asyncio.run(executor.execute(
        "write_file",
        {"path": "a.txt", "content": "hi"},
        confirm_token=pending.confirm_token,
    ))
    assert final.ok is True, final.error
    assert (sandbox / "a.txt").read_text() == "hi"


def test_confirm_token_is_single_use(executor: ToolExecutor) -> None:
    p = asyncio.run(executor.execute("write_file", {"path": "a.txt", "content": "hi"}))
    asyncio.run(executor.execute(
        "write_file", {"path": "a.txt", "content": "hi"},
        confirm_token=p.confirm_token,
    ))
    # Second use → token is gone, new pending issued.
    res = asyncio.run(executor.execute(
        "write_file", {"path": "a.txt", "content": "hi"},
        confirm_token=p.confirm_token,
    ))
    assert res.pending_confirm is True
    assert res.confirm_token != p.confirm_token


def test_confirm_token_does_not_cross_tools(executor: ToolExecutor, sandbox: Path) -> None:
    (sandbox / "x.txt").write_text("y")
    p = asyncio.run(executor.execute("write_file", {"path": "a.txt", "content": "hi"}))
    # Try to use the write_file token for delete_file — must be rejected.
    res = asyncio.run(executor.execute(
        "delete_file", {"path": "x.txt"},
        confirm_token=p.confirm_token,
    ))
    assert res.pending_confirm is True
    assert res.confirm_token != p.confirm_token


# ── Read/write/delete sandbox boundary ───────────────────────────────────


def test_read_file_blocks_path_traversal(executor: ToolExecutor) -> None:
    res = asyncio.run(executor.execute(
        "read_file", {"path": "../../../etc/passwd"},
    ))
    assert res.ok is False
    assert "sandbox" in (res.error or "").lower() or res.error_code == "handler_error"


def test_read_file_round_trip(executor: ToolExecutor, sandbox: Path) -> None:
    target = sandbox / "hello.txt"
    target.write_text("world", encoding="utf-8")
    res = asyncio.run(executor.execute("read_file", {"path": "hello.txt"}))
    assert res.ok is True
    assert res.result["content"] == "world"


def test_delete_file_refuses_directory(executor: ToolExecutor, sandbox: Path) -> None:
    sub = sandbox / "sub"
    sub.mkdir()
    p = asyncio.run(executor.execute("delete_file", {"path": "sub"}))
    res = asyncio.run(executor.execute(
        "delete_file", {"path": "sub"},
        confirm_token=p.confirm_token,
    ))
    assert res.ok is False
    assert res.error_code == "handler_error"


# ── Audit ────────────────────────────────────────────────────────────────


def test_audit_log_jsonl(executor: ToolExecutor, sandbox: Path) -> None:
    asyncio.run(executor.execute("does_not_exist", {}))
    audit = sandbox / "audit.jsonl"
    assert audit.exists()
    lines = [l for l in audit.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    last = json.loads(lines[-1])
    assert last["ok"] is False
    assert last["error_code"] == "no_such_tool"


# ── Pending confirm sweep ────────────────────────────────────────────────


def test_pending_confirm_listing_and_cancel(executor: ToolExecutor) -> None:
    p = asyncio.run(executor.execute("write_file", {"path": "a.txt", "content": "hi"}))
    pending = executor.list_pending()
    assert any(x.token == p.confirm_token for x in pending)
    assert executor.cancel_pending(p.confirm_token) is True
    assert not any(x.token == p.confirm_token for x in executor.list_pending())


# ── Singleton ────────────────────────────────────────────────────────────


def test_init_returns_same_instance(monkeypatch: pytest.MonkeyPatch, sandbox: Path) -> None:
    """ultron_tools.init() is idempotent."""
    from ultron_tools import get_service, init

    # Reset the singleton state for an isolated test
    import ultron_tools
    ultron_tools._service = None  # noqa: SLF001

    cfg = ToolsConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="t",
        audit_log_path=sandbox / "audit.jsonl",
        sandbox_root=sandbox,
    )
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b
    assert a is c
    ultron_tools._service = None  # noqa: SLF001
