"""Tests for ClientResponse model, diagnostic splitting, and response bodies (OMP-416-s01-s01)."""

from __future__ import annotations

from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from omp_work.v1.api_models import ClientResponse
from omp_work.v1.client_response import (
    client_body,
    client_error_body,
    is_diagnostic_key,
    split_diagnostics,
)


def test_client_response_body_key_order() -> None:
    expected_order = [
        "outcome",
        "state",
        "evidence",
        "blockers",
        "decisions",
        "artifacts",
        "operation",
        "contract",
        "result",
        "detail",
    ]
    resp = ClientResponse(operation="project.list", outcome="read")
    dumped = resp.model_dump(mode="json")
    assert list(dumped.keys()) == expected_order

    decision_id = uuid4()
    body = client_body(
        "mission.status",
        outcome="applied",
        state="running",
        evidence=("rec-1",),
        blockers=("blocker-1",),
        decisions=(decision_id,),
        artifacts=("art-1",),
        result={"title": "test"},
        detail=True,
    )
    assert list(body.keys()) == expected_order
    assert body["outcome"] == "applied"
    assert body["state"] == "running"
    assert body["evidence"] == ["rec-1"]
    assert body["blockers"] == ["blocker-1"]
    assert body["decisions"] == [str(decision_id)]
    assert body["artifacts"] == ["art-1"]
    assert body["operation"] == "mission.status"
    assert body["contract"] == "client.omp.dev/v1"
    assert body["result"] == {"title": "test"}
    assert body["detail"] == {}


def test_client_response_refusals() -> None:
    # "rejected" outcome refused
    with pytest.raises(ValidationError):
        ClientResponse(operation="project.list", outcome="rejected")  # type: ignore[arg-type]

    # "work.omp.dev/v1" contract refused
    with pytest.raises(ValidationError):
        ClientResponse(
            operation="project.list",
            outcome="read",
            contract="work.omp.dev/v1",  # type: ignore[arg-type]
        )

    # Empty operation refused (minLength 1)
    with pytest.raises(ValidationError):
        ClientResponse(operation="", outcome="read")


def test_is_diagnostic_key() -> None:
    # Truthy diagnostic keys
    assert is_diagnostic_key("worker_id")
    assert is_diagnostic_key("WORKER_ID")
    assert is_diagnostic_key("worker")
    assert is_diagnostic_key("max_tokens")
    assert is_diagnostic_key("token")
    assert is_diagnostic_key("tokens")
    assert is_diagnostic_key("budget_tokens")
    assert is_diagnostic_key("worktree_path")
    assert is_diagnostic_key("worktree")
    assert is_diagnostic_key("operation_id")
    assert is_diagnostic_key("request_id")
    assert is_diagnostic_key("correlation_id")

    # Falsy non-diagnostic keys
    assert not is_diagnostic_key("operation")
    assert not is_diagnostic_key("request")
    assert not is_diagnostic_key("correlation")
    assert not is_diagnostic_key("project_id")
    assert not is_diagnostic_key("mission_id")
    assert not is_diagnostic_key("workspace_id")
    assert not is_diagnostic_key("decision_id")
    assert not is_diagnostic_key("receipt_id")
    assert not is_diagnostic_key("title")
    assert not is_diagnostic_key("status")
    assert not is_diagnostic_key("state")


def test_result_diagnostics_kept_none_by_default_and_reported_on_detail() -> None:
    raw_result = {
        "title": "Alpha Project",
        "status": "in_progress",
        "worker_id": "worker-42",
        "budget": {
            "max_tokens": 100000,
            "currency": "USD",
        },
        "items": [
            {"worktree_path": "/tmp/wt1", "branch": "feature"},
        ],
        "operation_id": "op-789",
    }

    # 1. By default (detail=False), keeps no diagnostic keys at any depth; title and status stay
    default_body = client_body("mission.status", result=raw_result, detail=False)
    assert default_body["detail"] is None
    clean = default_body["result"]
    assert clean is not None
    assert clean["title"] == "Alpha Project"
    assert clean["status"] == "in_progress"
    assert "worker_id" not in clean
    assert "operation_id" not in clean
    assert clean["budget"] == {"currency": "USD"}
    assert "max_tokens" not in clean["budget"]
    assert clean["items"] == [{"branch": "feature"}]
    assert "worktree_path" not in clean["items"][0]

    # Verify no diagnostic keys exist at any depth
    def _assert_no_diagnostics(val: object) -> None:
        if isinstance(val, dict):
            for k, v in val.items():
                assert not is_diagnostic_key(k), f"Unexpected diagnostic key in clean result: {k}"
                _assert_no_diagnostics(v)
        elif isinstance(val, list):
            for item in val:
                _assert_no_diagnostics(item)

    _assert_no_diagnostics(clean)

    # 2. With detail=True, reports each diagnostic key by dotted path
    detailed_body = client_body("mission.status", result=raw_result, detail=True)
    assert detailed_body["result"] == clean
    detail = detailed_body["detail"]
    assert detail == {
        "worker_id": "worker-42",
        "budget.max_tokens": 100000,
        "items.0.worktree_path": "/tmp/wt1",
        "operation_id": "op-789",
    }


def test_client_error_body() -> None:
    req_id = uuid4()
    corr_id = uuid4()

    # Without detail: no request_id, correlation_id, or detail key
    err_default = client_error_body(
        "invalid_request",
        ("parameter missing",),
        request_id=req_id,
        correlation_id=corr_id,
        detail=False,
    )
    assert err_default == {
        "error": {
            "code": "invalid_request",
            "diagnostics": ["parameter missing"],
        }
    }
    assert "detail" not in err_default
    assert "request_id" not in err_default
    assert "correlation_id" not in err_default
    assert "request_id" not in err_default["error"]
    assert "correlation_id" not in err_default["error"]

    # With detail: detail key reports request_id and correlation_id
    err_detailed = client_error_body(
        "invalid_request",
        ("parameter missing",),
        request_id=req_id,
        correlation_id=corr_id,
        detail=True,
    )
    assert err_detailed == {
        "error": {
            "code": "invalid_request",
            "diagnostics": ["parameter missing"],
        },
        "detail": {
            "request_id": str(req_id),
            "correlation_id": str(corr_id),
        },
    }
    assert "request_id" not in err_detailed["error"]
    assert "correlation_id" not in err_detailed["error"]


def test_client_response_all_valid_outcomes_and_none_result() -> None:
    for outcome in ("read", "applied", "replayed", "pending_approval"):
        body = client_body("stop.status", outcome=outcome, result=None, detail=False)  # type: ignore[arg-type]
        assert body["outcome"] == outcome
        assert body["result"] is None
        assert body["detail"] is None

        detailed = client_body("stop.status", outcome=outcome, result=None, detail=True)  # type: ignore[arg-type]
        assert detailed["outcome"] == outcome
        assert detailed["result"] is None
        assert detailed["detail"] == {}

