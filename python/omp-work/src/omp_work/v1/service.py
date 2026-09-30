from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5


from pydantic import ValidationError

from ..control_plane import gate
from ..control_plane.envelope import bind_command
from ..control_plane.registry import CONTROL_PLANE_CHECKS, ControlPlane
from .api_models import (
    DecisionsPage,
    EventSubscriptionsPage,
    MissionEventsPage,
    StopStatusView,
)
from .models import (
    OWNER_APPROVAL_COMMAND_TYPES,
    AdvanceEventCursorCommand,
    CommandEnvelope,
    CreateDecisionCommand,
    CreateDecisionPayload,
    DeleteEventSubscriptionCommand,
    PutEventSubscriptionCommand,
)
from .store import WorkStore, WorkStoreError

# decision_payload hashes this same string into the decision id, so the
# stored id and the envelope ids stay aligned when the proposal is replayed.
_MISSING_PROPOSAL_ID = "00000000-0000-0000-0000-000000000000"
_RECORDED_DECISION = frozenset({"idempotency_conflict", "decision_exists"})
# A push_url with no checker installed is refused with this reason. The
# configured checker supplies its own reason string.
_NO_PUSH_CHECK = "no_check"
_SUBSCRIPTION_COMMANDS = (
    PutEventSubscriptionCommand,
    DeleteEventSubscriptionCommand,
    AdvanceEventCursorCommand,
)


@dataclass(frozen=True)
class Principal:
    actor_id: UUID
    actor_kind: str
    workspaces: frozenset[UUID]
    scopes: frozenset[str]
    candidate_ids: frozenset[UUID] | None = None


class WorkError(Exception):
    def __init__(
        self, code: str, *, status: int = 400, diagnostics: tuple[str, ...] = ()
    ) -> None:
        super().__init__(code)
        self.code, self.status, self.diagnostics = code, status, diagnostics


def _proposal_identity(proposal: gate.Proposal) -> str:
    if proposal.proposal_id is None:
        return _MISSING_PROPOSAL_ID
    return str(proposal.proposal_id)


def _decision_already_recorded(error: WorkStoreError) -> bool:
    if error.code in _RECORDED_DECISION:
        return True
    return any(item in _RECORDED_DECISION for item in error.diagnostics)


def _check_error_verdict() -> gate.Verdict:
    """No plane, or facts() failed: one deciding check_error, no other checks."""
    return gate.Verdict(
        refusals=(
            gate.Refusal(
                check="control_plane", code="check_error", raise_decision=True
            ),
        )
    )


class WorkService:
    _scopes = {
        "create_work_batch": "work.mutate",
        "revise_work": "work.mutate",
        "set_work_state": "work.mutate",
        "put_relation": "work.mutate",
        "remove_relation": "work.mutate",
        "set_focus": "work.mutate",
        "clear_focus": "work.mutate",
        "record_project_health": "work.mutate",
        "record_alarm_signal": "work.mutate",
        "append_evidence": "work.approve",
        "finalize_candidate": "work.approve",
        "assess_bounded_intake": "work.approve",
        "record_fable_advice": "work.approve",
        "attest_intake_admission": "work.approve",
        "publish_bounded_intake": "work.approve",
        "answer_intake_decision": "work.approve",
        "create_same_session_child": "work.close",
        "begin_close_attempt": "work.close",
        "seal_audit_manifest": "work.close",
        "reserve_auditor_launch": "work.close",
        "cancel_auditor_launch": "work.close",
        "settle_auditor_launch": "work.close",
        "attest_checkpoint_delivery": "work.close",
        "record_closeout_review": "work.close",
        "record_external_delivery": "work.close",
        "complete_work": "work.close",
        "stage_import_batch": "work.import",
        "promote_import_batch": "work.import",
        "activate_cutover": "work.operate",
        "attest_cutover_plan": "work.operate",
        "begin_execution": "work.execute",
        "activate_execution_item": "work.execute",
        "seal_execution_criteria": "work.execute",
        "stamp_execution_plan": "work.execute",
        "set_execution_state": "work.execute",
        "complete_execution_item": "work.execute",
        "skip_active_item": "work.execute",
        "register_research_artifact": "work.execute",
        "collect_research_artifact": "work.execute",
        "register_research_source": "work.execute",
        "register_research_dataset": "work.execute",
        "record_research_cache": "work.execute",
        "claim_research_replicate": "work.execute",
        "bind_research_receipt_manifest": "work.execute",
        "register_research_component": "work.execute",
        "create_research_campaign": "work.execute",
        "admit_research_campaign": "work.approve",
        "cancel_research_campaign": "work.execute",
        "propose_research_trial": "work.execute",
        "record_research_observation": "work.execute",
        "bind_research_deliverable": "work.execute",
        "set_research_campaign_state": "work.execute",
        "conclude_research_campaign": "work.approve",
        "engage_stop": "work.stop",
        "release_stop": "work.approve",
        "create_decision": "work.mutate",
        "answer_decision": "work.approve",
        "submit_mission": "work.mutate",
        "revise_mission": "work.mutate",
        "link_mission_work": "work.mutate",
        "approve_mission": "work.approve",
        "set_mission_status": "work.execute",
        "draft_mission_intake": "work.mutate",
        "answer_mission_draft": "work.approve",
        "record_finding": "work.execute",
        "put_event_subscription": "work.read",
        "delete_event_subscription": "work.read",
        "advance_event_cursor": "work.read",
    }

    def __init__(
        self,
        store: WorkStore,
        control_plane: ControlPlane | None = None,
        *,
        push_destination_check: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store
        self._control_plane = control_plane
        self._push_destination_check = push_destination_check

    def execute(
        self, principal: Principal, envelope: CommandEnvelope
    ) -> tuple[object, dict[str, object]]:
        if envelope.workspace_id not in principal.workspaces:
            raise WorkError("forbidden", status=403)
        scope = self._scopes[envelope.command.type]
        if scope not in principal.scopes:
            self._record_owner_approval_refusal(
                principal, envelope, "forbidden", ()
            )
            raise WorkError("forbidden", status=403)
        if envelope.command.type == "release_stop" and principal.actor_kind != "owner":
            raise WorkError("forbidden", status=403)
        # OMP-414: answering a decision is the owner's act — a signature on a
        # tier-3 action is the owner's alone, so automation never answers.
        if (
            envelope.command.type == "answer_decision"
            and principal.actor_kind != "owner"
        ):
            raise WorkError("forbidden", status=403)
        # OMP-426: answering a mission draft is the owner's act.
        if (
            envelope.command.type == "answer_mission_draft"
            and principal.actor_kind != "owner"
        ):
            raise WorkError("forbidden", status=403)
        if envelope.command.type in {"stage_import_batch", "promote_import_batch"}:
            raise WorkError("unavailable", status=503)
        # OMP-415: another client's subscription needs work.events.admin, and a
        # push_url is refused before the store writes it.
        self._enforce_event_subscription(principal, envelope)
        try:
            return self._store.execute(
                envelope,
                actor_id=principal.actor_id,
                actor_kind=principal.actor_kind,
                required_scope=scope,
            )
        except WorkStoreError as error:
            statuses = {
                "invalid_request": 400,
                "forbidden": 403,
                "relation_cycle": 400,
                "idempotency_conflict": 409,
                "revision_conflict": 409,
                "focus_conflict": 409,
                "stale_evidence": 409,
                "stale_intake": 409,
                "intake_not_ready": 409,
                "intake_admission_blocked": 409,
                "completion_blocked": 409,
                "cutover_invariant": 409,
                "artifact_unavailable": 503,
                "unavailable": 503,
            }
            self._record_owner_approval_refusal(
                principal, envelope, error.code, error.diagnostics
            )
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def _record_owner_approval_refusal(
        self,
        principal: Principal,
        envelope: CommandEnvelope,
        code: str,
        diagnostics: tuple[str, ...],
    ) -> None:
        # Record only in-workspace owner-approval refusals. Status mapping
        # stays in execute. A recording error propagates.
        if envelope.command.type not in OWNER_APPROVAL_COMMAND_TYPES:
            return
        self._store.record_refused_attempt(
            envelope,
            actor_id=principal.actor_id,
            actor_kind=principal.actor_kind,
            code=code,
            diagnostics=diagnostics,
        )

    def execute_proposal(
        self,
        principal: Principal,
        proposal: gate.Proposal,
        envelope: CommandEnvelope | None = None,
    ) -> tuple[object, dict[str, object]] | dict[str, object]:
        # The envelope's workspace is the one being acted on. A proposal with
        # no envelope is acted on in its own workspace. Either must be the
        # principal's, before any check or store call.
        workspace_id = (
            envelope.workspace_id if envelope is not None else proposal.workspace_id
        )
        if not isinstance(workspace_id, UUID) or workspace_id not in principal.workspaces:
            raise WorkError("forbidden", status=403)
        proposer = proposal.proposer
        if proposer in {"owner", "typed_command"} and principal.actor_kind != "owner":
            raise WorkError("forbidden", status=403)
        if proposer == "orchestrator" and principal.actor_kind != "automation":
            raise WorkError("forbidden", status=403)

        if envelope is not None:
            bound = bind_command(
                proposal, envelope, self._scopes.get(envelope.command.type)
            )
            if isinstance(bound, gate.Refusal):
                # A malformed binding is not an authorization question.
                verdict = gate.Verdict(refusals=(bound,))
            else:
                proposal = bound
                verdict = self._evaluate_control_plane(proposal, workspace_id)
        else:
            verdict = self._evaluate_control_plane(proposal, workspace_id)

        if not verdict.allowed:
            decision_id = None
            if verdict.raises_decision:
                decision_id = self._record_control_plane_decision(
                    principal, proposal, workspace_id, verdict
                )
            diagnostics = tuple(
                sorted(f"{item.check}:{item.code}" for item in verdict.refusals)
            )
            if decision_id is not None:
                diagnostics = diagnostics + (f"decision:{decision_id}",)
            raise WorkError(
                "control_plane_refused", status=409, diagnostics=diagnostics
            )
        if envelope is not None:
            return self.execute(principal, envelope)
        return {"authorized": True, "checks": list(verdict.checks_run)}

    def _evaluate_control_plane(
        self, proposal: gate.Proposal, workspace_id: UUID
    ) -> gate.Verdict:
        plane = self._control_plane
        if plane is None:
            return _check_error_verdict()
        try:
            facts = plane.facts(workspace_id)
        except Exception:
            return _check_error_verdict()
        return gate.evaluate(
            proposal,
            gate.CheckContext(
                facts=facts, verify_signature=plane.verify_signature
            ),
            CONTROL_PLANE_CHECKS,
            budget_seconds=plane.budget_seconds,
            clock=plane.clock,
        )

    def _record_control_plane_decision(
        self,
        principal: Principal,
        proposal: gate.Proposal,
        workspace_id: UUID,
        verdict: gate.Verdict,
    ) -> UUID:
        payload = gate.decision_payload(proposal, verdict)
        command = CreateDecisionCommand(
            type="create_decision",
            payload=CreateDecisionPayload.model_validate(payload.model_dump()),
        )
        # operation_id, request_id, and correlation_id are uuid5 of the
        # proposal id, so a replay is the same operation and the same hash.
        envelope_id = uuid5(NAMESPACE_URL, _proposal_identity(proposal))
        decision_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=envelope_id,
            request_id=envelope_id,
            correlation_id=envelope_id,
            command=command,
        )
        try:
            self._store.execute(
                decision_envelope,
                actor_id=principal.actor_id,
                actor_kind=principal.actor_kind,
                required_scope=self._scopes["create_decision"],
            )
        except WorkStoreError as error:
            if not _decision_already_recorded(error):
                raise
        return payload.decision_id

    def activity(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        project_id: UUID | None,
        limit: int,
    ) -> dict[str, object]:
        # work.read only — candidate-bounded readers hold no workspace-wide view.
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        try:
            return self._store.activity(
                workspace_id, principal.actor_id, project_id=project_id, limit=limit
            )
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def read(
        self, principal: Principal, workspace_id: UUID, kind: str, value: str
    ) -> dict[str, object]:
        if workspace_id not in principal.workspaces:
            raise WorkError("forbidden", status=403)
        allowlist: frozenset[UUID] | None = None
        if "work.read" not in principal.scopes:
            if (
                "work.candidate.read" not in principal.scopes
                or not principal.candidate_ids
                or kind != "workflow"
            ):
                raise WorkError("forbidden", status=403)
            allowlist = principal.candidate_ids
        try:
            return self._store.read(
                workspace_id,
                principal.actor_id,
                kind,
                value,
                candidate_allowlist=allowlist,
            )
        except WorkStoreError as error:
            statuses = {
                "invalid_request": 400,
                "forbidden": 403,
                "artifact_unavailable": 503,
                "stale_evidence": 409,
                "unavailable": 503,
            }
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def revision(
        self,
        principal: Principal,
        workspace_id: UUID,
        key: str,
        selector: str | int | UUID,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        try:
            return self._store.revision(
                workspace_id, principal.actor_id, key, selector
            )
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def receipt(
        self,
        principal: Principal,
        workspace_id: UUID,
        receipt_id: UUID | str,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        try:
            return self._store.receipt(workspace_id, principal.actor_id, receipt_id)
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def decisions(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        status: str | None = None,
        project_id: UUID | None = None,
        mission_id: str | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        # work.read only: a decision is workspace-wide, never candidate-bounded.
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        if status is not None and status not in ("pending", "answered"):
            raise WorkError("invalid_request", status=400)
        try:
            return DecisionsPage.model_validate(
                self._store.decisions(
                    workspace_id,
                    principal.actor_id,
                    status=status,
                    project_id=project_id,
                    mission_id=mission_id,
                    after=after,
                    limit=limit,
                )
            ).model_dump(mode="json")
        except ValidationError as error:
            raise WorkError("invalid_request", status=400) from error
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def work_items(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        try:
            return self._store.work_items(
                workspace_id, principal.actor_id, after, limit
            )
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def events(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        after_sequence: int = 0,
        after: int | None = None,
        limit: int = 500,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
            or principal.candidate_ids is not None
        ):
            raise WorkError("forbidden", status=403)
        effective_after = after if after is not None else after_sequence
        try:
            return self._store.events(
                workspace_id, principal.actor_id, after=effective_after, limit=limit
            )
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def mission_events(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 500,
        mission_id: UUID | None = None,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        try:
            return MissionEventsPage.model_validate(
                self._store.mission_events(
                    workspace_id,
                    principal.actor_id,
                    after=after_sequence,
                    limit=limit,
                    mission_id=mission_id,
                )
            ).model_dump(mode="json")
        except ValidationError as error:
            raise WorkError("invalid_request", status=400) from error
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def _enforce_event_subscription(
        self, principal: Principal, envelope: CommandEnvelope
    ) -> None:
        command = envelope.command
        if not isinstance(command, _SUBSCRIPTION_COMMANDS):
            return
        if "work.events.admin" not in principal.scopes:
            self._forbid_other_client_subscription(principal, envelope, command)
        self._refuse_push_destination(command)

    def _forbid_other_client_subscription(
        self,
        principal: Principal,
        envelope: CommandEnvelope,
        command: PutEventSubscriptionCommand
        | DeleteEventSubscriptionCommand
        | AdvanceEventCursorCommand,
    ) -> None:
        if isinstance(command, PutEventSubscriptionCommand):
            named = command.payload.client_id
            if named is not None and named != principal.actor_id:
                raise WorkError("forbidden", status=403)
        # Unknown ids are the store's refusal (subscription_not_found). A
        # deleted id is absent from the list, so it reaches the store too.
        owner = self._stored_subscription_client(
            principal, envelope.workspace_id, command.payload.subscription_id
        )
        if owner is not None and owner != principal.actor_id:
            raise WorkError("forbidden", status=403)

    def _stored_subscription_client(
        self, principal: Principal, workspace_id: UUID, subscription_id: UUID
    ) -> UUID | None:
        try:
            page = self._store.event_subscriptions(
                workspace_id, principal.actor_id, client_id=None
            )
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error
        subscriptions = page.get("subscriptions", ()) if isinstance(page, dict) else ()
        if not isinstance(subscriptions, (list, tuple)):
            return None
        target = str(subscription_id)
        for item in subscriptions:
            if not isinstance(item, dict) or item.get("deleted") is True:
                continue
            if str(item.get("subscription_id")) != target:
                continue
            client = item.get("client_id")
            if client is None:
                return None
            return client if isinstance(client, UUID) else UUID(str(client))
        return None

    def _refuse_push_destination(
        self,
        command: PutEventSubscriptionCommand
        | DeleteEventSubscriptionCommand
        | AdvanceEventCursorCommand,
    ) -> None:
        if not isinstance(command, PutEventSubscriptionCommand):
            return
        push_url = command.payload.push_url
        if push_url is None:
            return
        check = self._push_destination_check
        reason = _NO_PUSH_CHECK if check is None else check(push_url)
        if reason is not None:
            raise WorkError(
                "invalid_request",
                status=400,
                diagnostics=("push_destination_refused", reason),
            )

    def event_subscriptions(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        client_id: UUID | None = None,
    ) -> dict[str, object]:
        if (
            workspace_id not in principal.workspaces
            or "work.read" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        if (
            client_id is not None
            and client_id != principal.actor_id
            and "work.events.admin" not in principal.scopes
        ):
            raise WorkError("forbidden", status=403)
        resolved = (
            principal.actor_id
            if client_id is None and "work.events.admin" not in principal.scopes
            else client_id
        )
        try:
            return EventSubscriptionsPage.model_validate(
                self._store.event_subscriptions(
                    workspace_id, principal.actor_id, client_id=resolved
                )
            ).model_dump(mode="json")
        except ValidationError as error:
            raise WorkError("invalid_request", status=400) from error
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

    def stop_status(
        self,
        principal: Principal,
        workspace_id: UUID,
    ) -> dict[str, object]:
        # work.read sees the stop state; the stop clients themselves (work.stop)
        # must be able to read it to decide whether to halt (OMP-405).
        if (
            workspace_id not in principal.workspaces
            or (
                "work.read" not in principal.scopes
                and "work.stop" not in principal.scopes
            )
        ):
            raise WorkError("forbidden", status=403)
        try:
            return StopStatusView.model_validate(
                self._store.stop_status(workspace_id, principal.actor_id)
            ).model_dump(mode="json")
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error


