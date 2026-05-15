"""ultron_knowledge — local curated knowledge graph.

Walks `%APPDATA%/ULTRON/knowledge/**/*.md`, chunks each note by heading,
embeds with sentence-transformers locally, persists to a SQLite store,
and exposes a `KnowledgeRetriever` that Module C uses to inject relevant
notes into the LLM prompt.

Designed for ~hundreds of small markdown notes — not Wikipedia. Brute-
force cosine search in numpy is fast enough at this scale; we don't
bring in a vector-db library.
"""
from .indexer import KnowledgeIndexer, index_directory
from .retriever import KnowledgeRetriever, RetrievedChunk
from .store import KnowledgeStore

__all__ = [
    "KnowledgeIndexer",
    "KnowledgeRetriever",
    "KnowledgeStore",
    "RetrievedChunk",
    "index_directory",
]
