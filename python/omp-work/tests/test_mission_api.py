"""OMP-413-s04: Mission response views and command scopes.

Without a database:
- CommandResponse validates a MissionResult carrying a full MissionView and rejects extra fields.
- POST each mission command with a principal lacking its required scope -> 403 and the store is not called.
- With the scope, the store is called and receives the mapped required_scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import (
    CommandResponse,
    MissionApprovedScope,
    MissionDrawn,
    MissionLink,
    MissionResult,
    MissionTransition,
    MissionView,
)
from omp_work.v1.models import (
    ItemBudget,
    MissionDraft,
    MissionStatus,
    OperationReceipt,
    OperationState,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")

MISSION_COMMAND_SCOPES = {
    "submit_mission": "work.mutate",
    "revise_mission": "work.mutate",
    "link_mission_work": "work.mutate",
    "approve_mission": "work.approve",
    "set_mission_status": "work.execute",
}


def _full_mission_view_dict(project_id: UUID, mission_id: UUID) -> dict[str, object]:
    return {
        "project_id": str(project_id),
        "objective": "Deliver the full mission lifecycle",
        "constraints": ["in-memory testing only"],
        "acceptance_criteria": ["views validate", "scopes enforce"],
        "context_refs": ["https://example.com/spec/d29"],
        "artifact_expectations": ["api-schema.json"],
        "requested_capabilities": ["cap-execute"],
        "repositories": ["repo-mission"],
        "approval_classes": ["class-d35-tier3"],
        "risk_policy": "risk-policy-default",
        "approval_policy": "approval-policy-default",
        "effort_policy": "effort-policy-default",
        "budget_policy": {
            "usd": "500.00",
            "tokens": 1000000,
            "wall_clock_seconds": 3600,
            "max_subagents": 4,
        },
        "priority": 1,
        "continuation_of": str(uuid4()),
        "parent_mission": str(uuid4()),
        "mission_id": str(mission_id),
        "created_by": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "revision": 2,
        "status": "running",
        "budget": {
            "usd": "500.00",
            "tokens": 1000000,
            "wall_clock_seconds": 3600,
            "max_subagents": 4,
        },
        "budget_source": "mission",
        "hold_decision": {"reason": "none"},
        "approved_scope": {
            "revision": 1,
            "basis_kind": "decision",
            "basis_id": "dec-001",
            "approved_by": str(uuid4()),
            "approved_by_actor_kind": "owner",
            "approved_at": datetime.now(UTC).isoformat(),
            "envelope": {
                "project_id": str(project_id),
                "objective": "Deliver the full mission lifecycle",
                "risk_policy": "risk-policy-default",
                "approval_policy": "approval-policy-default",
                "effort_policy": "effort-policy-default",
            },
        },
        "transitions": [
            {
                "from_status": None,
                "to_status": "draft",
                "cause_kind": "principal",
                "cause_id": "operator",
                "actor_id": str(uuid4()),
                "actor_kind": "operator",
                "at": datetime.now(UTC).isoformat(),
                "revision": 1,
            },
            {
                "from_status": "draft",
                "to_status": "running",
                "cause_kind": "policy_rule",
                "cause_id": "rule-admit",
                "actor_id": str(uuid4()),
                "actor_kind": "system",
                "at": datetime.now(UTC).isoformat(),
                "revision": 2,
            },
        ],
        "links": [
            {
                "work_id": str(uuid4()),
                "budget": {
                    "usd": "50.00",
                    "tokens": 100000,
                    "wall_clock_seconds": 600,
                    "max_subagents": 1,
                },
                "linked_at": datetime.now(UTC).isoformat(),
            }
        ],
        "drawn": {
            "usd": "12.50",
            "tokens": 25000,
            "wall_clock_seconds": 150,
        },
    }


class _RecordingStore:
    def __init__(self, view_dict: dict[str, object]) -> None:
        self.calls: list[dict[str, object]] = []
        self.view_dict = view_dict

    def execute(
        self, envelope, *, actor_id: UUID, actor_kind: str, required_scope: str
    ) -> tuple[OperationReceipt, dict[str, object]]:
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_id": actor_id,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
                "workspace_id": envelope.workspace_id,
            }
        )
        return (
            OperationReceipt(
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                state=OperationState.APPLIED,
                request_sha256="0" * 64,
                result_sha256="1" * 64,
            ),
            {
                "type": envelope.command.type,
                "mission": self.view_dict,
            },
        )


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, scopes in (
        ("mutate", ["work.mutate"]),
        ("approve", ["work.approve"]),
        ("execute", ["work.execute"]),
        ("readonly", ["work.read"]),
        ("unscoped", []),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "automation",
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _client(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities_dir(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def test_service_scopes_map_mission_commands() -> None:
    for cmd_type, expected_scope in MISSION_COMMAND_SCOPES.items():
        assert WorkService._scopes[cmd_type] == expected_scope


def test_command_response_validates_mission_result_full_view() -> None:
    project_id = uuid4()
    mission_id = uuid4()
    view_dict = _full_mission_view_dict(project_id, mission_id)

    receipt = OperationReceipt(
        operation_id=uuid4(),
        request_id=uuid4(),
        state=OperationState.APPLIED,
        request_sha256="0" * 64,
        result_sha256="1" * 64,
    )

    for cmd_type in MISSION_COMMAND_SCOPES:
        response = CommandResponse.model_validate(
            {
                "receipt": receipt.model_dump(mode="json"),
                "result": {
                    "type": cmd_type,
                    "mission": view_dict,
                },
            }
        )
        assert isinstance(response.result, MissionResult)
        assert response.result.type == cmd_type
        mission = response.result.mission
        assert isinstance(mission, MissionView)
        assert mission.mission_id == mission_id
        assert mission.project_id == project_id
        assert mission.status == MissionStatus.RUNNING
        assert mission.revision == 2
        assert mission.budget is not None
        assert mission.budget.usd == "500.00"
        assert mission.budget_source == "mission"
        assert mission.hold_decision == {"reason": "none"}
        assert mission.drawn.usd == "12.50"
        assert mission.drawn.tokens == 25000
        assert mission.drawn.wall_clock_seconds == 150
        assert len(mission.transitions) == 2
        assert isinstance(mission.transitions[0], MissionTransition)
        assert mission.transitions[0].from_status is None
        assert mission.transitions[0].to_status == MissionStatus.DRAFT
        assert len(mission.links) == 1
        assert isinstance(mission.links[0], MissionLink)
        assert mission.links[0].budget.usd == "50.00"
        assert isinstance(mission.approved_scope, MissionApprovedScope)
        assert mission.approved_scope.basis_kind == "decision"
        assert mission.approved_scope.basis_id == "dec-001"
        assert isinstance(mission.approved_scope.envelope, MissionDraft)


def test_command_response_rejects_extra_fields() -> None:
    project_id = uuid4()
    mission_id = uuid4()
    view_dict = _full_mission_view_dict(project_id, mission_id)

    receipt = OperationReceipt(
        operation_id=uuid4(),
        request_id=uuid4(),
        state=OperationState.APPLIED,
        request_sha256="0" * 64,
        result_sha256="1" * 64,
    ).model_dump(mode="json")

    # Extra field on MissionResult
    with pytest.raises(ValidationError):
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": view_dict,
                    "unexpected_result_field": "disallowed",
                },
            }
        )

    # Extra field on MissionView
    with pytest.raises(ValidationError):
        bad_view = {**view_dict, "unexpected_view_field": "disallowed"}
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": bad_view,
                },
            }
        )

    # Extra field on MissionDrawn
    with pytest.raises(ValidationError):
        bad_drawn = {**view_dict["drawn"], "extra_metric": 42}  # type: ignore[dict-item]
        bad_view = {**view_dict, "drawn": bad_drawn}
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": bad_view,
                },
            }
        )

    # Extra field on MissionTransition
    with pytest.raises(ValidationError):
        bad_transition = {**view_dict["transitions"][0], "extra_trans": "x"}  # type: ignore[index]
        bad_view = {**view_dict, "transitions": [bad_transition]}
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": bad_view,
                },
            }
        )

    # Extra field on MissionApprovedScope
    with pytest.raises(ValidationError):
        bad_scope = {**view_dict["approved_scope"], "extra_basis": "x"}  # type: ignore[dict-item]
        bad_view = {**view_dict, "approved_scope": bad_scope}
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": bad_view,
                },
            }
        )

    # Extra field on MissionLink
    with pytest.raises(ValidationError):
        bad_link = {**view_dict["links"][0], "extra_link": "x"}  # type: ignore[index]
        bad_view = {**view_dict, "links": [bad_link]}
        CommandResponse.model_validate(
            {
                "receipt": receipt,
                "result": {
                    "type": "submit_mission",
                    "mission": bad_view,
                },
            }
        )


def _make_command_payload(cmd_type: str, project_id: UUID, mission_id: UUID) -> dict[str, object]:
    if cmd_type == "submit_mission":
        return {
            "mission_id": str(mission_id),
            "draft": {
                "project_id": str(project_id),
                "objective": "Build compiler",
                "risk_policy": "risk-policy-1",
                "approval_policy": "approval-policy-1",
                "effort_policy": "effort-policy-1",
            },
        }
    if cmd_type == "revise_mission":
        return {
            "mission_id": str(mission_id),
            "base_revision": 1,
            "draft": {
                "project_id": str(project_id),
                "objective": "Build compiler v2",
                "risk_policy": "risk-policy-1",
                "approval_policy": "approval-policy-1",
                "effort_policy": "effort-policy-1",
            },
        }
    if cmd_type == "link_mission_work":
        return {
            "mission_id": str(mission_id),
            "work_id": str(uuid4()),
        }
    if cmd_type == "approve_mission":
        return {
            "mission_id": str(mission_id),
            "revision": 1,
            "basis_kind": "decision",
            "basis_id": str(uuid4()),
        }
    if cmd_type == "set_mission_status":
        return {
            "mission_id": str(mission_id),
            "target_status": "running",
            "cause_kind": "principal",
        }
    raise ValueError(f"unknown command type: {cmd_type}")


def test_post_mission_commands_authorization(tmp_path: Path) -> None:
    project_id = uuid4()
    mission_id = uuid4()
    view_dict = _full_mission_view_dict(project_id, mission_id)
    store = _RecordingStore(view_dict)
    client = _client(tmp_path, store)

    token_for_scope = {
        "work.mutate": "mutate-token",
        "work.approve": "approve-token",
        "work.execute": "execute-token",
    }
    unauthorized_tokens_by_scope = {
        "work.mutate": ["approve-token", "execute-token", "readonly-token", "unscoped-token"],
        "work.approve": ["mutate-token", "execute-token", "readonly-token", "unscoped-token"],
        "work.execute": ["mutate-token", "approve-token", "readonly-token", "unscoped-token"],
    }

    for cmd_type, required_scope in MISSION_COMMAND_SCOPES.items():
        payload = _make_command_payload(cmd_type, project_id, mission_id)
        envelope = {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": cmd_type, "payload": payload},
        }

        # 1. POST with principals lacking the required scope -> 403, store not called
        for bad_token in unauthorized_tokens_by_scope[required_scope]:
            store.calls.clear()
            refused = client.post(
                "/v1/commands",
                headers={
                    "Authorization": f"Bearer {bad_token}",
                    "X-OMP-Contract-SHA256": contract_sha256(),
                    "X-OMP-Workspace-ID": str(WORKSPACE),
                },
                json=envelope,
            )
            assert refused.status_code == 403
            assert refused.json()["error"]["code"] == "forbidden"
            assert store.calls == [], f"store must not be called when {bad_token} posts {cmd_type}"

        # 2. POST with principal holding the required scope -> 200, store sees required_scope
        store.calls.clear()
        good_token = token_for_scope[required_scope]
        allowed = client.post(
            "/v1/commands",
            headers={
                "Authorization": f"Bearer {good_token}",
                "X-OMP-Contract-SHA256": contract_sha256(),
                "X-OMP-Workspace-ID": str(WORKSPACE),
            },
            json=envelope,
        )
        assert allowed.status_code == 200, f"expected 200 for {good_token} on {cmd_type}: {allowed.text}"
        assert len(store.calls) == 1
        assert store.calls[0]["command_type"] == cmd_type
        assert store.calls[0]["required_scope"] == required_scope
        response = CommandResponse.model_validate(allowed.json())
        assert isinstance(response.result, MissionResult)
        assert response.result.type == cmd_type
