"""OMP-416: the seven client GETs over HTTP.

Without a database, a fake store (same shape as test_agent_stop_authz.py)
is passed to create_app. A client capability holds CLIENT_SCOPES. Each route
returns a client body, diagnostic fields stay out of the body unless
?detail=true, a work.stop-only client is 403 with no request_id, and a
missing contract header is 409 contract_mismatch.

stop.status is validated through StopStatusView before it reaches the client
body, so the fake returns that shape and the detail map stays empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.capabilities import CLIENT_SCOPES, CLIENT_STOP_ONLY_SCOPES
from omp_work.operations.config import OperationsConfig
from omp_work.v1.server import create_app
from omp_work.v1.store_shared import WorkStoreError

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
PROJECT = UUID("00000000-0000-7000-8000-000000000020")
MISSION = UUID("00000000-0000-7000-8000-000000000030")
RECEIPT = UUID("00000000-0000-7000-8000-000000000040")
DECISION_A = UUID("00000000-0000-7000-8000-000000000041")
DECISION_B = UUID("00000000-0000-7000-8000-000000000042")

_WORKER = "worker-7"
_MAX_TOKENS = 4096
_WORKTREE = "/tmp/client-wt"
_SECRET_KEYS = frozenset({"worker_id", "max_tokens", "worktree"})
_PREFIX = [
    "outcome",
    "state",
    "evidence",
    "blockers",
    "decisions",
    "artifacts",
    "operation",
    "contract",
]

_BASE = f"/v1/workspaces/{WORKSPACE}/client"
_ROUTES = (
    ("project.list", f"{_BASE}/projects"),
    ("project.context", f"{_BASE}/projects/{PROJECT}/context"),
    ("project.status", f"{_BASE}/projects/{PROJECT}/status"),
    ("project.decisions", f"{_BASE}/projects/{PROJECT}/decisions"),
    ("mission.status", f"{_BASE}/missions/{MISSION}"),
    ("evidence.inspect", f"{_BASE}/evidence/{RECEIPT}"),
    ("stop.status", f"{_BASE}/stop"),
)
_FIELDS = {
    "project.list": (None, [], []),
    "project.context": (None, [], []),
    "project.status": (None, [], []),
    "project.decisions": (None, [], [str(DECISION_A), str(DECISION_B)]),
    "mission.status": ("paused", [], []),
    "evidence.inspect": (None, [str(RECEIPT)], []),
    "stop.status": ("running", [], []),
}


class _RecordingStore:
    """Fake WorkStore: records each read and returns a view with diagnostics."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.stopped = False
        self.fail: tuple[str, ...] | None = None

    def list_projects(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        self.calls.append(("list_projects", workspace_id, actor_id))
        if self.fail is not None:
            raise WorkStoreError("invalid_request", self.fail)
        return _leaky("project.list", workspace_id=str(workspace_id))

    def project_context(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        self.calls.append(("project_context", workspace_id, actor_id, project_id))
        return _leaky("project.context", project_id=str(project_id))

    def read_project(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        self.calls.append(("read_project", workspace_id, actor_id, project_id))
        return _leaky("project.status", project_id=str(project_id))

    def decisions(
        self, workspace_id: UUID, actor_id: UUID, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append(("decisions", workspace_id, actor_id, kwargs))
        return _leaky(
            "project.decisions",
            decisions=[
                {"decision_id": str(DECISION_A), "question": "ship"},
                {"decision_id": str(DECISION_B), "question": "wait"},
            ],
        )

    def read(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        kind: str,
        value: str,
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append(("read", workspace_id, actor_id, kind, value))
        return _leaky("mission.status", mission_id=value, status="paused")

    def receipt(
        self, workspace_id: UUID, actor_id: UUID, receipt_id: object
    ) -> dict[str, object]:
        self.calls.append(("receipt", workspace_id, actor_id, receipt_id))
        return _leaky("evidence.inspect", receipt_id=str(receipt_id))

    def stop_status(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        self.calls.append(("stop_status", workspace_id, actor_id))
        return {
            "workspace_id": workspace_id,
            "stopped": self.stopped,
            "reason": None,
            "changed_at": None,
            "changed_by_actor_kind": None,
        }


def _leaky(operation: str, **extra: object) -> dict[str, object]:
    view: dict[str, object] = {
        "marker": operation,
        "worker_id": _WORKER,
        "budget": {"max_tokens": _MAX_TOKENS, "currency": "USD"},
        "items": [{"worktree": _WORKTREE, "name": "keep"}],
    }
    view.update(extra)
    return view


def _capabilities(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, token, scopes in (
        ("client", "client-token", list(CLIENT_SCOPES)),
        ("stop-only", "stop-only-token", list(CLIENT_STOP_ONLY_SCOPES)),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": token,
                    "actor_id": str(uuid4()),
                    "actor_kind": "client",
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _http(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def _headers(token: str, *, contract: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if contract:
        headers["X-OMP-Contract-SHA256"] = contract_sha256()
    return headers


def _assert_no_secret_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in _SECRET_KEYS
            _assert_no_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_secret_keys(item)


def _assert_no_request_id(value: object) -> None:
    if isinstance(value, dict):
        assert "request_id" not in value
        for item in value.values():
            _assert_no_request_id(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_request_id(item)


def _assert_read(body: dict[str, Any], operation: str) -> None:
    assert list(body.keys())[:8] == _PREFIX
    assert body["outcome"] == "read"
    assert body["operation"] == operation
    assert body["contract"] == "client.omp.dev/v1"
    assert body["blockers"] == []
    assert body["artifacts"] == []
    state, evidence, decisions = _FIELDS[operation]
    assert body["state"] == state
    assert body["evidence"] == evidence
    assert body["decisions"] == decisions
    if operation == "stop.status":
        assert body["result"]["stopped"] is False
        assert body["result"]["workspace_id"] == str(WORKSPACE)
        return
    assert body["result"]["marker"] == operation
    assert body["result"]["budget"] == {"currency": "USD"}
    assert body["result"]["items"] == [{"name": "keep"}]


def test_each_route_returns_a_client_body(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    headers = _headers("client-token")

    for operation, path in _ROUTES:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (operation, response.text)
        body = response.json()
        _assert_read(body, operation)
        assert body["detail"] is None
        _assert_no_secret_keys(body)

        detailed = client.get(path, headers=headers, params={"detail": "true"})
        assert detailed.status_code == 200, (operation, detailed.text)
        detail_body = detailed.json()
        _assert_read(detail_body, operation)
        for key, value in detail_body.items():
            if key != "detail":
                _assert_no_secret_keys(value)
        if operation == "stop.status":
            assert detail_body["detail"] == {}
            continue
        assert detail_body["detail"] == {
            "worker_id": _WORKER,
            "budget.max_tokens": _MAX_TOKENS,
            "items.0.worktree": _WORKTREE,
        }

    store.stopped = True
    stopped = client.get(f"{_BASE}/stop", headers=headers)
    assert stopped.status_code == 200
    stopped_body = stopped.json()
    assert stopped_body["state"] == "stopped"
    assert stopped_body["result"]["stopped"] is True
    _assert_no_secret_keys(stopped_body)


def test_stop_only_client_is_forbidden_without_request_id(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    headers = _headers("stop-only-token")

    for operation, path in _ROUTES:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, (operation, response.text)
        body = response.json()
        assert body == {"error": {"code": "forbidden", "diagnostics": []}}
        _assert_no_request_id(body)

    assert store.calls == []


def test_missing_contract_header_is_contract_mismatch(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    headers = _headers("client-token", contract=False)

    for operation, path in _ROUTES:
        response = client.get(path, headers=headers)
        assert response.status_code == 409, (operation, response.text)
        body = response.json()
        assert body["error"]["code"] == "contract_mismatch"
        assert len(body["error"]["diagnostics"]) == 3
        _assert_no_request_id(body)

    assert store.calls == []


def test_work_error_diagnostics_are_capped_at_eight(tmp_path: Path) -> None:
    store = _RecordingStore()
    store.fail = tuple(f"diagnostic-{index}" for index in range(10))
    client = _http(tmp_path, store)
    headers = _headers("client-token")

    response = client.get(f"{_BASE}/projects", headers=headers)
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["diagnostics"] == [f"diagnostic-{index}" for index in range(8)]
    _assert_no_request_id(body)

    detailed = client.get(
        f"{_BASE}/projects", headers=headers, params={"detail": "true"}
    )
    assert detailed.status_code == 400
    detailed_body = detailed.json()
    assert detailed_body["error"]["diagnostics"] == body["error"]["diagnostics"]
    assert "request_id" not in detailed_body["error"]
    assert detailed_body["detail"] == {"request_id": None, "correlation_id": None}
