from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from .api_models import ClientResponse

__all__ = [
    "client_body",
    "client_error_body",
    "is_diagnostic_key",
    "split_diagnostics",
]


def is_diagnostic_key(key: str) -> bool:
    """Return True if key is considered diagnostic:
    lowercase key contains token, worker or worktree,
    or is operation_id, request_id or correlation_id."""
    lower = str(key).lower()
    return (
        "token" in lower
        or "worker" in lower
        or "worktree" in lower
        or lower in ("operation_id", "request_id", "correlation_id")
    )


def split_diagnostics(value: Any) -> tuple[Any, dict[str, Any]]:
    """Walk dicts and lists at every depth; clean drops diagnostic keys;
    found maps each dropped key's dotted path (e.g. 'items.0.worker_id') to its value."""
    found: dict[str, Any] = {}

    def _walk(val: Any, prefix: str) -> Any:
        if hasattr(val, "model_dump"):
            val = val.model_dump(mode="json")
        if isinstance(val, dict):
            clean_dict: dict[str, Any] = {}
            for k, v in val.items():
                key_str = str(k)
                path = f"{prefix}.{key_str}" if prefix else key_str
                if is_diagnostic_key(key_str):
                    if hasattr(v, "model_dump"):
                        v = v.model_dump(mode="json")
                    found[path] = v
                else:
                    clean_dict[k] = _walk(v, path)
            return clean_dict
        elif isinstance(val, list):
            return [_walk(item, f"{prefix}.{i}" if prefix else str(i)) for i, item in enumerate(val)]
        elif isinstance(val, tuple):
            return tuple(_walk(item, f"{prefix}.{i}" if prefix else str(i)) for i, item in enumerate(val))
        return val

    clean = _walk(value, "")
    return clean, found


def client_body(
    operation: str,
    *,
    result: Any = None,
    detail: bool = False,
    outcome: Literal["read", "applied", "replayed", "pending_approval"] = "read",
    state: str | None = None,
    evidence: tuple[str, ...] | list[str] = (),
    blockers: tuple[str, ...] | list[str] = (),
    decisions: tuple[UUID, ...] | list[UUID] = (),
    artifacts: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Build a serialized ClientResponse dict.
    clean drops diagnostic keys; detail is found diagnostics if detail=True else None."""
    clean, found = split_diagnostics(result)
    detail_data = found if detail else None
    response = ClientResponse(
        outcome=outcome,
        state=state,
        evidence=tuple(evidence),
        blockers=tuple(blockers),
        decisions=tuple(decisions),
        artifacts=tuple(artifacts),
        operation=operation,
        result=clean,
        detail=detail_data,
    )
    return response.model_dump(mode="json")


def client_error_body(
    code: str,
    diagnostics: Any = (),
    *,
    request_id: UUID | str | None = None,
    correlation_id: UUID | str | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Build a client error response dict: {"error": {"code", "diagnostics"}},
    plus "detail": {"request_id", "correlation_id"} when detail is True."""
    if isinstance(diagnostics, (list, tuple)):
        diagnostics_list = list(diagnostics)
    elif diagnostics is None:
        diagnostics_list = []
    else:
        diagnostics_list = [str(diagnostics)]

    body: dict[str, Any] = {
        "error": {
            "code": code,
            "diagnostics": diagnostics_list,
        }
    }
    if detail:
        body["detail"] = {
            "request_id": str(request_id) if request_id is not None else None,
            "correlation_id": str(correlation_id) if correlation_id is not None else None,
        }
    return body
