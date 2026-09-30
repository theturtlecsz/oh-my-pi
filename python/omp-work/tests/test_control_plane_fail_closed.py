"""WorkService.execute_proposal fails closed: each row breaks one thing in a passing fixture.

Every row starts from a proposal the control plane would allow, changes exactly
one thing, and asserts the refusal the service raises: the ``control_plane_refused``
code, the deciding ``check:code`` diagnostic, and the one ``create_decision``
recorded for it (with its action class). A ``None`` action class in a row means
``decision_payload`` could not promote a tier-3 class, so the class is inherited
from the proposal or left unset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

import omp_work.control_plane.envelope as envelope_module
from omp_work.control_plane.gate import (
    ControlPlaneFacts,
    LeaseClaim,
    LeaseFact,
    OwnerAuthorization,
    Proposal,
)
from omp_work.control_plane.registry import CONTROL_PLANE_CHECKS, ControlPlane
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.canonical import command_sha256
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store_shared import WorkStoreError

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
PAST = datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc)
FUTURE = datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("00000000-0000-0000-0000-0000000000aa")
REVISION = 4
SIGNATURE = "owner-sig"
JOB = "job-7"
WORKER = "worker-7"
CHECK_NAMES = sorted(check.name for check in CONTROL_PLANE_CHECKS)


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


def _principal(kind: str = "automation") -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind=kind,
        workspaces=frozenset({WORKSPACE}),
        scopes=frozenset(
            {"work.mutate", "work.execute", "work.approve", "work.stop"}
        ),
    )


def _push_policy(*, policy_id: str, repos: tuple[str, ...] = ("repo-1",)) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="push_branch",
        repositories=repos,
        branch_patterns=("feature/*",),
        decision_id="d-1",
    )


def _push_action() -> ActionRequest:
    return ActionRequest(action_class="push_branch", repository="repo-1", branch="feature/x")


def _push_repo() -> RepositoryRecord:
    return RepositoryRecord(
        key="repo-1", default_branch="main", automation_ci_secret_free=True
    )


class _Store:
    """Fake store: one create_decision per refusal, idempotent by operation_id."""

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


class _RaisingGet(dict):
    """A lease table whose lookup fails; a worker proposal must fail closed."""

    def get(self, key: object, default: object = None) -> object:
        raise RuntimeError("lease store unavailable")


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        value = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return value


class _Plane:
    def __init__(
        self,
        facts: ControlPlaneFacts,
        *,
        verifier: object = None,
        budget_seconds: float = 10.0,
        clock: object = None,
        raises_facts: bool = False,
    ) -> None:
        self._facts = facts
        self.raises_facts = raises_facts
        if verifier == "raise":

            def verify(_message: bytes, _signature: str) -> bool:
                raise RuntimeError("verification subprocess crashed")

        else:

            def verify(_message: bytes, signature: str) -> bool:
                return signature == SIGNATURE

        self.plane = ControlPlane(
            self._load,
            verify,
            budget_seconds,
            clock,  # type: ignore[arg-type]
        )

    def _load(self, _workspace_id: UUID) -> ControlPlaneFacts:
        if self.raises_facts:
            raise RuntimeError("facts unavailable")
        return self._facts


@dataclass(frozen=True)
class Row:
    id: str
    proposal: dict[str, object] = field(default_factory=dict)
    facts: dict[str, object] = field(default_factory=dict)
    verifier: object = None
    budget_seconds: float = 10.0
    clock: object = None
    raises_facts: bool = False
    raises_covers: bool = False
    # The deciding check:code; or a suffix every non-decision diagnostic carries.
    diagnostic: str | None = None
    diagnostic_suffix: str | None = None
    action_class: str | None = None


ROWS: tuple[Row, ...] = (
    Row(
        id="unlisted-class",
        proposal={"action_class": None},
        diagnostic="tier_gate:unclassified_action",
        action_class="unlisted",
    ),
    Row(
        id="two-policies",
        proposal={"action_class": "push_branch", "action": _push_action()},
        facts={
            "repositories": (_push_repo(),),
            "standing_policies": (
                _push_policy(policy_id="p-1"),
                _push_policy(policy_id="p-2", repos=("repo-1", "repo-2")),
            ),
        },
        diagnostic="tier_gate:policy_conflict",
    ),
    Row(
        id="no-policy",
        proposal={"action_class": "push_branch", "action": _push_action()},
        facts={"repositories": (_push_repo(),)},
        diagnostic="tier_gate:standing_policy_missing",
    ),
    Row(
        id="blank-provenance",
        proposal={"provenance": "   "},
        diagnostic="provenance_present:provenance_missing",
    ),
    Row(
        id="lease-expired",
        proposal={
            "proposer": "worker",
            "lease": LeaseClaim(job_id=JOB, worker_id=WORKER, fence=3),
        },
        facts={
            "leases": {JOB: LeaseFact(worker_id=WORKER, fence=3, expires_at=PAST)}
        },
        diagnostic="worker_lifecycle:lease_expired",
    ),
    Row(
        id="lease-reassigned",
        proposal={
            "proposer": "worker",
            "lease": LeaseClaim(job_id=JOB, worker_id=WORKER, fence=4),
        },
        facts={
            "leases": {JOB: LeaseFact(worker_id=WORKER, fence=3, expires_at=FUTURE)}
        },
        diagnostic="worker_lifecycle:lease_reassigned",
    ),
    Row(
        id="merge-signature-missing",
        proposal={"action_class": "merge_protected_branch"},
        diagnostic="tier_gate:owner_signature_missing",
        action_class="merge_protected_branch",
    ),
    Row(
        id="merge-signature-bad",
        proposal={
            "action_class": "merge_protected_branch",
            "owner_authorization": OwnerAuthorization(
                signature="not-owner-sig", expires_at=FUTURE
            ),
        },
        diagnostic="tier_gate:owner_signature_invalid",
        action_class="merge_protected_branch",
    ),
    Row(
        id="merge-signature-expired",
        proposal={
            "action_class": "merge_protected_branch",
            "owner_authorization": OwnerAuthorization(signature=SIGNATURE, expires_at=PAST),
        },
        diagnostic="tier_gate:owner_signature_expired",
        action_class="merge_protected_branch",
    ),
    Row(
        id="merge-verifier-raises",
        proposal={
            "action_class": "merge_protected_branch",
            "owner_authorization": OwnerAuthorization(signature=SIGNATURE, expires_at=FUTURE),
        },
        verifier="raise",
        diagnostic="tier_gate:check_error",
        action_class="merge_protected_branch",
    ),
    Row(
        id="covers-raises",
        proposal={"action_class": "push_branch", "action": _push_action()},
        facts={
            "repositories": (_push_repo(),),
            "standing_policies": (_push_policy(policy_id="p-1"),),
        },
        raises_covers=True,
        diagnostic="tier_gate:check_error",
    ),
    Row(
        id="lease-lookup-raises",
        proposal={
            "proposer": "worker",
            "lease": LeaseClaim(job_id=JOB, worker_id=WORKER, fence=3),
        },
        facts={"leases": _RaisingGet()},
        diagnostic="worker_lifecycle:check_error",
    ),
    Row(
        id="facts-raises",
        raises_facts=True,
        diagnostic="control_plane:check_error",
    ),
    Row(
        id="budget-timeout",
        clock=_Clock([0.0, 100.0]),
        diagnostic_suffix="check_timeout",
    ),
)


def _run(row: Row, monkeypatch: pytest.MonkeyPatch) -> tuple[WorkError, _Store]:
    store = _Store()
    if row.raises_covers:

        def raising_covers(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("policy store unavailable")

        monkeypatch.setattr(envelope_module, "covers", raising_covers)
    service = WorkService(
        store,  # type: ignore[arg-type]
        _Plane(
            _facts(**row.facts),
            verifier=row.verifier,
            budget_seconds=row.budget_seconds,
            clock=row.clock,
            raises_facts=row.raises_facts,
        ).plane,
    )
    with pytest.raises(WorkError) as caught:
        service.execute_proposal(_principal(), _proposal(**row.proposal))
    return caught.value, store


@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
def test_execute_proposal_fails_closed(row: Row, monkeypatch: pytest.MonkeyPatch) -> None:
    error, store = _run(row, monkeypatch)
    assert error.code == "control_plane_refused"
    assert error.status == 409

    decision = store.decisions[0].command.payload  # type: ignore[attr-defined]
    assert error.diagnostics[-1] == f"decision:{decision.decision_id}"
    refusals = error.diagnostics[:-1]

    if row.diagnostic is not None:
        assert refusals == (row.diagnostic,)
    else:
        assert refusals == tuple(f"{name}:{row.diagnostic_suffix}" for name in CHECK_NAMES)

    assert store.executed == []
    assert len(store.decisions) == 1
    assert decision.action_class == row.action_class
