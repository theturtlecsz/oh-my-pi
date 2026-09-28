"""Local inference clients for fleet knowledge."""

from omp_knowledge.inference.embedding import (
    Embedder,
    EmbeddingProfile,
    EmbeddingUnavailable,
    HttpEmbedder,
)

__all__ = [
    "Embedder",
    "EmbeddingProfile",
    "EmbeddingUnavailable",
    "HttpEmbedder",
]
