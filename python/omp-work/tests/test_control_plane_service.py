"""WorkService.execute_proposal: the control plane decides before any command runs."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from omp_work.control_plane.adr0004 import contract_freeze, high_risk_review
from omp_work.control_plane.envelope import tier_gate
from omp_work.control_plane.gate import (
    ControlPlaneFacts,
    LeaseClaim,
    LeaseFact,
    MissionFact,
    OwnerAuthorization,
    Proposal,
)
from omp_work.control_plane.locks import lock_integrity
from omp_work.control_plane.mutation import (
    authoritative_state,
    provenance_present,
    single_mutation_path,
    worker_lifecycle,
)
from omp_work.control_plane.registry import (
    CONTROL_PLANE_CHECKS,
    INVARIANT_CHECKS,
    ControlPlane,
)
from omp_work.control_plane.work_gates import (
    acceptance_semantics,
    admission_control,
    paid_work_gate,
)
from omp_work.v1.canonical import command_sha256
from omp_work.v1.models import (
    CommandEnvelope,
    EngageStopCommand,
    ReleaseStopCommand,
    ReviseMissionCommand,
    SetMissionStatusCommand,
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
CHECK_NAMES = [check.name for check in CONTROL_PLANE_CHECKS]


def _facts(**overrides: object) -> ControlPlaneFacts:
    base: dict[str, object] = {"now": NOW, "current_revision": REVISION}
    base.update(overrides)
    return ControlPlaneFacts(**base)  # type: ignore[arg-type]


def _proposal(**overrides: object) -> Proposal:
    base: dict[str, object] = {
        "proposal_id": uuid4(),
        "workspace_id": WORKSPACE,
        "proposer": "model",
        "operation": "command",
        "provenance": "model:trace-1",
        "basis_source": "workservice",
        "basis_revision": REVISION,
        "action_class": "read_state",
    }
    base.update(overrides)
    return Proposal(**base)  # type: ignore[arg-type]


def _principal(
    kind: str = "automation",
    scopes: frozenset[str] | set[str] | None = None,
    workspaces: frozenset[UUID] | set[UUID] | None = None,
) -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind=kind,
        workspaces=frozenset(workspaces if workspaces is not None else {WORKSPACE}),
        scopes=frozenset(scopes if scopes is not None else {"work.stop", "work.execute", "work.mutate", "work.approve"}),
    )


def _signed(**overrides: object) -> Proposal:
    return _proposal(
        owner_authorization=OwnerAuthorization(signature=SIGNATURE, expires_at=FUTURE),
        **overrides,
    )


class _Store:
    """Keys command_sha256 by operation_id. A second hash for that id conflicts."""

    def __init__(self) -> None:
        self.hashes: dict[UUID, str] = {}
        self.decisions: list[CommandEnvelope] = []
        self.executed: list[CommandEnvelope] = []
        self.attempts: list[tuple[UUID, str]] = []
        self.scopes: list[str] = []
        self.fail_next: WorkStoreError | None = None

    def execute(self, envelope: CommandEnvelope, *, actor_id: UUID, actor_kind: str, required_scope: str):
        del actor_id, actor_kind
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error
        digest = command_sha256(envelope)
        self.attempts.append((envelope.operation_id, digest))
        self.scopes.append(required_scope)
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


class _Plane:
    def __init__(
        self,
        facts: ControlPlaneFacts | None = None,
        *,
        budget_seconds: float = 10.0,
        clock: object | None = None,
        raises: bool = False,
    ) -> None:
        self.seen: list[UUID] = []
        self._facts = facts if facts is not None else _facts()
        self.raises = raises
        self.plane = ControlPlane(
            self._load,
            lambda _message, signature: signature == SIGNATURE,
            budget_seconds,
            clock,  # type: ignore[arg-type]
        )

    def _load(self, workspace_id: UUID) -> ControlPlaneFacts:
        self.seen.append(workspace_id)
        if self.raises:
            raise RuntimeError("facts unavailable")
        return self._facts


def _service(store: _Store, plane: _Plane | None = None) -> WorkService:
    held = plane if plane is not None else _Plane()
    return WorkService(store, held.plane)  # type: ignore[arg-type]


def _envelope(command: object, workspace_id: UUID = WORKSPACE) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,  # type: ignore[arg-type]
    )


def _engage() -> EngageStopCommand:
    return EngageStopCommand(type="engage_stop", payload={"reason": "hold"})


def _mission_draft() -> dict[str, object]:
    return {
        "project_id": str(uuid4()),
        "objective": "do the thing",
        "risk_policy": "risk-1",
        "approval_policy": "approval-1",
        "effort_policy": "effort-1",
    }


def _revise(mission_id: UUID) -> ReviseMissionCommand:
    return ReviseMissionCommand(
        type="revise_mission",
        payload={
            "mission_id": mission_id,
            "base_revision": 1,
            "draft": _mission_draft(),
        },
    )


def _abandoned(mission_id: UUID) -> SetMissionStatusCommand:
    return SetMissionStatusCommand(
        type="set_mission_status",
        payload={
            "mission_id": mission_id,
            "target_status": "abandoned",
            "cause_kind": "principal",
        },
    )


def _refused(
    service: WorkService,
    principal: Principal,
    proposal: Proposal,
    envelope: CommandEnvelope | None = None,
) -> WorkError:
    with pytest.raises(WorkError) as caught:
        service.execute_proposal(principal, proposal, envelope)
    return caught.value


def _decision_id(proposal: Proposal) -> UUID:
    return uuid5(NAMESPACE_URL, str(proposal.proposal_id))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_fixes_the_eleven_checks_and_maps_invariants() -> None:
    assert CONTROL_PLANE_CHECKS == (
        single_mutation_path,
        provenance_present,
        authoritative_state,
        worker_lifecycle,
        admission_control,
        paid_work_gate,
        acceptance_semantics,
        high_risk_review,
        contract_freeze,
        tier_gate,
        lock_integrity,
    )
    assert [check.name for check in CONTROL_PLANE_CHECKS] == [
        "single_mutation_path",
        "provenance_present",
        "authoritative_state",
        "worker_lifecycle",
        "admission_control",
        "paid_work_gate",
        "acceptance_semantics",
        "high_risk_review",
        "contract_freeze",
        "tier_gate",
        "lock_integrity",
    ]
    assert set(INVARIANT_CHECKS.values()) == set(CHECK_NAMES)
    assert INVARIANT_CHECKS["Single mutation authority"] == "single_mutation_path"
    assert INVARIANT_CHECKS["Sole mutator"] == "single_mutation_path"
    assert INVARIANT_CHECKS["Provenance"] == "provenance_present"
    assert INVARIANT_CHECKS["Authoritative state"] == "authoritative_state"
    assert INVARIANT_CHECKS["Worker lifecycle"] == "worker_lifecycle"
    assert INVARIANT_CHECKS["Admission control"] == "admission_control"
    assert INVARIANT_CHECKS["Effort launch-gate"] == "admission_control"
    assert INVARIANT_CHECKS["Budget policy"] == "paid_work_gate"
    assert INVARIANT_CHECKS["Acceptance semantics"] == "acceptance_semantics"
    assert INVARIANT_CHECKS["Contract freeze"] == "contract_freeze"
    assert INVARIANT_CHECKS["Job API freeze"] == "contract_freeze"
    assert INVARIANT_CHECKS["reviewer_required"] == "high_risk_review"
    assert INVARIANT_CHECKS["Protected-action gate"] == "tier_gate"
    assert INVARIANT_CHECKS["Fail closed"] == "tier_gate"
    assert INVARIANT_CHECKS["Lock integrity"] == "lock_integrity"
    with pytest.raises(TypeError):
        INVARIANT_CHECKS["Provenance"] = "other"  # type: ignore[index]


def test_control_plane_is_a_frozen_positional_facts_handle() -> None:
    def facts(_workspace_id: UUID) -> ControlPlaneFacts:
        return _facts()

    def verify(_message: bytes, _signature: str) -> bool:
        return True

    def clock() -> float:
        return 1.0

    plane = ControlPlane(facts, verify, 2.5, clock)
    assert plane.facts is facts
    assert plane.verify_signature is verify
    assert plane.budget_seconds == 2.5
    assert plane.clock is clock
    with pytest.raises(FrozenInstanceError):
        plane.budget_seconds = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_model_check_violation_does_not_run_and_stores_one_decision() -> None:
    store = _Store()
    service = _service(store)
    proposal = _proposal(provenance=None, action_class=None)
    envelope = _envelope(_engage())
    # work.stop would run the command; it cannot record a decision by itself.
    principal = _principal(scopes={"work.stop"})
    error = _refused(service, principal, proposal, envelope)
    decision = _decision_id(proposal)
    assert error.code == "control_plane_refused"
    assert error.status == 409
    assert error.diagnostics == (
        "provenance_present:provenance_missing",
        f"decision:{decision}",
    )
    assert store.executed == []
    assert len(store.decisions) == 1
    recorded = store.decisions[0]
    assert recorded.command.type == "create_decision"
    assert recorded.operation_id == decision
    assert recorded.request_id == decision
    assert recorded.correlation_id == decision
    assert recorded.command.payload.decision_id == decision
    assert store.scopes == ["work.mutate"]
    assert command_sha256(recorded) == store.attempts[0][1]


def test_direct_write_makes_no_store_call() -> None:
    store = _Store()
    service = _service(store)
    proposal = _proposal(operation="direct_write")
    error = _refused(service, _principal(), proposal)
    assert error.code == "control_plane_refused"
    assert error.status == 409
    assert error.diagnostics == ("single_mutation_path:direct_write_refused",)
    assert store.attempts == []
    assert store.decisions == []
    assert store.executed == []


def test_replayed_refusal_stores_one_decision_and_hashes_match() -> None:
    store = _Store()
    service = _service(store)
    proposal = _proposal(provenance=None, action_class=None)
    envelope = _envelope(_engage())
    principal = _principal(scopes={"work.stop"})
    first = _refused(service, principal, proposal, envelope)
    second = _refused(service, principal, proposal, envelope)
    assert first.diagnostics == second.diagnostics
    assert len(store.decisions) == 1
    assert len(store.attempts) == 2
    assert store.attempts[0] == store.attempts[1]
    assert store.attempts[0][0] == _decision_id(proposal)
    assert store.executed == []


def test_idempotency_conflict_and_decision_exists_count_as_recorded() -> None:
    proposal = _proposal(provenance=None, action_class=None)
    envelope = _envelope(_engage())
    principal = _principal(scopes={"work.stop"})
    decision = _decision_id(proposal)

    conflict = _Store()
    conflict.hashes[decision] = "f" * 64
    error = _refused(_service(conflict), principal, proposal, envelope)
    assert error.code == "control_plane_refused"
    assert f"decision:{decision}" in error.diagnostics
    assert conflict.decisions == []

    exists = _Store()
    exists.fail_next = WorkStoreError("revision_conflict", ("decision_exists",))
    error = _refused(_service(exists), principal, proposal, envelope)
    assert error.code == "control_plane_refused"
    assert error.diagnostics[-1] == f"decision:{decision}"
    assert exists.decisions == []

    unavailable = _Store()
    unavailable.fail_next = WorkStoreError("unavailable")
    with pytest.raises(WorkStoreError) as caught:
        _service(unavailable).execute_proposal(principal, proposal, envelope)
    assert caught.value.code == "unavailable"


def test_owner_signed_unlisted_runs_not_without_lease_or_provenance() -> None:
    mission_id = uuid4()
    lease = LeaseClaim(job_id=JOB, worker_id=WORKER, fence=3)
    facts = _facts(
        leases={JOB: LeaseFact(worker_id=WORKER, fence=3, expires_at=FUTURE)}
    )
    principal = _principal(scopes={"work.execute"})

    def proposal(**overrides: object) -> Proposal:
        fields: dict[str, object] = {
            "proposer": "worker",
            "action_class": None,
            "lease": lease,
        }
        fields.update(overrides)
        return _signed(**fields)

    store = _Store()
    service = _service(store, _Plane(facts))
    envelope = _envelope(_abandoned(mission_id))
    result = service.execute_proposal(principal, proposal(), envelope)
    assert result[1]["type"] == "set_mission_status"  # type: ignore[index]
    assert [item.command.type for item in store.executed] == ["set_mission_status"]
    assert store.executed[0].operation_id == envelope.operation_id
    assert store.decisions == []

    missing_lease = _Store()
    error = _refused(
        _service(missing_lease, _Plane(facts)),
        principal,
        proposal(lease=None),
        _envelope(_abandoned(mission_id)),
    )
    assert error.code == "control_plane_refused"
    assert "worker_lifecycle:lease_missing" in error.diagnostics
    assert "tier_gate:" not in " ".join(error.diagnostics)
    assert missing_lease.executed == []
    assert len(missing_lease.decisions) == 1

    missing_provenance = _Store()
    error = _refused(
        _service(missing_provenance, _Plane(facts)),
        principal,
        proposal(provenance="   "),
        _envelope(_abandoned(mission_id)),
    )
    assert "provenance_present:provenance_missing" in error.diagnostics
    assert "tier_gate:" not in " ".join(error.diagnostics)
    assert missing_provenance.executed == []
    assert len(missing_provenance.decisions) == 1


def test_allowed_proposal_without_an_envelope_does_not_call_the_store() -> None:
    store = _Store()
    service = _service(store)
    result = service.execute_proposal(
        _principal(),
        _signed(action_class="unlisted"),
    )
    assert result == {"authorized": True, "checks": CHECK_NAMES}
    assert store.attempts == []


def test_orchestrator_dispatch_without_a_reservation_is_refused() -> None:
    mission_id = uuid4()
    facts = _facts(missions={mission_id: MissionFact(approved=True)})
    store = _Store()
    plane = _Plane(facts)
    service = _service(store, plane)
    proposal = _proposal(
        proposer="orchestrator",
        operation="dispatch",
        mission_id=mission_id,
        effort="E1",
        cost_usd=Decimal("1.00"),
        action_class="update_mission_state",
    )
    error = _refused(service, _principal("automation"), proposal)
    assert error.code == "control_plane_refused"
    assert error.diagnostics == (
        "admission_control:reservation_missing",
        "paid_work_gate:reservation_missing",
    )
    assert store.attempts == []
    assert plane.seen == [WORKSPACE]

    # An orchestrator proposal from anyone but automation is forbidden first.
    forbidden = _refused(service, _principal("owner"), proposal)
    assert forbidden.code == "forbidden"
    assert forbidden.status == 403


def test_revise_mission_labelled_update_mission_state_does_not_evaluate() -> None:
    store = _Store()
    plane = _Plane()
    service = _service(store, plane)
    mission_id = uuid4()
    proposal = _proposal(
        action_class="update_mission_state",
        command_type="revise_mission",
        mission_id=mission_id,
    )
    error = _refused(service, _principal(), proposal, _envelope(_revise(mission_id)))
    assert error.diagnostics == ("command_binding:classification_mismatch",)
    assert store.attempts == []
    assert plane.seen == []


def test_unlabelled_unsigned_revise_mission_is_broaden_scope() -> None:
    store = _Store()
    service = _service(store)
    mission_id = uuid4()
    proposal = _proposal(action_class=None)
    error = _refused(service, _principal(), proposal, _envelope(_revise(mission_id)))
    decision = store.decisions[0].command.payload
    assert error.code == "control_plane_refused"
    assert decision.action_class == "broaden_scope"
    assert decision.mission_id == str(mission_id)
    assert "tier_gate:owner_signature_missing" in error.diagnostics
    assert store.executed == []
    assert len(store.decisions) == 1


def test_release_stop_is_refused_as_disable_safeguards() -> None:
    store = _Store()
    service = _service(store)
    proposal = _proposal(action_class=None)
    envelope = _envelope(ReleaseStopCommand(type="release_stop", payload={"reason": "clear"}))
    error = _refused(service, _principal("automation"), proposal, envelope)
    assert store.decisions[0].command.payload.action_class == "disable_safeguards"
    assert error.diagnostics == (
        "lock_integrity:lock_change_not_owner",
        "tier_gate:owner_signature_missing",
        f"decision:{_decision_id(proposal)}",
    )
    assert store.executed == []
    assert len(store.decisions) == 1


def test_other_workspace_envelope_is_forbidden() -> None:
    store = _Store()
    plane = _Plane()
    service = _service(store, plane)
    other = uuid4()
    error = _refused(
        service,
        _principal(),
        _proposal(workspace_id=other),
        _envelope(_engage(), workspace_id=other),
    )
    assert error.code == "forbidden"
    assert error.status == 403
    assert store.attempts == []
    assert plane.seen == []


def test_owner_and_typed_command_impersonation_is_forbidden() -> None:
    store = _Store()
    service = _service(store)
    principal = _principal("automation")
    for proposer in ("owner", "typed_command"):
        error = _refused(service, principal, _signed(proposer=proposer))
        assert error.code == "forbidden"
        assert error.status == 403
    assert store.attempts == []


def test_missing_control_plane_is_a_deciding_check_error() -> None:
    store = _Store()
    service = WorkService(store)  # type: ignore[arg-type]
    proposal = _proposal(action_class=None)
    envelope = _envelope(_engage())
    error = _refused(service, _principal(scopes={"work.stop"}), proposal, envelope)
    assert error.diagnostics == (
        "control_plane:check_error",
        f"decision:{_decision_id(proposal)}",
    )
    assert len(store.decisions) == 1
    assert store.executed == []


def test_facts_failure_is_a_deciding_check_error() -> None:
    store = _Store()
    service = _service(store, _Plane(raises=True))
    proposal = _proposal(action_class=None)
    error = _refused(service, _principal(scopes={"work.stop"}), proposal, _envelope(_engage()))
    assert error.diagnostics[0] == "control_plane:check_error"
    assert error.diagnostics[1].startswith("decision:")
    assert len(store.decisions) == 1
    assert store.executed == []


def test_evaluate_uses_the_planes_budget_and_clock() -> None:
    store = _Store()
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    service = _service(store, _Plane(budget_seconds=10.0, clock=clock))
    proposal = _proposal(action_class=None)
    error = _refused(service, _principal(scopes={"work.stop"}), proposal, _envelope(_engage()))
    assert "single_mutation_path:check_timeout" in error.diagnostics
    assert store.executed == []
    assert len(store.decisions) == 1
    assert calls["n"] > 1
