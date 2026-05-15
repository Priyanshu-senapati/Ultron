"""Sentence-transformers wrapper.

Lazy-loads the model so importing this module is cheap. Returns numpy
arrays of shape (n, dim) for batch embedding.

Default model: `sentence-transformers/all-MiniLM-L6-v2` — 384-dim, ~80MB
on disk, CPU inference ~10ms/sentence on a modern laptop. Good signal
for short technical notes; bigger models are not worth the latency cost
at our corpus size.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger("ultron.knowledge.embedder")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class Embedder:
    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model = None  # loaded on first encode()

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            # Lazy import — keeps import-time cost low and lets the
            # rest of the package load even if sentence-transformers
            # is not installed (retrieval just degrades to "no results").
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "Run: pip install sentence-transformers"
            ) from exc
        logger.info("loading embedding model: %s", self.model_name)
        self._model = SentenceTransformer(self.model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode a batch of texts. Returns (n, dim) float32 array."""
        if not texts:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        self._ensure_model()
        # convert_to_numpy=True → ndarray; normalize_embeddings=True so cosine
        # similarity reduces to a dot product (much faster).
        return self._model.encode(  # type: ignore[union-attr]
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]
