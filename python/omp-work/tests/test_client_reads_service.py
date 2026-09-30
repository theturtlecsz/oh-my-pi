"""OMP-416: the seven client GET reads on WorkService.

Without a database, a fake store records every call:
- each operation calls its store method with the right workspace, actor and id;
- a work.client-only and a work.read-only principal succeed;
- a work.stop-only principal and a principal from another workspace get 403 and
  the store is never called;
- a non-UUID project ident and a ProjectNotFound both map to invalid_request 400.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from omp_work.project_store import ProjectNotFound
from omp_work.v1.service import CLIENT_READS, Principal, WorkError, WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
OTHER_WORKSPACE = UUID("00000000-0000-7000-8000-000000000011")


class _RecordingStore:
    """Fake WorkStore: records (method, workspace, actor, args) and returns markers."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.missing_project = False

    def _record(self, method: str, workspace_id: UUID, actor_id: UUID, **args):
        self.calls.append(
            {
                "method": method,
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "args": args,
            }
        )

    def list_projects(self, workspace_id: UUID, actor_id: UUID):
        self._record("list_projects", workspace_id, actor_id)
        return {"workspace_id": str(workspace_id), "projects": []}

    def read_project(self, workspace_id: UUID, actor_id: UUID, project_id: UUID):
        self._record("read_project", workspace_id, actor_id, project_id=project_id)
        if self.missing_project:
            raise ProjectNotFound(f"id {project_id}")
        return {"project_id": str(project_id), "key": "k", "name": "N", "kind": "surface"}

    def project_context(self, workspace_id: UUID, actor_id: UUID, project_id: UUID):
        self._record("project_context", workspace_id, actor_id, project_id=project_id)
        if self.missing_project:
            raise ProjectNotFound(f"id {project_id}")
        return {"refs": [], "missions": [], "history": [], "research": []}

    def decisions(self, workspace_id: UUID, actor_id: UUID, **kwargs):
        self._record("decisions", workspace_id, actor_id, **kwargs)
        return {"decisions": []}

    def read(self, workspace_id: UUID, actor_id: UUID, kind: str, value: str, **kwargs):
        self._record("read", workspace_id, actor_id, kind=kind, value=value)
        return {"mission_id": value}

    def receipt(self, workspace_id: UUID, actor_id: UUID, receipt_id):
        self._record("receipt", workspace_id, actor_id, receipt_id=receipt_id)
        return {"receipt_id": str(receipt_id)}

    def stop_status(self, workspace_id: UUID, actor_id: UUID):
        self._record("stop_status", workspace_id, actor_id)
        return {
            "workspace_id": workspace_id,
            "stopped": False,
            "reason": None,
            "changed_at": None,
            "changed_by_actor_kind": None,
        }


def _principal(scopes: frozenset[str], workspaces=frozenset({WORKSPACE})) -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind="client",
        workspaces=workspaces,
        scopes=scopes,
    )


# operation -> (store method, ident, expected args)
_CASES = {
    "project.list": ("list_projects", None, {}),
    "project.context": ("project_context", uuid4(), {}),
    "project.status": ("read_project", uuid4(), {}),
    "project.decisions": ("decisions", uuid4(), {}),
    "mission.status": ("read", uuid4(), {"kind": "mission"}),
    "evidence.inspect": ("receipt", uuid4(), {}),
    "stop.status": ("stop_status", None, {}),
}


@pytest.mark.parametrize("operation", list(_CASES))
def test_each_read_calls_its_store_method_with_the_ids(operation: str) -> None:
    method, ident, extra = _CASES[operation]
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    principal = _principal(frozenset({"work.client"}))

    service.client_read(principal, WORKSPACE, operation, ident)

    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["method"] == method
    assert call["workspace_id"] == WORKSPACE
    assert call["actor_id"] == principal.actor_id
    args = call["args"]
    assert args.get("kind") == extra.get("kind")
    if operation == "mission.status":
        assert args["value"] == str(ident)
    elif operation in ("project.context", "project.status", "project.decisions"):
        assert args["project_id"] == ident
    elif operation == "evidence.inspect":
        assert args["receipt_id"] == ident


def test_client_reads_lists_the_seven_operations() -> None:
    assert set(CLIENT_READS) == set(_CASES)


@pytest.mark.parametrize("scopes", [frozenset({"work.client"}), frozenset({"work.read"})])
def test_client_scope_holders_succeed(scopes: frozenset[str]) -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]

    for operation, (_, ident, _) in _CASES.items():
        service.client_read(_principal(scopes), WORKSPACE, operation, ident)

    assert len(store.calls) == len(_CASES)


def test_stop_only_principal_is_forbidden_and_store_is_not_called() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    principal = _principal(frozenset({"work.stop"}))

    for operation, (_, ident, _) in _CASES.items():
        with pytest.raises(WorkError) as exc_info:
            service.client_read(principal, WORKSPACE, operation, ident)
        assert exc_info.value.status == 403
        assert exc_info.value.code == "forbidden"

    assert store.calls == []


def test_other_workspace_principal_is_forbidden_and_store_is_not_called() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    principal = _principal(frozenset({"work.client"}), workspaces=frozenset({OTHER_WORKSPACE}))

    for operation, (_, ident, _) in _CASES.items():
        with pytest.raises(WorkError) as exc_info:
            service.client_read(principal, WORKSPACE, operation, ident)
        assert exc_info.value.status == 403
        assert exc_info.value.code == "forbidden"

    assert store.calls == []


@pytest.mark.parametrize(
    "operation", ["project.context", "project.status", "project.decisions"]
)
def test_non_uuid_project_ident_is_invalid_request_without_a_store_call(
    operation: str,
) -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]

    with pytest.raises(WorkError) as exc_info:
        service.client_read(_principal(frozenset({"work.client"})), WORKSPACE, operation, "not-a-uuid")

    assert exc_info.value.status == 400
    assert exc_info.value.code == "invalid_request"
    assert store.calls == []


@pytest.mark.parametrize("operation", ["project.context", "project.status"])
def test_project_not_found_is_invalid_request(operation: str) -> None:
    store = _RecordingStore()
    store.missing_project = True
    service = WorkService(store)  # type: ignore[arg-type]

    with pytest.raises(WorkError) as exc_info:
        service.client_read(_principal(frozenset({"work.client"})), WORKSPACE, operation, uuid4())

    assert exc_info.value.status == 400
    assert exc_info.value.code == "invalid_request"


def test_unknown_operation_is_invalid_request_without_a_store_call() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]

    with pytest.raises(WorkError) as exc_info:
        service.client_read(_principal(frozenset({"work.client"})), WORKSPACE, "project.delete")

    assert exc_info.value.status == 400
    assert exc_info.value.code == "invalid_request"
    assert store.calls == []
