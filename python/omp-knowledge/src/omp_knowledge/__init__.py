from __future__ import annotations

from .config import KnowledgeConfig, load_config
from .engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter, compute_graph_sha256
from .engine.protocol import (
    CorrectResult,
    EngineStatus,
    FactRecord,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RetireResult,
)
from .errors import (
    CancelledError,
    ContractMismatchError,
    ERROR_TAXONOMY,
    EngineTimeoutError,
    EngineUnavailableError,
    ForbiddenError,
    IdempotencyConflictError,
    InvalidRequestError,
    KnowledgeError,
    ScopeViolationError,
    SnapshotNotPublishedError,
    UnauthenticatedError,
)
from .publication import PublicationManager

__version__ = "0.1.0"

__all__ = [
    "COGNEE_AVAILABLE",
    "CancelledError",
    "ContractMismatchError",
    "CorrectResult",
    "ERROR_TAXONOMY",
    "EngineStatus",
    "EngineTimeoutError",
    "EngineUnavailableError",
    "FactRecord",
    "ForbiddenError",
    "IdempotencyConflictError",
    "IngestPlan",
    "IngestResult",
    "InvalidRequestError",
    "KnowledgeConfig",
    "KnowledgeEngine",
    "KnowledgeError",
    "LookupResult",
    "PublicationManager",
    "QueryResult",
    "RealCogneeAdapter",
    "RetireResult",
    "ScopeViolationError",
    "SnapshotNotPublishedError",
    "UnauthenticatedError",
    "__version__",
    "compute_graph_sha256",
    "load_config",
]
