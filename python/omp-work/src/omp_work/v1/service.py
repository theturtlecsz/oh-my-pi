from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


from pydantic import ValidationError

from .api_models import DecisionsPage, StopStatusView
from .models import OWNER_APPROVAL_COMMAND_TYPES, CommandEnvelope
from .store import WorkStore, WorkStoreError


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
    }

    def __init__(self, store: WorkStore) -> None:
        self._store = store

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
        if envelope.command.type in {"stage_import_batch", "promote_import_batch"}:
            raise WorkError("unavailable", status=503)
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


