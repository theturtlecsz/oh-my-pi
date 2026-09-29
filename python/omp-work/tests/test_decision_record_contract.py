"""OMP-414: decision records — contract closure, payload rules, owner-only answer.

Without a database: the contract closures must name the new read and the
``create_decision`` / ``answer_decision`` commands, the payload must demand the
why/risk fields and a per-option risk, only the owner may answer, and
``GET /v1/workspaces/{workspace_id}/decisions`` must forward its filters while
refusing an unknown ``status`` and a principal without ``work.read``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import omp_work
from omp_work import contract_sha256, load_contract
from omp_work.action_tiers import TIER3, tier_of
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import DecisionsPage
from omp_work.v1.models import (
    AnswerDecisionCommand,
    CommandEnvelope,
    CreateDecisionCommand,
    DecisionActionClass,
    OperationReceipt,
    OperationState,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import Principal, WorkError, WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
DECISION = UUID("00000000-0000-7000-8000-000000000101")
PROJECT = UUID("00000000-0000-7000-8000-000000000102")


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision_id": str(DECISION),
        "project_id": str(PROJECT),
        "mission_id": "OMP-414",
        "question": "Publish the intake externally?",
        "why_it_matters": "The mission cannot continue without a ruling.",
        "risk_of_delay": "The window closes and the mission stalls.",
        "options": ("publish", "hold"),
        "evidence_refs": ("receipt:abc",),
        "default_if_any": "hold",
        "risk_of_each_choice": {
            "publish": "Irreversible external exposure.",
            "hold": "Missed deadline.",
        },
        "action_class": "publish_as_owner",
        "resume_state": "awaiting-publication",
    }
    base.update(overrides)
    return base


class _RecordingStore:
    """Fake WorkStore: records the read filters and the execute call scope."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.reads: list[dict[str, object]] = []
        self.decisions_page: dict[str, object] = {"decisions": ()}

    def execute(
        self, envelope, *, actor_id, actor_kind, required_scope
    ) -> tuple[OperationReceipt, dict[str, object]]:
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
            }
        )
        payload = envelope.command.payload
        if envelope.command.type == "create_decision":
            return (
                OperationReceipt(
                    operation_id=envelope.operation_id,
                    request_id=envelope.request_id,
                    state=OperationState.APPLIED,
                    request_sha256="0" * 64,
                    result_sha256="1" * 64,
                ),
                {
                    "type": "create_decision",
                    "decision_id": str(payload.decision_id),
                    "project_id": str(payload.project_id),
                    "mission_id": payload.mission_id,
                    "action_class": payload.action_class,
                    "created_at": "2026-09-29T00:00:00+00:00",
                },
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
                "type": "answer_decision",
                "decision_id": str(payload.decision_id),
                "mission_id": "OMP-414",
                "answer": payload.answer,
                "resume_state": "awaiting-publication",
            },
        )

    def decisions(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        *,
        status: str | None = None,
        project_id: UUID | None = None,
        mission_id: str | None = None,
        after: tuple[object, UUID] | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        self.reads.append(
            {
                "workspace_id": workspace_id,
                "status": status,
                "project_id": project_id,
                "mission_id": mission_id,
                "after": after,
                "limit": limit,
            }
        )
        return self.decisions_page


def _principal(principal_kind: str, scopes: frozenset[str]) -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind=principal_kind,
        workspaces=frozenset({WORKSPACE}),
        scopes=scopes,
    )


def _envelope(command: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


def test_contract_closures_name_the_decision_read_and_commands() -> None:
    contract = load_contract()
    read = "GET /v1/workspaces/{workspace_id}/decisions"
    assert read in omp_work._READS
    assert read in contract.reads
    # Declared before the mission/stop tail, so the FK-1 tail stays pinned.
    assert contract.reads.index(read) < contract.reads.index(
        "GET /v1/workspaces/{workspace_id}/missions/{mission_id}"
    )
    assert contract.reads[-4:] == (
        "GET /v1/work-items/{key}/revisions/{selector}",
        "GET /v1/receipts/{receipt_id}",
        "GET /v1/workspaces/{workspace_id}/work-items",
        "GET /v1/workspaces/{workspace_id}/events",
    )
    for command in ("create_decision", "answer_decision"):
        assert command in omp_work._COMMAND_TYPES
        assert command in contract.command_types


def test_decision_scope_mapping_and_owner_only_answer_constant() -> None:
    assert WorkService._scopes["create_decision"] == "work.mutate"
    assert WorkService._scopes["answer_decision"] == "work.approve"


def test_decision_action_classes_are_the_d35_tier3_ids() -> None:
    classes = set(get_args(DecisionActionClass))
    # The ten named classes are exactly the tier table's tier-3 ids.
    assert classes - {"contract_hash", "unlisted"} == set(TIER3)
    assert all(tier_of(action_class) == 3 for action_class in classes)
    # A non-null action_class names a real tier-3 class, so it must validate.
    payload = _payload(action_class="production_deploy")
    command = CreateDecisionCommand.model_validate(
        {"type": "create_decision", "payload": payload}
    )
    assert command.payload.action_class == "production_deploy"
    # A renamed class the tier table does not know is refused, not silently unlisted.
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {
                "type": "create_decision",
                "payload": _payload(action_class="production_deployment"),
            }
        )


def test_envelope_discriminates_decision_commands() -> None:
    create = _envelope({"type": "create_decision", "payload": _payload()})
    assert isinstance(create.command, CreateDecisionCommand)
    assert create.command.payload.options == ("publish", "hold")
    assert create.command.payload.action_class == "publish_as_owner"
    answer = _envelope(
        {"type": "answer_decision", "payload": {"decision_id": str(DECISION), "answer": "publish"}}
    )
    assert isinstance(answer.command, AnswerDecisionCommand)
    assert answer.command.payload.answer == "publish"
    assert answer.command.payload.owner_signature is None


def test_payload_bounds_and_risk_exhaustiveness() -> None:
    payload = CreateDecisionCommand.model_validate(
        {"type": "create_decision", "payload": _payload()}
    ).payload
    assert payload.risk_of_each_choice == {
        "publish": "Irreversible external exposure.",
        "hold": "Missed deadline.",
    }
    # A single option is not a decision.
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {
                "type": "create_decision",
                "payload": _payload(options=("publish",), risk_of_each_choice={"publish": "x"}),
            }
        )
    # Duplicate options are not a bounded choice set.
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {
                "type": "create_decision",
                "payload": _payload(
                    options=("publish", "publish"),
                    risk_of_each_choice={"publish": "x"},
                ),
            }
        )
    # default_if_any must name one of the options.
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {"type": "create_decision", "payload": _payload(default_if_any="maybe")}
        )


def test_missing_why_it_matters_rejected() -> None:
    payload = _payload()
    payload.pop("why_it_matters")
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate({"type": "create_decision", "payload": payload})


def test_missing_risk_of_delay_rejected() -> None:
    payload = _payload()
    payload.pop("risk_of_delay")
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate({"type": "create_decision", "payload": payload})


def test_missing_option_risk_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {
                "type": "create_decision",
                "payload": _payload(risk_of_each_choice={"publish": "Irreversible."}),
            }
        )
    with pytest.raises(ValidationError):
        CreateDecisionCommand.model_validate(
            {
                "type": "create_decision",
                "payload": _payload(
                    risk_of_each_choice={"publish": "Irreversible.", "hold": "   "}
                ),
            }
        )


def test_automation_may_create_but_never_answers() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    automation = _principal("automation", frozenset({"work.mutate", "work.approve"}))

    _, created = service.execute(
        automation, _envelope({"type": "create_decision", "payload": _payload()})
    )
    assert created["type"] == "create_decision"
    assert store.calls[-1]["required_scope"] == "work.mutate"

    with pytest.raises(WorkError) as denied:
        service.execute(
            automation,
            _envelope(
                {
                    "type": "answer_decision",
                    "payload": {"decision_id": str(DECISION), "answer": "publish"},
                }
            ),
        )
    assert denied.value.code == "forbidden"
    assert denied.value.status == 403

    owner = _principal("owner", frozenset({"work.approve"}))
    _, answered = service.execute(
        owner,
        _envelope(
            {
                "type": "answer_decision",
                "payload": {"decision_id": str(DECISION), "answer": "publish"},
            }
        ),
    )
    assert answered["answer"] == "publish"
    assert answered["resume_state"] == "awaiting-publication"
    assert store.calls[-1]["required_scope"] == "work.approve"


def test_decisions_read_requires_work_read() -> None:
    service = WorkService(_RecordingStore())  # type: ignore[arg-type]
    with pytest.raises(WorkError) as denied:
        service.decisions(_principal("automation", frozenset({"work.mutate"})), WORKSPACE)
    assert denied.value.code == "forbidden"
    assert denied.value.status == 403


def test_decisions_read_rejects_unknown_status() -> None:
    service = WorkService(_RecordingStore())  # type: ignore[arg-type]
    with pytest.raises(WorkError) as denied:
        service.decisions(
            _principal("owner", frozenset({"work.read"})), WORKSPACE, status="bogus"
        )
    assert denied.value.code == "invalid_request"
    assert denied.value.status == 400


def test_decisions_read_forwards_filters() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    view = service.decisions(
        _principal("owner", frozenset({"work.read"})),
        WORKSPACE,
        status="pending",
        project_id=PROJECT,
        mission_id="OMP-414",
        after=(None, None),
        limit=7,
    )
    assert DecisionsPage.model_validate(view).decisions == ()
    assert store.reads[-1] == {
        "workspace_id": WORKSPACE,
        "status": "pending",
        "project_id": PROJECT,
        "mission_id": "OMP-414",
        "after": (None, None),
        "limit": 7,
    }


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_kind, scopes in (
        ("owner", "owner", ["work.read", "work.mutate", "work.approve"]),
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


def test_decisions_read_serves_owner_and_refuses_plain(tmp_path: Path) -> None:
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
    route = f"/v1/workspaces/{WORKSPACE}/decisions"

    def get(token: str, **params: str):
        return client.get(
            route,
            params=params,
            headers={
                "Authorization": f"Bearer {token}-token",
                "X-OMP-Contract-SHA256": contract_sha256(),
                "X-OMP-Workspace-ID": str(WORKSPACE),
            },
        )

    served = get("owner", status="pending", mission_id="OMP-414", limit="7")
    assert served.status_code == 200
    assert DecisionsPage.model_validate(served.json()).decisions == ()
    assert store.reads[-1]["status"] == "pending"
    assert store.reads[-1]["mission_id"] == "OMP-414"
    assert store.reads[-1]["limit"] == 7

    bad_status = get("owner", status="bogus")
    assert bad_status.status_code == 400
    assert bad_status.json()["error"]["code"] == "invalid_request"

    refused = get("plain")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
