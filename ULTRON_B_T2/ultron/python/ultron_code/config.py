"""Config for Module G (Code Intelligence)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import]
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _ultron_data_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "ULTRON"


# Source file extensions we recognise. Mapping → language tag.
DEFAULT_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".toml": "toml",
    ".md": "markdown",
}


# Directory names we always skip.
DEFAULT_IGNORE_DIRS: tuple[str, ...] = (
    ".git", ".hg", ".svn", "__pycache__",
    "node_modules", "target", "dist", "build", ".venv", "venv", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cargo",
    "site-packages", ".idea", ".vscode",
)


@dataclass
class CodeIntelConfig:
    ws_url: str
    ws_token: str

    # Roots to index. Each is scanned recursively.
    roots: tuple[Path, ...] = field(default_factory=lambda: (Path("C:/dev"),))

    # Output DB
    db_path: Path = field(default_factory=lambda: _ultron_data_dir() / "data" / "code.db")

    language_map: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_LANGUAGE_MAP)
    )
    ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS

    # Hard cap on a single file's size (bytes) we'll parse. Bigger → skip.
    max_file_bytes: int = 1_000_000

    # Re-index trigger: how often (seconds) the service may rescan when
    # explicitly asked. The scanner uses mtime checks so a full rescan is
    # cheap once warmed up.
    rescan_min_interval_seconds: int = 30


def load_code_config(config_path: Path | None = None) -> CodeIntelConfig:
    data_dir = _ultron_data_dir()
    config_path = config_path or (data_dir / "config.toml")

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    bridge = raw.get("bridge", {}) if isinstance(raw.get("bridge"), dict) else {}
    ws_token = bridge.get("token") or os.environ.get("ULTRON_TOKEN", "")
    ws_url = f"ws://{bridge.get('bind', '127.0.0.1:9420')}/ws"

    c = raw.get("code", {}) if isinstance(raw.get("code"), dict) else {}
    roots_raw = c.get("roots") or ["C:/dev"]
    if isinstance(roots_raw, str):
        roots_raw = [roots_raw]
    roots = tuple(Path(r) for r in roots_raw)

    return CodeIntelConfig(
        ws_url=ws_url,
        ws_token=ws_token,
        roots=roots,
        db_path=Path(str(c.get("db_path", data_dir / "data" / "code.db"))),
        ignore_dirs=tuple(c.get("ignore_dirs", DEFAULT_IGNORE_DIRS)),
        max_file_bytes=int(c.get("max_file_bytes", 1_000_000)),
        rescan_min_interval_seconds=int(c.get("rescan_min_interval_seconds", 30)),
    )
