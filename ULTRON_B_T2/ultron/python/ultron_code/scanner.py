"""Repo scanner — walks configured roots, yields source files.

We deliberately don't honour ``.gitignore`` (full parsing is large and
unreliable across shells). Instead we use a static directory blacklist
plus an extension allow-list. That's enough for indexing source files
the user actually wrote.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import CodeIntelConfig


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    language: str
    size: int
    mtime: float


def iter_source_files(config: CodeIntelConfig) -> Iterator[ScannedFile]:
    """Yield every recognised source file under every configured root."""
    ext_map = config.language_map
    ignore_dirs = set(config.ignore_dirs)
    for root in config.roots:
        root = root.resolve()
        if not root.exists() or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Mutate dirnames in-place to skip ignored dirs.
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                lang = ext_map.get(ext)
                if lang is None:
                    continue
                fpath = Path(dirpath) / fn
                try:
                    stat = fpath.stat()
                except OSError:
                    continue
                if stat.st_size > config.max_file_bytes:
                    continue
                yield ScannedFile(
                    path=fpath,
                    language=lang,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
