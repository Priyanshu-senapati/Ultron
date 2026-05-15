"""Tests for Module G (Code Intelligence)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ultron_code.config import CodeIntelConfig
from ultron_code.index import CodeIndex
from ultron_code.parser import extract, extract_python, extract_regex
from ultron_code.scanner import iter_source_files


# ── Parser ───────────────────────────────────────────────────────────────


def test_python_parser_extracts_functions_and_classes() -> None:
    src = textwrap.dedent("""
        def hello():
            return 1

        async def afoo(x):
            return x

        class Bar:
            def method(self):
                pass

            async def amethod(self):
                pass
        """)
    syms = extract_python(src)
    names = {(s.name, s.kind, s.parent) for s in syms}
    assert ("hello", "function", "") in names
    assert ("afoo", "function", "") in names
    assert ("Bar", "class", "") in names
    assert ("method", "method", "Bar") in names
    assert ("amethod", "method", "Bar") in names


def test_python_parser_handles_syntax_error() -> None:
    syms = extract_python("def broken(:\n    pass")
    assert syms == []


def test_rust_regex_extracts_fn_and_struct() -> None:
    src = textwrap.dedent("""
        pub fn add(a: i32, b: i32) -> i32 { a + b }

        async fn fetch() -> Result<()> { Ok(()) }

        struct Point {
            x: f64,
        }

        pub trait Shape { fn area(&self) -> f64; }
        """)
    syms = extract_regex(src, "rust")
    kinds = {(s.name, s.kind) for s in syms}
    assert ("add", "function") in kinds
    assert ("fetch", "function") in kinds
    assert ("Point", "struct") in kinds
    assert ("Shape", "trait") in kinds


def test_typescript_regex_picks_class_and_iface() -> None:
    src = textwrap.dedent("""
        export class Foo {}
        export interface IBar { x: number }
        const arrow = async () => 1;
        function bare() {}
        """)
    syms = extract_regex(src, "typescript")
    names = {(s.name, s.kind) for s in syms}
    assert ("Foo", "class") in names
    assert ("IBar", "interface") in names
    assert ("bare", "function") in names


def test_extract_dispatches_by_language() -> None:
    py_syms = extract("def x(): pass", "python")
    assert any(s.name == "x" for s in py_syms)
    rs_syms = extract("fn y() {}", "rust")
    assert any(s.name == "y" for s in rs_syms)
    none = extract("anything at all", "fortran")
    assert none == []


# ── Scanner ──────────────────────────────────────────────────────────────


def test_scanner_finds_source_files_and_skips_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a(): pass")
    (tmp_path / "b.rs").write_text("fn b() {}")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    sub = tmp_path / "node_modules" / "skip_me"
    sub.mkdir(parents=True)
    (sub / "junk.js").write_text("function junk(){}")
    cfg = CodeIntelConfig(
        ws_url="ws://x", ws_token="t",
        roots=(tmp_path,),
        db_path=tmp_path / "code.db",
    )
    found = list(iter_source_files(cfg))
    names = {f.path.name for f in found}
    assert "a.py" in names
    assert "b.rs" in names
    assert "data.bin" not in names
    assert "junk.js" not in names


# ── Index ────────────────────────────────────────────────────────────────


@pytest.fixture
def index(tmp_path: Path) -> CodeIndex:
    (tmp_path / "alpha.py").write_text(textwrap.dedent("""
        def alpha_one():
            pass

        class Alpha:
            def m(self): pass
    """))
    (tmp_path / "beta.py").write_text(textwrap.dedent("""
        def beta_one():
            pass
    """))
    cfg = CodeIntelConfig(
        ws_url="ws://x", ws_token="t",
        roots=(tmp_path,),
        db_path=tmp_path / "code.db",
    )
    return CodeIndex(cfg)


def test_index_rebuild_inserts_files(index: CodeIndex) -> None:
    stats = index.rebuild(full=True)
    assert stats.scanned == 2
    assert stats.inserted == 2
    assert stats.symbols >= 4  # 2 funcs + 1 class + 1 method


def test_index_find_symbol(index: CodeIndex) -> None:
    index.rebuild(full=True)
    rows = index.find_symbol("alpha_one")
    assert len(rows) == 1
    assert rows[0]["kind"] == "function"
    assert rows[0]["line"] >= 1


def test_index_search_symbols(index: CodeIndex) -> None:
    index.rebuild(full=True)
    rows = index.search_symbols("alpha")
    names = {r["name"] for r in rows}
    assert {"alpha_one", "Alpha"} <= names


def test_index_list_files(index: CodeIndex) -> None:
    index.rebuild(full=True)
    rows = index.list_files(language="python")
    assert len(rows) == 2
    assert all(r["language"] == "python" for r in rows)


def test_index_stats(index: CodeIndex) -> None:
    index.rebuild(full=True)
    s = index.stats()
    assert s["files"] == 2
    assert s["symbols"] >= 4
    assert any(l["language"] == "python" for l in s["languages"])


def test_index_incremental_skips_unchanged(index: CodeIndex, tmp_path: Path) -> None:
    first = index.rebuild(full=True)
    second = index.rebuild(full=False)
    # No files changed → second pass updates and inserts nothing.
    assert second.scanned == first.scanned
    assert second.inserted == 0
    assert second.updated == 0


def test_index_prunes_deleted(index: CodeIndex, tmp_path: Path) -> None:
    index.rebuild(full=True)
    (tmp_path / "alpha.py").unlink()
    stats = index.rebuild(full=False)
    assert stats.pruned == 1
    assert index.find_symbol("alpha_one") == []


# ── Singleton ────────────────────────────────────────────────────────────


def test_code_service_singleton(tmp_path: Path) -> None:
    from ultron_code import get_service, init
    import ultron_code
    ultron_code._service = None  # noqa: SLF001
    cfg = CodeIntelConfig(
        ws_url="ws://127.0.0.1:9420/ws",
        ws_token="t",
        roots=(tmp_path,),
        db_path=tmp_path / "code.db",
    )
    a = init(cfg)
    b = init(cfg)
    c = get_service()
    assert a is b is c
    ultron_code._service = None  # noqa: SLF001
