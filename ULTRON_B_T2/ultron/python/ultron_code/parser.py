"""Language-aware symbol extraction.

Python: full AST walk (functions, async functions, classes).
Other languages: regex-based heuristics that cover the common shapes
(``fn`` / ``func`` / ``function`` / ``def`` / ``class``).

Each extractor returns a list of ``Symbol`` records the indexer will
persist.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str        # function | method | class | const | other
    line: int        # 1-indexed
    end_line: int    # 1-indexed
    signature: str = ""
    parent: str = ""  # enclosing class for methods


def extract_python(source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    syms: list[Symbol] = []

    def _signature(node: ast.AST) -> str:
        try:
            return ast.unparse(node).splitlines()[0][:200]
        except Exception:  # noqa: BLE001
            return ""

    def _walk(node: ast.AST, parent: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "function"
                syms.append(Symbol(
                    name=child.name,
                    kind=kind,
                    line=child.lineno,
                    end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    signature=_signature(child).split(":", 1)[0],
                    parent=parent,
                ))
                _walk(child, parent)  # nested defs are still globally visible
            elif isinstance(child, ast.ClassDef):
                syms.append(Symbol(
                    name=child.name,
                    kind="class",
                    line=child.lineno,
                    end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    signature=_signature(child).split(":", 1)[0],
                    parent=parent,
                ))
                _walk(child, child.name)
            else:
                _walk(child, parent)

    _walk(tree)
    return syms


# Each regex has one capture group → the symbol name.
_REGEX_LANG_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "rust": [
        ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("struct",   re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("enum",     re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("trait",    re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("impl",     re.compile(r"^\s*impl(?:\s*<[^>]+>)?\s+(?:[^\{]+for\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ],
    "go": [
        ("function", re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("type",     re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ],
    "javascript": [
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>", re.MULTILINE)),
        ("class",    re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
    ],
    "typescript": [
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[:=]", re.MULTILINE)),
        ("class",    re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
        ("type",     re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][A-Za-z0-9_$]*)", re.MULTILINE)),
    ],
    "java": [
        ("class",    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ],
    "csharp": [
        ("class",    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|sealed\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|async\s+|virtual\s+)+(?:[A-Za-z_][A-Za-z0-9_<>,\s\[\]]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)),
    ],
    "cpp": [
        ("class",    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
        ("struct",   re.compile(r"^\s*struct\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ],
    "shell": [
        ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.MULTILINE)),
    ],
    "powershell": [
        ("function", re.compile(r"^\s*function\s+([A-Za-z][A-Za-z0-9_\-]*)", re.MULTILINE | re.IGNORECASE)),
    ],
}


def extract_regex(source: str, language: str) -> list[Symbol]:
    patterns = _REGEX_LANG_PATTERNS.get(language)
    if not patterns:
        return []
    lines = source.splitlines(keepends=False)
    line_offsets: list[int] = [0]
    pos = 0
    for ln in lines:
        pos += len(ln) + 1
        line_offsets.append(pos)
    syms: list[Symbol] = []
    seen: set[tuple[str, str, int]] = set()
    for kind, pat in patterns:
        for m in pat.finditer(source):
            name = m.group(1)
            offset = m.start(1)
            # Binary search the line containing this offset (simple linear is fine here).
            line_no = 1
            for i, o in enumerate(line_offsets):
                if o > offset:
                    line_no = i
                    break
                line_no = i + 1
            key = (name, kind, line_no)
            if key in seen:
                continue
            seen.add(key)
            sig_line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            syms.append(Symbol(
                name=name,
                kind=kind,
                line=line_no,
                end_line=line_no,
                signature=sig_line.strip()[:200],
            ))
    return syms


def extract(source: str, language: str) -> list[Symbol]:
    """Top-level dispatcher. Returns [] when language has no extractor."""
    if language == "python":
        return extract_python(source)
    return extract_regex(source, language)
