from __future__ import annotations

from typing import Any
from uuid import UUID


class KnowledgeError(Exception):
    """Base error for the knowledge domain error taxonomy."""

    code: str = "knowledge_error"
    status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        super().__init__(message or f"{self.code}: error occurred")


class InvalidRequestError(KnowledgeError):
    """400: Malformed, contradictory, or unparseable knowledge request."""

    code = "invalid_request"
    status_code = 400

    def __init__(self, message: str = "invalid_request: malformed or unparseable request payload") -> None:
        super().__init__(message)


class UnauthenticatedError(KnowledgeError):
    """401: Missing or invalid authentication token/credential."""

    code = "unauthenticated"
    status_code = 401

    def __init__(self, message: str = "unauthenticated: authentication token missing or invalid") -> None:
        super().__init__(message)


class ForbiddenError(KnowledgeError):
    """403: Caller lacks required permission or capability."""

    code = "forbidden"
    status_code = 403

    def __init__(self, message: str = "forbidden: principal lacks required permission") -> None:
        super().__init__(message)


class IdempotencyConflictError(KnowledgeError):
    """409: Duplicate operation_id attempted with differing request payloads/digests."""

    code = "idempotency_conflict"
    status_code = 409

    def __init__(
        self,
        operation_id: UUID,
        current_hash: str,
        existing_hash: str,
        message: str | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.current_hash = current_hash
        self.existing_hash = existing_hash
        msg = (
            message
            or f"idempotency_conflict: payload conflict for operation {operation_id} "
            f"(current={current_hash}, existing={existing_hash})"
        )
        super().__init__(
            msg,
            details={
                "operation_id": str(operation_id),
                "current_hash": current_hash,
                "existing_hash": existing_hash,
            },
        )


class ScopeViolationError(KnowledgeError):
    """403: Target repository or resource does not belong to authorized workspace scope."""

    code = "scope_violation"
    status_code = 403

    def __init__(self, message: str = "scope_violation: resource outside principal's permitted workspace") -> None:
        super().__init__(message)


class SnapshotNotPublishedError(KnowledgeError):
    """400: Queried snapshot has not been published or has been aborted/retired."""

    code = "snapshot_not_published"
    status_code = 400

    def __init__(self, snapshot_id: str, message: str | None = None) -> None:
        self.snapshot_id = snapshot_id
        msg = message or f"snapshot_not_published: snapshot {snapshot_id} has not been published"
        super().__init__(msg, details={"snapshot_id": snapshot_id})


class EngineUnavailableError(KnowledgeError):
    """503: Cognee or Ladybug graph engine dependencies unavailable or offline."""

    code = "engine_unavailable"
    status_code = 503

    def __init__(self, message: str = "engine_unavailable: Cognee or Ladybug dependencies not available") -> None:
        super().__init__(message)


class EngineTimeoutError(KnowledgeError):
    """504: Engine graph operation exceeded timeout threshold."""

    code = "engine_timeout"
    status_code = 504

    def __init__(self, message: str = "engine_timeout: engine operation timed out") -> None:
        super().__init__(message)


class CancelledError(KnowledgeError):
    """409: Requested operation was cancelled prior to or during completion."""

    code = "cancelled"
    status_code = 409

    def __init__(self, message: str = "cancelled: operation was cancelled") -> None:
        super().__init__(message)


class ContractMismatchError(KnowledgeError):
    """400: Contract schema, algorithm identifier, or hash preimage mismatch."""

    code = "contract_mismatch"
    status_code = 400

    def __init__(self, message: str = "contract_mismatch: contract definition or version mismatch") -> None:
        super().__init__(message)


ERROR_TAXONOMY: dict[str, type[KnowledgeError]] = {
    "invalid_request": InvalidRequestError,
    "unauthenticated": UnauthenticatedError,
    "forbidden": ForbiddenError,
    "idempotency_conflict": IdempotencyConflictError,
    "scope_violation": ScopeViolationError,
    "snapshot_not_published": SnapshotNotPublishedError,
    "engine_unavailable": EngineUnavailableError,
    "engine_timeout": EngineTimeoutError,
    "cancelled": CancelledError,
    "contract_mismatch": ContractMismatchError,
}

__all__ = [
    "CancelledError",
    "ContractMismatchError",
    "ERROR_TAXONOMY",
    "EngineTimeoutError",
    "EngineUnavailableError",
    "ForbiddenError",
    "IdempotencyConflictError",
    "InvalidRequestError",
    "KnowledgeError",
    "ScopeViolationError",
    "SnapshotNotPublishedError",
    "UnauthenticatedError",
]
