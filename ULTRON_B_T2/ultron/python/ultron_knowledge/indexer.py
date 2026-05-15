"""KnowledgeIndexer — walk a directory of .md notes, embed, persist.

Incremental: skips files whose mtime matches what's already in the
store. Run periodically or manually after editing notes.

Idempotent: re-running with no changes does nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .chunker import chunk_markdown
from .embedder import Embedder
from .store import KnowledgeStore

logger = logging.getLogger("ultron.knowledge.indexer")


class KnowledgeIndexer:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Optional[Embedder] = None,
    ) -> None:
        self.store = store
        self.embedder = embedder or Embedder()

    def index_path(self, file_path: Path) -> int:
        """Index a single .md file. Returns # chunks written (0 if skipped)."""
        if not file_path.exists() or file_path.suffix.lower() not in {".md", ".markdown"}:
            return 0

        mtime = file_path.stat().st_mtime
        stored_mtime = self.store.file_mtime(str(file_path))
        if stored_mtime is not None and abs(stored_mtime - mtime) < 1e-6:
            return 0  # unchanged

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("could not read %s: %s", file_path, exc)
            return 0

        chunks = chunk_markdown(content)
        if not chunks:
            self.store.delete_file(str(file_path))
            return 0

        texts = [c.text for c in chunks]
        # Encoding for the embedder is the chunk_text prefixed by the heading
        # path — gives the embedding context about *where* the snippet lives
        # without polluting the stored chunk text we hand the LLM.
        encode_inputs = [f"{c.heading_path}\n\n{c.text}" for c in chunks]
        embeddings = self.embedder.encode(encode_inputs)
        rows = [
            (chunks[i].text, chunks[i].heading_path, embeddings[i])
            for i in range(len(chunks))
        ]
        self.store.replace_file(str(file_path), mtime, rows)
        logger.info("indexed %s: %d chunks", file_path, len(chunks))
        return len(chunks)

    def index_directory(self, root: Path) -> tuple[int, int, int]:
        """Walk `root`, index all .md files.

        Returns (files_indexed, chunks_written, files_deleted).
        """
        if not root.exists():
            logger.warning("knowledge dir does not exist: %s", root)
            return (0, 0, 0)

        # Track existing files on disk so we can prune the DB.
        on_disk: set[str] = set()
        files_indexed = 0
        chunks_written = 0
        for md in root.rglob("*.md"):
            on_disk.add(str(md))
            n = self.index_path(md)
            if n > 0:
                files_indexed += 1
                chunks_written += n

        # Prune deleted files.
        files_deleted = 0
        for stored in list(self.store.all_indexed_files()):
            if not Path(stored).is_relative_to(root):
                continue
            if stored not in on_disk:
                self.store.delete_file(stored)
                files_deleted += 1

        return (files_indexed, chunks_written, files_deleted)


def index_directory(
    root: Path,
    db_path: Path,
) -> tuple[int, int, int]:
    """Convenience: build indexer, index a directory, return stats."""
    store = KnowledgeStore(db_path)
    indexer = KnowledgeIndexer(store)
    return indexer.index_directory(root)
