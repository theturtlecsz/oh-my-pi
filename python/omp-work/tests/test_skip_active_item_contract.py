"""OMP-219: skip_active_item command/result discrimination, reason bounds, owner
approval issue, and work.execute-only scope dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import omp_work
from omp_work.v1.api_models import SkipActiveItemResult
from omp_work.v1.models import (
    Approval,
    CommandEnvelope,
    SkipActiveItemCommand,
    SkipActiveItemPayload,
)
from omp_work.v1.service import Principal, WorkError, WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
GRANT = UUID("00000000-0000-7000-8000-000000000011")
WORK = UUID("00000000-0000-7000-8000-000000000012")
JUDGE_SHA = "a" * 64


def _payload(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "grant_id": GRANT,
        "expected_grant_version": 3,
        "position": 0,
        "work_id": WORK,
        "expected_focus_version": 1,
        "judge_sha256": JUDGE_SHA,
        "reason": "owner wants to reorder the queue",
    }
    data.update(updates)
    return data


def test_skip_active_item_command_discriminates_and_carries_payload() -> None:
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": WORKSPACE,
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {"type": "skip_active_item", "payload": _payload()},
        }
    )
    assert isinstance(envelope.command, SkipActiveItemCommand)
    assert envelope.command.type == "skip_active_item"
    assert envelope.command.payload.grant_id == GRANT
    assert envelope.command.payload.position == 0
    assert envelope.command.payload.reason == "owner wants to reorder the queue"


def test_skip_active_item_result_discriminates() -> None:
    result = SkipActiveItemResult.model_validate(
        {
            "type": "skip_active_item",
            "grant": {
                "grant_id": GRANT,
                "workspace_id": WORKSPACE,
                "owner_id": uuid4(),
                "repository": "owner/repo",
                "remote_ref": "refs/heads/main",
                "state": "active",
                "mode": "queue",
                "grant_version": 4,
                "max_continuations": 8,
                "max_close_attempts": 5,
                "max_no_progress": 3,
                "continuations_scheduled": 1,
                "authorization_hash": "b" * 64,
                "judge_sha256": JUDGE_SHA,
                "created_at": datetime(2026, 8, 15, tzinfo=UTC),
                "expires_at": datetime(2026, 8, 22, tzinfo=UTC),
            },
            "item": {
                "item_id": uuid4(),
                "workspace_id": WORKSPACE,
                "grant_id": GRANT,
                "work_id": WORK,
                "position": 0,
                "phase": "pending",
                "claimed_revision_id": uuid4(),
                "initial_git_baseline": "c" * 40,
                "original_request": "do the thing",
                "original_request_sha256": "d" * 64,
                "close_attempts_started": 0,
                "consecutive_no_progress": 0,
            },
            "reason": "owner wants to reorder the queue",
        }
    )
    assert result.type == "skip_active_item"
    assert result.reason == "owner wants to reorder the queue"


def test_skip_active_item_reason_blank_fails() -> None:
    with pytest.raises(ValueError):
        SkipActiveItemPayload.model_validate(_payload(reason=""))
    with pytest.raises(ValueError):
        SkipActiveItemPayload.model_validate(_payload(reason="   "))


def test_skip_active_item_reason_over_240_fails() -> None:
    with pytest.raises(ValueError):
        SkipActiveItemPayload.model_validate(_payload(reason="x" * 241))


def test_skip_active_item_reason_is_trimmed() -> None:
    payload = SkipActiveItemPayload.model_validate(
        _payload(reason="  needs owner attention  ")
    )
    assert payload.reason == "needs owner attention"


def test_skip_active_item_reason_at_240_is_accepted() -> None:
    payload = SkipActiveItemPayload.model_validate(_payload(reason="x" * 240))
    assert len(payload.reason) == 240


def test_approval_accepts_omp_219_issue() -> None:
    approval = Approval.model_validate(
        {
            "contract_version": omp_work.CONTRACT_VERSION,
            "contract_sha256": "e" * 64,
            "approved_by": "owner",
            "approved_at": "2026-09-24T00:00:00Z",
            "issue": "OMP-219",
        }
    )
    assert approval.issue == "OMP-219"


def test_command_types_closure_includes_skip_active_item() -> None:
    contract = omp_work.load_contract()
    assert "skip_active_item" in contract.command_types


def test_skip_active_item_requires_work_execute_scope() -> None:
    service = WorkService(store=None)
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": WORKSPACE,
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {"type": "skip_active_item", "payload": _payload()},
        }
    )
    principal = Principal(
        actor_id=uuid4(),
        actor_kind="owner",
        workspaces=frozenset({WORKSPACE}),
        scopes=frozenset({"work.mutate", "work.close"}),
    )
    with pytest.raises(WorkError) as exc:
        service.execute(principal, envelope)
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403


def test_skip_active_item_scope_is_work_execute() -> None:
    assert WorkService._scopes["skip_active_item"] == "work.execute"
