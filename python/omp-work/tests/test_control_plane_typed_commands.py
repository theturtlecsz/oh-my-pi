"""WorkService.execute_proposal for typed commands: only owner-signed typed changes reach the store.

A ``typed_command`` proposal is an owner act: the principal must be the owner, and
the proposal is authorised through the same control-plane checks as any other
proposal. These tests pin the two refusal shapes (single-mutation-path and the
tier/lock gates), that a typed command sees exactly the decisions an
``orchestrator`` proposal does when nothing but the principal's kind changes, and
that an allowed envelope reaches the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from omp_work.control_plane.gate import (
    ControlPlaneFacts,
    LeaseClaim,
    LeaseFact,
    MissionFact,
    OwnerAuthorization,
    Proposal,
    ReservationFact,
)
from omp_work.control_plane.registry import ControlPlane
from omp_work.v1.canonical import command_sha256
from omp_work.v1.models import (
    CommandEnvelope,
    FocusSlot,
    ReviseMissionCommand,
    SetFocusCommand,
    SetFocusPayload,
)
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store_shared import WorkStoreError

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("00000000-0000-0000-0000-0000000000aa")
REVISION = 4
SIGNATURE = "owner-sig"
JOB = "job-7"
WORKER = "worker-7"
MISSION = UUID("00000000-0000-0000-0000-0000000000b1")
RESERVATION = "res-1"
SCOPES = frozenset({"work.mutate", "work.execute", "work.approve", "work.stop"})


def _facts(**overrides: object) -> ControlPlaneFacts:
    base: dict[str, object] = {"now": NOW, "current_revision": REVISION}
    base.update(overrides)
    return ControlPlaneFacts(**base)  # type: ignore[arg-type]


def _principal(kind: str) -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind=kind,
        workspaces=frozenset({WORKSPACE}),
        scopes=SCOPES,
    )


class _Store:
    def __init__(self) -> None:
        self.hashes: dict[UUID, str] = {}
        self.decisions: list[CommandEnvelope] = []
        self.executed: list[CommandEnvelope] = []

    def execute(
        self,
        envelope: CommandEnvelope,
        *,
        actor_id: UUID,
        actor_kind: str,
        required_scope: str,
    ):
        del actor_id, actor_kind, required_scope
        digest = command_sha256(envelope)
        previous = self.hashes.get(envelope.operation_id)
        if previous is None:
            self.hashes[envelope.operation_id] = digest
        elif previous != digest:
            raise WorkStoreError("idempotency_conflict")
        else:
            return {"replayed": True}, {"type": envelope.command.type}
        if envelope.command.type == "create_decision":
            self.decisions.append(envelope)
        else:
            self.executed.append(envelope)
        return {"applied": True}, {"type": envelope.command.type}


def _service(store: _Store, facts: ControlPlaneFacts | None = None) -> WorkService:
    held = facts if facts is not None else _facts()
    return WorkService(  # type: ignore[arg-type]
        store,
        ControlPlane(
            lambda _workspace_id: held,
            lambda _message, signature: signature == SIGNATURE,
        ),
    )


def _envelope(command: object, workspace_id: UUID = WORKSPACE) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("command_type", ["intake", "plan", "execute", "summary", "done"])
@pytest.mark.parametrize("operation", ["direct_write", "lease", "dispatch"])
def test_typed_command_never_writes_the_control_plane(
    command_type: str, operation: str
) -> None:
    facts: dict[str, object] = {}
    fields: dict[str, object] = {
        "proposal_id": uuid4(),
        "workspace_id": WORKSPACE,
        "proposer": "typed_command",
        "typed_command": command_type,
        "operation": operation,
        "provenance": "typed_command:trace-1",
        "basis_source": "workservice",
        "basis_revision": REVISION,
        "action_class": "read_state",
    }
    if operation == "lease":
        fields["lease"] = LeaseClaim(job_id=JOB, worker_id=WORKER, fence=3)
        facts["leases"] = {JOB: LeaseFact(worker_id=WORKER, fence=3, expires_at=FUTURE)}
    if operation == "dispatch":
        fields["mission_id"] = MISSION
        fields["effort"] = "E1"
        fields["reservation_id"] = RESERVATION
        fields["cost_usd"] = Decimal("1.00")
        facts["missions"] = {MISSION: MissionFact(approved=True, in_flight=0, capacity=1)}
        facts["reservations"] = {
            RESERVATION: ReservationFact(
                mission_id=MISSION, remaining_usd=Decimal("10.00")
            )
        }
    store = _Store()

    with pytest.raises(WorkError) as caught:
        _service(store, _facts(**facts)).execute_proposal(
            _principal("owner"), Proposal(**fields)  # type: ignore[arg-type]
        )

    error = caught.value
    expected = (
        "single_mutation_path:direct_write_refused"
        if operation == "direct_write"
        else "single_mutation_path:control_plane_operation"
    )
    assert error.code == "control_plane_refused"
    assert error.status == 409
    assert error.diagnostics == (expected,)
    assert store.decisions == []
    assert store.executed == []


def _revise_mission(mission_id: UUID) -> ReviseMissionCommand:
    return ReviseMissionCommand(
        type="revise_mission",
        payload={
            "mission_id": mission_id,
            "base_revision": 1,
            "draft": {
                "project_id": str(uuid4()),
                "objective": "do the thing",
                "risk_policy": "risk-1",
                "approval_policy": "approval-1",
                "effort_policy": "effort-1",
            },
        },
    )


@dataclass(frozen=True)
class Row:
    id: str
    proposal: dict[str, object] = field(default_factory=dict)
    revise: bool = False
    signed: bool = False


_ROWS: tuple[Row, ...] = (
    Row(
        id="unsigned-revise-mission",
        proposal={
            "proposal_id": uuid4(),
            "workspace_id": WORKSPACE,
            "mission_id": uuid4(),
            "action_class": None,
        },
        revise=True,
    ),
    Row(
        id="revise-mission-labelled-update-mission-state",
        proposal={
            "proposal_id": uuid4(),
            "workspace_id": WORKSPACE,
            "mission_id": uuid4(),
            "command_type": "revise_mission",
            "action_class": "update_mission_state",
        },
        revise=True,
    ),
    Row(
        id="lock-4-change",
        proposal={
            "proposal_id": uuid4(),
            "workspace_id": WORKSPACE,
            "operation": "lock_change",
            "lock_id": 4,
            "action_class": None,
        },
        signed=True,
    ),
)


@pytest.mark.parametrize("row", _ROWS, ids=[row.id for row in _ROWS])
def test_typed_command_matches_orchestrator_decisions(row: Row) -> None:
    envelope = (
        _envelope(_revise_mission(row.proposal["mission_id"])) if row.revise else None
    )

    def run(proposer: str, kind: str) -> tuple[WorkError, _Store]:
        fields: dict[str, object] = {
            "proposer": proposer,
            "provenance": f"{proposer}:trace-1",
            "basis_source": "workservice",
            "basis_revision": REVISION,
        }
        if row.signed:
            fields["owner_authorization"] = OwnerAuthorization(
                signature=SIGNATURE, expires_at=FUTURE
            )
        fields.update(row.proposal)
        store = _Store()
        with pytest.raises(WorkError) as caught:
            _service(store).execute_proposal(
                _principal(kind), Proposal(**fields), envelope  # type: ignore[arg-type]
            )
        return caught.value, store

    typed_error, typed_store = run("typed_command", "owner")
    orch_error, orch_store = run("orchestrator", "automation")

    assert typed_error.code == "control_plane_refused"
    assert orch_error.code == typed_error.code
    assert orch_error.diagnostics == typed_error.diagnostics
    assert typed_store.executed == []
    assert orch_store.executed == []
    assert len(orch_store.decisions) == len(typed_store.decisions)
    typed_decisions = [item.command.payload.model_dump() for item in typed_store.decisions]
    orch_decisions = [item.command.payload.model_dump() for item in orch_store.decisions]
    assert typed_decisions == orch_decisions
    for decision in typed_store.decisions:
        # The decision id is uuid5(NAMESPACE_URL, proposal_id), the same in both.
        assert decision.command.payload.decision_id == uuid5(
            NAMESPACE_URL, str(row.proposal["proposal_id"])
        )


def test_typed_command_allowed_set_focus_reaches_the_store() -> None:
    store = _Store()
    work_id = uuid4()
    envelope = _envelope(
        SetFocusCommand(
            type="set_focus",
            payload=SetFocusPayload(
                slot=FocusSlot(
                    workspace_id=WORKSPACE,
                    owner_id=uuid4(),
                    work_id=work_id,
                    version=0,
                ),
                expected_version=0,
            ),
        )
    )
    proposal = Proposal(
        proposal_id=uuid4(),
        workspace_id=WORKSPACE,
        proposer="typed_command",
        typed_command="plan",
        operation="command",
        provenance="typed_command:trace-1",
        basis_source="workservice",
        basis_revision=REVISION,
        action_class=None,
    )

    result = _service(store).execute_proposal(_principal("owner"), proposal, envelope)

    assert result[1]["type"] == "set_focus"  # type: ignore[index]
    assert store.decisions == []
    assert [item.command.type for item in store.executed] == ["set_focus"]
    assert store.executed[0].operation_id == envelope.operation_id
