from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .models import CommandEnvelope
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
        "append_evidence": "work.approve",
        "finalize_candidate": "work.approve",
        "create_same_session_child": "work.close",
        "begin_close_attempt": "work.close",
        "seal_audit_manifest": "work.close",
        "reserve_auditor_launch": "work.close",
        "cancel_auditor_launch": "work.close",
        "settle_auditor_launch": "work.close",
        "attest_checkpoint_delivery": "work.close",
        "record_closeout_review": "work.close",
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
                "relation_cycle": 400,
                "idempotency_conflict": 409,
                "revision_conflict": 409,
                "focus_conflict": 409,
                "stale_evidence": 409,
                "completion_blocked": 409,
                "cutover_invariant": 409,
                "unavailable": 503,
            }
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error

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
            statuses = {"invalid_request": 400, "forbidden": 403, "unavailable": 503}
            raise WorkError(
                error.code,
                status=statuses.get(error.code, 409),
                diagnostics=error.diagnostics,
            ) from error
