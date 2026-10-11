from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work import _COMMAND_TYPES, _READS, _manifest_paths, contract_sha256, load_contract
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import IntakeRecordView
from omp_work.v1.models import Approval
from omp_work.v1.server import create_app
from omp_work.v1.store import WorkStoreError


def test_intake_wire_is_in_contract_and_approval_closure() -> None:
    contract = load_contract()
    for command in (
        "start_intake",
        "register_intake_evidence",
        "record_intake_claim",
        "record_intake_decision",
        "supersede_intake_claim",
        "record_intake_issue",
        "link_intake_criterion",
        "assess_intake",
    ):
        assert command in _COMMAND_TYPES
        assert command in contract.command_types

    read = "GET /v1/workspaces/{workspace_id}/intake/{run_id}"
    assert read in _READS
    assert read in contract.reads
    assert contract.reads[contract.reads.index(read) + 1] == (
        "GET /v1/workspaces/{workspace_id}/missions/{mission_id}"
    )

    paths = _manifest_paths()
    assert "api-schema.json" in paths
    assert "decisions/0022-intake-provenance.md" in paths
    approval = Approval.model_validate({
        "contract_version": contract.contract_version,
        "contract_sha256": contract_sha256(),
        "approved_by": "owner",
        "approved_at": "2026-10-11T00:00:00Z",
        "issue": "OMP-522",
    })
    assert approval.issue == "OMP-522"


def default_view_dump(workspace_id: UUID, run_id: str = "run-1", revision: int = 1) -> dict[str, Any]:
    view = IntakeRecordView.model_validate({
        "workspace_id": str(workspace_id),
        "mission_id": "00000000-0000-0000-0000-000000000002",
        "run_id": run_id,
        "revision": revision,
        "snapshot": {
            "run_id": run_id,
            "revision": 0,
            "state": "collecting",
            "artifacts": [],
            "entities": [],
            "claims": [],
            "ontology": {"predicates": [], "dimensions": []},
        },
        "evidence_links": (),
        "criterion_links": (),
        "decisions": (),
        "issues": (),
        "traces": (),
        "assessments": (),
        "provenance": {
            "run_id": run_id,
            "revision": revision,
            "source_sha256": "a" * 64,
        },
    })
    return view.model_dump(mode="json")


class FakeStore:
    def __init__(
        self,
        view_data: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.recorded: list[tuple[str, str]] = []
        self.view_data = view_data
        self.error = error

    def read(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        kind: str,
        value: str,
        candidate_allowlist: frozenset[UUID] | None = None,
        standing: bool = False,
    ) -> dict[str, Any]:
        self.recorded.append((kind, value))
        if self.error is not None:
            raise self.error
        if self.view_data is not None:
            return self.view_data
        run_id = value.split("@")[0]
        revision = int(value.split("@")[1]) if "@" in value else 1
        return default_view_dump(workspace_id=workspace_id, run_id=run_id, revision=revision)


def write_capability(
    capabilities_dir: Path,
    filename: str,
    *,
    token: str,
    actor_id: UUID,
    actor_kind: str = "owner",
    workspaces: list[UUID],
    scopes: list[str],
    candidate_ids: list[UUID] | None = None,
) -> Path:
    capabilities_dir.mkdir(parents=True, exist_ok=True)
    capabilities_dir.chmod(0o700)
    path = capabilities_dir / filename
    data: dict[str, Any] = {
        "token": token,
        "actor_id": str(actor_id),
        "actor_kind": actor_kind,
        "workspaces": [str(ws) for ws in workspaces],
        "scopes": scopes,
    }
    if candidate_ids is not None:
        data["candidate_ids"] = [str(cid) for cid in candidate_ids]
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_work_read_no_query_returns_200_and_records_run_id(tmp_path: Path) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    run_id = "test-run-1"
    capabilities_dir = tmp_path / "capabilities"
    write_capability(
        capabilities_dir,
        "owner.json",
        token="token-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=["work.read"],
    )
    store = FakeStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/{run_id}",
        headers={
            "Authorization": "Bearer token-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 200
    validated = IntakeRecordView.model_validate(response.json())
    assert validated.run_id == run_id
    assert store.recorded == [("intake", run_id)]


def test_revision_query_records_run_id_at_revision(tmp_path: Path) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    run_id = "test-run-2"
    capabilities_dir = tmp_path / "capabilities"
    write_capability(
        capabilities_dir,
        "owner.json",
        token="token-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=["work.read"],
    )
    store = FakeStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/{run_id}?revision=2",
        headers={
            "Authorization": "Bearer token-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 200
    validated = IntakeRecordView.model_validate(response.json())
    assert validated.run_id == run_id
    assert validated.revision == 2
    assert store.recorded == [("intake", f"{run_id}@2")]


@pytest.mark.parametrize("bad_revision", ["0", "foo"])
def test_invalid_revision_returns_400_invalid_request(
    tmp_path: Path, bad_revision: str
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    run_id = "test-run-3"
    capabilities_dir = tmp_path / "capabilities"
    write_capability(
        capabilities_dir,
        "owner.json",
        token="token-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=["work.read"],
    )
    store = FakeStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/{run_id}?revision={bad_revision}",
        headers={
            "Authorization": "Bearer token-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 400
    assert response.status_code != 422
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    assert len(store.recorded) == 0


def test_contract_header_without_authorization_returns_401(tmp_path: Path) -> None:
    workspace_id = uuid4()
    capabilities_dir = tmp_path / "capabilities"
    capabilities_dir.mkdir(parents=True, exist_ok=True)
    capabilities_dir.chmod(0o700)
    store = FakeStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/test-run",
        headers={
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 401
    assert len(store.recorded) == 0


@pytest.mark.parametrize("scopes", [["work.mutate"], ["work.candidate.read"], []])
def test_bearer_without_work_read_returns_403(
    tmp_path: Path, scopes: list[str]
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    capabilities_dir = tmp_path / "capabilities"
    candidate_ids = [uuid4()] if "work.candidate.read" in scopes else None
    write_capability(
        capabilities_dir,
        "caller.json",
        token="token-no-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=scopes,
        candidate_ids=candidate_ids,
    )
    store = FakeStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/test-run",
        headers={
            "Authorization": "Bearer token-no-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "forbidden"
    assert len(store.recorded) == 0


def test_non_view_store_dict_returns_400_invalid_request(tmp_path: Path) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    capabilities_dir = tmp_path / "capabilities"
    write_capability(
        capabilities_dir,
        "owner.json",
        token="token-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=["work.read"],
    )
    store = FakeStore(view_data={"corrupt": "data", "not_a_view": True})
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/test-run",
        headers={
            "Authorization": "Bearer token-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "invalid_request"
    assert store.recorded == [("intake", "test-run")]


@pytest.mark.parametrize(
    "store_code,expected_status",
    [
        ("invalid_request", 400),
        ("forbidden", 403),
        ("artifact_unavailable", 503),
        ("stale_evidence", 409),
        ("unavailable", 503),
        ("unmapped_code", 409),
    ],
)
def test_store_error_status_mapping(
    tmp_path: Path, store_code: str, expected_status: int
) -> None:
    workspace_id = uuid4()
    actor_id = uuid4()
    capabilities_dir = tmp_path / "capabilities"
    write_capability(
        capabilities_dir,
        "owner.json",
        token="token-read",
        actor_id=actor_id,
        workspaces=[workspace_id],
        scopes=["work.read"],
    )
    store = FakeStore(error=WorkStoreError(store_code))
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    app = create_app(config, capabilities_dir=capabilities_dir, store=store)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/intake/test-run",
        headers={
            "Authorization": "Bearer token-read",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )
    assert response.status_code == expected_status
    payload = response.json()
    assert payload["error"]["code"] == store_code
