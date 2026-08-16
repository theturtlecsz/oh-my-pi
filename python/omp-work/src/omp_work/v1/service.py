from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .models import CommandEnvelope
from .store import PostgresWorkStore, WorkStore, WorkStoreError


@dataclass(frozen=True)
class Principal:
    actor_id: UUID
    actor_kind: str
    workspaces: frozenset[UUID]
    scopes: frozenset[str]


class WorkError(Exception):
    def __init__(self, code: str, *, status: int = 400, diagnostics: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code, self.status, self.diagnostics = code, status, diagnostics


class WorkService:
    _scopes = {
        "create_work_batch": "work.mutate", "revise_work": "work.mutate", "set_work_state": "work.mutate", "put_relation": "work.mutate", "remove_relation": "work.mutate", "set_focus": "work.mutate", "clear_focus": "work.mutate", "record_project_health": "work.mutate",
        "append_evidence": "work.approve", "request_closeout": "work.close", "complete_work": "work.close", "stage_import_batch": "work.import", "promote_import_batch": "work.import", "activate_cutover": "work.operate",
    }

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def execute(self, principal: Principal, envelope: CommandEnvelope) -> tuple[object, dict[str, object]]:
        if envelope.workspace_id not in principal.workspaces:
            raise WorkError("forbidden", status=403)
        scope = self._scopes[envelope.command.type]
        if scope not in principal.scopes:
            raise WorkError("forbidden", status=403)
        if envelope.command.type in {"stage_import_batch", "promote_import_batch", "activate_cutover"}:
            raise WorkError("unavailable", status=503)
        try:
            return self._store.execute(envelope, actor_id=principal.actor_id, actor_kind=principal.actor_kind, required_scope=scope)
        except WorkStoreError as error:
            statuses = {
                "invalid_request": 400,
                "relation_cycle": 400,
                "idempotency_conflict": 409,
                "revision_conflict": 409,
                "focus_conflict": 409,
                "stale_evidence": 409,
                "completion_blocked": 409,
                "unavailable": 503,
            }
            raise WorkError(error.code, status=statuses.get(error.code, 409), diagnostics=error.diagnostics) from error

    def read(self, principal: Principal, workspace_id: UUID, kind: str, value: str) -> dict[str, object]:
        self.require_read(principal, workspace_id)
        try:
            return self._store.read(workspace_id, principal.actor_id, kind, value)
        except WorkStoreError as error:
            statuses = {"invalid_request": 400, "unavailable": 503}
            raise WorkError(error.code, status=statuses.get(error.code, 409), diagnostics=error.diagnostics) from error

    @staticmethod
    def require_read(principal: Principal, workspace_id: UUID) -> None:
        if workspace_id not in principal.workspaces or "work.read" not in principal.scopes:
            raise WorkError("forbidden", status=403)
