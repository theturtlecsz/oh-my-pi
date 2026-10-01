"""OMP-403: the control-plane executor for one classified action.

``perform`` is the only path that turns a submitted operation into a side
effect. It parses the submission (dropping any tier/label/description/class the
worker claimed), resolves the target itself, and classifies the operation from
those two facts alone. The tier decides what happens next:

- Tier 1 runs: ``request_action`` records the verdict and the executor runs.
- Tier 2 runs only when the recorded action is allowed — a spend through
  ``record_spend``, anything else through ``request_action`` under a standing
  policy. A refusal is returned as a refused outcome, never executed. A created
  disposable cloud resource is recorded so a later delete can be tier 2.
- Tier 3 is held: the action is recorded as blocked and an owner decision is
  written through the v1 contract with the action class and the target digest.
  A refusal that rolled the block back (``mission_not_found``) does not open
  that decision.
- Tier 3 presenting a decision_id runs only through ``Tier3Approval``. The
  target is re-resolved and re-classified before acting, and the executor runs
  only if the target did not change. A changed digest or class is refused
  ``target_changed``, and the executor does not run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID, uuid4

from omp_work.action_classify import (
    Classification,
    Operation,
    ResolvedTarget,
    classify,
    parse_submission,
)
from omp_work.project_store import ProjectAuthorityRefused, Tier3Approval
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest
from omp_work.v1.models import (
    CommandEnvelope,
    CreateDecisionCommand,
    CreateDecisionPayload,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)

__all__ = [
    "ActionOutcome",
    "ActionStore",
    "Executor",
    "Resolver",
    "perform",
]

ActionStatus = Literal["done", "held", "refused"]

# The resolver resolves the submitted operation to the target the control plane
# computed itself; the executor performs the allowed action.
Resolver = Callable[[Operation], ResolvedTarget]
Executor = Callable[[Operation, ResolvedTarget], object]

_DECISION_QUESTION = "Authorize this tier 3 action?"
_DECISION_WHY = (
    "The control plane classified the action as tier 3, so it needs the owner's"
    " explicit authorization before it runs."
)
_DECISION_RISK_OF_DELAY = "The action stays blocked and the mission waits on the owner."
_DECISION_OPTIONS = ("approve", "decline")
_DECISION_DEFAULT = "decline"
_DECISION_RISKS = {
    "approve": "The action runs exactly as classified against the recorded target.",
    "decline": "The action stays blocked.",
}


@dataclass(frozen=True)
class ActionOutcome:
    """What ``perform`` did with one submitted action."""

    status: ActionStatus
    action_class: str
    tier: int
    decision_id: UUID | None = None
    code: str | None = None

    @property
    def class_(self) -> str:
        """The action class under a name that is not a Python keyword."""
        return self.action_class


class ActionStore(Protocol):
    """The store surface ``perform`` drives (satisfied by PostgresWorkStore)."""

    def request_action(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        action: ActionRequest,
        authority: ChangeAuthority | None,
        now: datetime,
        *,
        approval: Tier3Approval | None = None,
    ) -> UUID | None: ...

    def record_spend(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        amount_usd: Decimal,
        now: datetime,
    ) -> None: ...

    def record_disposable_resource(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        resource_id: str,
        policy_id: UUID | None,
    ) -> None: ...

    def execute(
        self,
        envelope: CommandEnvelope,
        *,
        actor_id: UUID,
        actor_kind: str,
        required_scope: str,
    ) -> tuple[object, dict[str, object]]: ...


def perform(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    submission: Mapping[str, object],
    resolver: Resolver,
    executor: Executor,
    now: datetime,
    decision_id: UUID | None = None,
) -> ActionOutcome:
    """Classify one submission and carry it out, hold it, or refuse it."""
    operation = parse_submission(submission)
    resolved = resolver(operation)
    classification = classify(operation, resolved)

    if classification.tier == 3:
        _record_alarm(
            store,
            workspace_id,
            actor_id,
            3,
            classification.action_class,
            "attempted",
        )
        if decision_id is None:
            return _hold(
                store,
                workspace_id,
                actor_id,
                project_id,
                mission_id,
                operation,
                now,
                classification,
            )
        return _execute_tier3(
            store,
            workspace_id,
            actor_id,
            project_id,
            mission_id,
            operation,
            resolver,
            executor,
            now,
            decision_id,
            classification,
        )
    if classification.tier == 2:
        return _execute_tier2(
            store,
            workspace_id,
            actor_id,
            project_id,
            mission_id,
            operation,
            resolved,
            executor,
            now,
            classification,
        )
    return _execute_tier1(
        store,
        workspace_id,
        actor_id,
        project_id,
        mission_id,
        operation,
        resolved,
        executor,
        now,
        classification,
    )


def _request_action(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    action: ActionRequest,
    now: datetime,
    *,
    approval: Tier3Approval | None = None,
) -> UUID | None:
    """Record one action with no standing authority; the approval carries the owner's answer."""
    return store.request_action(
        workspace_id,
        actor_id,
        project_id,
        mission_id,
        action,
        None,
        now,
        approval=approval,
    )


def _execute_tier1(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    operation: Operation,
    resolved: ResolvedTarget,
    executor: Executor,
    now: datetime,
    classification: Classification,
) -> ActionOutcome:
    action = _action_for(operation, classification.action_class)
    try:
        _request_action(
            store, workspace_id, actor_id, project_id, mission_id, action, now
        )
    except ProjectAuthorityRefused as refused:
        return ActionOutcome(
            "refused", classification.action_class, 1, None, refused.code
        )
    executor(operation, resolved)
    return ActionOutcome("done", classification.action_class, 1)


def _execute_tier2(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    operation: Operation,
    resolved: ResolvedTarget,
    executor: Executor,
    now: datetime,
    classification: Classification,
) -> ActionOutcome:
    if classification.action_class == "spend_beyond_threshold":
        if operation.amount_usd is None:
            return ActionOutcome(
                "refused", classification.action_class, 2, None, "amount_required"
            )
        try:
            store.record_spend(
                workspace_id,
                actor_id,
                project_id,
                mission_id,
                operation.amount_usd,
                now,
            )
        except ProjectAuthorityRefused as refused:
            if refused.code == "standing_policy_required":
                _record_alarm(
                    store,
                    workspace_id,
                    actor_id,
                    2,
                    classification.action_class,
                    refused.code,
                )
            return ActionOutcome(
                "refused", classification.action_class, 2, None, refused.code
            )
        executor(operation, resolved)
        return ActionOutcome("done", classification.action_class, 2)

    action = _action_for(operation, classification.action_class)
    try:
        policy_id = _request_action(
            store, workspace_id, actor_id, project_id, mission_id, action, now
        )
    except ProjectAuthorityRefused as refused:
        if refused.code == "standing_policy_required":
            _record_alarm(
                store,
                workspace_id,
                actor_id,
                2,
                classification.action_class,
                refused.code,
            )
        return ActionOutcome(
            "refused", classification.action_class, 2, None, refused.code
        )
    executor(operation, resolved)
    if operation.kind == "cloud_create" and operation.resource_id is not None:
        store.record_disposable_resource(
            workspace_id,
            actor_id,
            project_id,
            mission_id,
            operation.resource_id,
            policy_id,
        )
    return ActionOutcome("done", classification.action_class, 2)


def _hold(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    operation: Operation,
    now: datetime,
    classification: Classification,
) -> ActionOutcome:
    """Record the block and write the owner decision. Nothing runs.

    ``blocked_owner_signature`` is raised after the refused row commits. Any
    other refusal, including ``mission_not_found``, aborted that transaction,
    so no decision is opened on top of a missing block.
    """
    action = _action_for(operation, classification.action_class)
    try:
        _request_action(
            store, workspace_id, actor_id, project_id, mission_id, action, now
        )
    except ProjectAuthorityRefused as refused:
        if refused.code != "blocked_owner_signature":
            return ActionOutcome(
                "refused", classification.action_class, 3, None, refused.code
            )
    envelope = _decision_envelope(workspace_id, project_id, mission_id, classification)
    _receipt, result = store.execute(
        envelope,
        actor_id=actor_id,
        actor_kind="automation",
        required_scope="work.mutate",
    )
    return ActionOutcome(
        "held",
        classification.action_class,
        3,
        _as_uuid(result.get("decision_id")),
    )


def _execute_tier3(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    operation: Operation,
    resolver: Resolver,
    executor: Executor,
    now: datetime,
    decision_id: UUID | None,
    classification: Classification,
) -> ActionOutcome:
    approval = Tier3Approval(
        decision_id=decision_id, target_sha256=classification.target_sha256
    )
    action = _action_for(operation, classification.action_class)
    try:
        _request_action(
            store,
            workspace_id,
            actor_id,
            project_id,
            mission_id,
            action,
            now,
            approval=approval,
        )
    except ProjectAuthorityRefused as refused:
        return ActionOutcome(
            "refused",
            classification.action_class,
            3,
            _as_uuid(decision_id),
            refused.code,
        )

    # Re-resolve and re-classify immediately before acting: a target that moved
    # after the owner signed must not run under the old signature.
    resolved = resolver(operation)
    rechecked = classify(operation, resolved)
    if (
        rechecked.action_class != classification.action_class
        or rechecked.target_sha256 != classification.target_sha256
    ):
        return ActionOutcome(
            "refused",
            rechecked.action_class,
            3,
            _as_uuid(decision_id),
            "target_changed",
        )
    executor(operation, resolved)
    return ActionOutcome("done", rechecked.action_class, 3, _as_uuid(decision_id))


def _action_for(operation: Operation, action_class: str) -> ActionRequest:
    """The audit ActionRequest for a classified operation."""
    return ActionRequest(
        action_class,
        repository=operation.repository,
        branch=operation.branch,
        destination=operation.destination,
        resource_type=operation.resource_type,
        amount_usd=operation.amount_usd,
    )


def _decision_envelope(
    workspace_id: UUID,
    project_id: UUID,
    mission_id: UUID | None,
    classification: Classification,
) -> CommandEnvelope:
    payload = CreateDecisionPayload(
        decision_id=uuid4(),
        project_id=project_id,
        mission_id=None if mission_id is None else str(mission_id),
        question=_DECISION_QUESTION,
        why_it_matters=_DECISION_WHY,
        risk_of_delay=_DECISION_RISK_OF_DELAY,
        options=_DECISION_OPTIONS,
        default_if_any=_DECISION_DEFAULT,
        risk_of_each_choice=dict(_DECISION_RISKS),
        action_class=classification.action_class,
        target_sha256=classification.target_sha256,
        resume_state=f"tier3:{classification.action_class}",
    )
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=CreateDecisionCommand(type="create_decision", payload=payload),
    )


def _as_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _alarm_envelope(
    workspace_id: UUID,
    signal: Literal[
        "cost_threshold",
        "budget_exceeded",
        "safety_check_failed",
        "credential_appeared",
        "owner_approval_attempt",
    ],
    subject: str,
    detail: str,
) -> CommandEnvelope:
    payload = RecordAlarmSignalPayload(
        signal=signal,
        subject=subject,
        detail=detail,
    )
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=RecordAlarmSignalCommand(type="record_alarm_signal", payload=payload),
    )


def _record_alarm(
    store: ActionStore,
    workspace_id: UUID,
    actor_id: UUID,
    tier: int,
    action_class: str,
    detail: str,
) -> None:
    envelope = _alarm_envelope(
        workspace_id,
        signal="owner_approval_attempt",
        subject=f"tier {tier} {action_class}",
        detail=detail,
    )
    store.execute(
        envelope,
        actor_id=actor_id,
        actor_kind="automation",
        required_scope="work.mutate",
    )
