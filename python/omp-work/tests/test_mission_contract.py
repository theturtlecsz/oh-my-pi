"""OMP-413-s01: the mission record contract — envelopes, causes, approval, read.

Without a database: the contract closures must name the mission read, the five
mission commands, and the two error codes; the mission command envelopes must
discriminate and reject a cause id that does not belong to its kind;
``Approval`` must accept the OMP-413 issue; and
``GET /v1/workspaces/{workspace_id}/missions/{mission_id}`` must serve a
``work.read`` principal while refusing one that lacks it.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import omp_work
import pytest
from fastapi.testclient import TestClient
from omp_work import contract_sha256, load_contract
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import (
    Approval,
    ApproveMissionCommand,
    CommandEnvelope,
    LinkMissionWorkCommand,
    MissionDraft,
    MissionStatus,
    ReviseMissionCommand,
    SetMissionStatusCommand,
    SetMissionStatusPayload,
    SubmitMissionCommand,
)
from omp_work.v1.server import create_app
from pydantic import ValidationError

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
MISSION = UUID("00000000-0000-7000-8000-0000000000c1")
DECISION = UUID("00000000-0000-7000-8000-0000000000d1")

MISSION_COMMANDS = (
    "submit_mission",
    "revise_mission",
    "approve_mission",
    "set_mission_status",
    "link_mission_work",
)


class _RecordingStore:
    """Fake WorkStore: records the read call and returns a mission snapshot."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.read_error: str | None = None

    def read(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        kind: str,
        value: str,
        *,
        candidate_allowlist=None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "kind": kind,
                "value": value,
                "allowlist": candidate_allowlist,
            }
        )
        if self.read_error is not None:
            from omp_work.v1.store import WorkStoreError

            raise WorkStoreError(self.read_error, (self.read_error,))
        return {
            "mission_id": value,
            "project_id": str(uuid4()),
            "status": MissionStatus.DRAFT.value,
            "objective": "ship the widget",
        }


def _draft(**overrides: object) -> dict[str, object]:
    draft: dict[str, object] = {
        "project_id": str(uuid4()),
        "objective": "ship the widget",
        "acceptance_criteria": ("widget ships",),
        "risk_policy": "policy-risk",
        "approval_policy": "policy-approval",
        "effort_policy": "policy-effort",
    }
    draft.update(overrides)
    return draft


def _envelope(command_type: str, payload: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": command_type, "payload": payload},
        }
    )


def test_contract_closures_name_the_mission_read_commands_and_errors() -> None:
    contract = load_contract()
    mission_read = "GET /v1/workspaces/{workspace_id}/missions/{mission_id}"
    assert mission_read in omp_work._READS
    assert mission_read in contract.reads
    # Declared immediately before the stop read, so both order pins still hold.
    assert contract.reads[contract.reads.index(mission_read) :][:4] == (
        mission_read,
        "GET /v1/workspaces/{workspace_id}/stop",
        "GET /v1/health/live",
        "GET /v1/health/ready",
    )
    for command in MISSION_COMMANDS:
        assert command in omp_work._COMMAND_TYPES
        assert command in contract.command_types
    for code in ("mission_transition_refused", "mission_budget_exceeded"):
        assert code in omp_work._ERROR_CODES
        assert code in contract.error_codes


def test_mission_status_enum_is_the_d29_set() -> None:
    assert tuple(status.value for status in MissionStatus) == (
        "draft",
        "awaiting_confirmation",
        "approved",
        "running",
        "paused",
        "blocked",
        "completed",
        "failed",
        "abandoned",
    )


def test_envelope_discriminates_mission_commands() -> None:
    submit = _envelope("submit_mission", {"mission_id": str(MISSION), "draft": _draft()})
    assert isinstance(submit.command, SubmitMissionCommand)
    assert submit.command.payload.mission_id == MISSION
    assert submit.command.payload.draft.budget_policy is None
    assert submit.command.payload.draft.priority == 2

    revise = _envelope(
        "revise_mission",
        {
            "mission_id": str(MISSION),
            "base_revision": 1,
            "draft": _draft(),
            "proposed_classification": "material",
        },
    )
    assert isinstance(revise.command, ReviseMissionCommand)
    assert revise.command.payload.proposed_classification == "material"
    assert revise.command.payload.base_revision == 1

    approve = _envelope(
        "approve_mission",
        {
            "mission_id": str(MISSION),
            "revision": 1,
            "basis_kind": "decision",
            "basis_id": str(DECISION),
        },
    )
    assert isinstance(approve.command, ApproveMissionCommand)
    assert approve.command.payload.basis_kind == "decision"

    status = _envelope(
        "set_mission_status",
        {
            "mission_id": str(MISSION),
            "target_status": "running",
            "cause_kind": "policy_rule",
            "policy_rule_id": "rule-42",
        },
    )
    assert isinstance(status.command, SetMissionStatusCommand)
    assert status.command.payload.policy_rule_id == "rule-42"

    link = _envelope(
        "link_mission_work",
        {"mission_id": str(MISSION), "work_id": str(uuid4())},
    )
    assert isinstance(link.command, LinkMissionWorkCommand)


def test_draft_bounds_and_required_policy_ids() -> None:
    assert MissionDraft.model_validate(_draft(objective="x" * 4000)).objective == "x" * 4000
    with pytest.raises(ValidationError):
        MissionDraft.model_validate(_draft(objective="x" * 4001))
    with pytest.raises(ValidationError):
        MissionDraft.model_validate(_draft(objective=""))
    with pytest.raises(ValidationError):
        MissionDraft.model_validate(_draft(risk_policy="x" * 201))
    with pytest.raises(ValidationError):
        MissionDraft.model_validate(_draft(priority=4))
    with pytest.raises(ValidationError):
        MissionDraft.model_validate(_draft(created_by="someone"))


@pytest.mark.parametrize("target", ["draft", "awaiting_confirmation", "approved"])
def test_status_target_refuses_the_three_unsettable_states(target: str) -> None:
    with pytest.raises(ValidationError):
        SetMissionStatusPayload.model_validate(
            {
                "mission_id": MISSION,
                "target_status": target,
                "cause_kind": "principal",
            }
        )


@pytest.mark.parametrize(
    ("cause_kind", "extra"),
    [
        ("principal", {"policy_rule_id": "rule-1"}),
        ("principal", {"decision_id": DECISION}),
        ("policy_rule", {}),
        ("policy_rule", {"decision_id": DECISION}),
        ("decision", {}),
        ("decision", {"policy_rule_id": "rule-1"}),
    ],
)
def test_wrong_cause_ids_are_rejected(cause_kind: str, extra: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "mission_id": MISSION,
        "target_status": "blocked",
        "cause_kind": cause_kind,
    }
    payload.update(extra)
    with pytest.raises(ValidationError):
        SetMissionStatusPayload.model_validate(payload)


@pytest.mark.parametrize(
    ("cause_kind", "extra"),
    [
        ("principal", {}),
        ("policy_rule", {"policy_rule_id": "rule-1"}),
        ("decision", {"decision_id": DECISION}),
    ],
)
def test_each_cause_kind_accepts_its_own_id(cause_kind: str, extra: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "mission_id": MISSION,
        "target_status": "blocked",
        "cause_kind": cause_kind,
    }
    payload.update(extra)
    parsed = SetMissionStatusPayload.model_validate(payload)
    assert parsed.cause_kind == cause_kind
    assert parsed.target_status == "blocked"


def test_approval_accepts_the_omp_413_issue() -> None:
    approval = Approval.model_validate(
        {
            "contract_version": "work.omp.dev/v1",
            "contract_sha256": "0" * 64,
            "approved_by": "owner",
            "approved_at": "2026-09-29T00:00:00+00:00",
            "issue": "OMP-413",
        }
    )
    assert approval.issue == "OMP-413"


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_kind, scopes in (
        ("owner", "owner", ["work.read"]),
        ("plain", "automation", ["work.mutate"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": actor_kind,
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def test_mission_read_serves_work_read_and_refuses_others(tmp_path: Path) -> None:
    capabilities = _capabilities_dir(tmp_path)
    store = _RecordingStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    client = TestClient(
        create_app(config, capabilities_dir=capabilities, store=store)  # type: ignore[arg-type]
    )
    route = f"/v1/workspaces/{WORKSPACE}/missions/{MISSION}"

    def get(token: str):
        return client.get(
            route,
            headers={
                "Authorization": f"Bearer {token}-token",
                "X-OMP-Contract-SHA256": contract_sha256(),
                "X-OMP-Workspace-ID": str(WORKSPACE),
            },
        )

    served = get("owner")
    assert served.status_code == 200
    assert served.json()["mission_id"] == str(MISSION)
    assert served.json()["status"] == MissionStatus.DRAFT.value
    assert store.calls[-1]["kind"] == "mission"
    assert store.calls[-1]["value"] == str(MISSION)

    refused = get("plain")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"


def test_mission_read_failure_maps_the_store_error(tmp_path: Path) -> None:
    capabilities = _capabilities_dir(tmp_path)
    store = _RecordingStore()
    store.read_error = "invalid_request"
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    client = TestClient(
        create_app(config, capabilities_dir=capabilities, store=store)  # type: ignore[arg-type]
    )
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/missions/{MISSION}",
        headers={
            "Authorization": "Bearer owner-token",
            "X-OMP-Contract-SHA256": contract_sha256(),
            "X-OMP-Workspace-ID": str(WORKSPACE),
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
