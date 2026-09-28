"""Local inference clients for fleet knowledge."""

from omp_knowledge.inference.embedding import (
    Embedder,
    EmbeddingProfile,
    EmbeddingUnavailable,
    HttpEmbedder,
)
from omp_knowledge.inference.routes import (
    PROBE_TIMEOUT_S,
    Accelerator,
    ResolvedRoute,
    RouteConfigError,
    RouteEntry,
    RouteHealth,
    RouteProfile,
    RouteProvider,
    RouteRole,
    RouteSelection,
    RouteSet,
    RouteUnavailable,
    load_routes,
    probe,
    resolve,
)

__all__ = [
    "PROBE_TIMEOUT_S",
    "Accelerator",
    "Embedder",
    "EmbeddingProfile",
    "EmbeddingUnavailable",
    "HttpEmbedder",
    "ResolvedRoute",
    "RouteConfigError",
    "RouteEntry",
    "RouteHealth",
    "RouteProfile",
    "RouteProvider",
    "RouteRole",
    "RouteSelection",
    "RouteSet",
    "RouteUnavailable",
    "load_routes",
    "probe",
    "resolve",
]
