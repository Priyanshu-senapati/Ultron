"""kg_indexer.py — one-shot CLI to index the user's knowledge dir.

Usage:
    python python/kg_indexer.py
    python python/kg_indexer.py --watch

`--watch` re-indexes whenever a file under `%APPDATA%/ULTRON/knowledge/`
changes (polled every 10s; cheap because the indexer skips unchanged
files by mtime).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from ultron_knowledge import KnowledgeIndexer, KnowledgeStore


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or os.path.expanduser("~")) / "ULTRON"


def main() -> int:
    ap = argparse.ArgumentParser(description="Index ULTRON's local knowledge corpus.")
    ap.add_argument("--watch", action="store_true", help="re-index on file changes (polling)")
    ap.add_argument("--root", type=Path, default=None, help="override knowledge directory")
    ap.add_argument("--db", type=Path, default=None, help="override database path")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("ULTRON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root = args.root or (_appdata() / "knowledge")
    db = args.db or (_appdata() / "data" / "knowledge.db")
    root.mkdir(parents=True, exist_ok=True)

    store = KnowledgeStore(db)
    indexer = KnowledgeIndexer(store)

    files, chunks, deleted = indexer.index_directory(root)
    print(f"indexed {files} file(s), {chunks} chunk(s), pruned {deleted} stale file(s)")

    if not args.watch:
        return 0

    print(f"watching {root} (poll every 10s) — Ctrl+C to stop")
    last_check_files: set[Path] = set()
    try:
        while True:
            time.sleep(10)
            files, chunks, deleted = indexer.index_directory(root)
            if files or deleted:
                print(f"  + indexed {files}, chunks {chunks}, pruned {deleted}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
