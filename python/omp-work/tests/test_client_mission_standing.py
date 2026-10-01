"""OMP-423-s02: client mission.status serves the standing mission view.

No database. A fake store and a client capability go through create_app, the
same shape as test_client_reads_http.py. A client GET of mission.status reads
the mission once with standing=True. ClientResponse.decisions is the pending
decision ids in order, state is the mission status, and result carries
pending_decisions and jobs_in_flight. An empty pending list yields decisions
[]. The legacy GET /v1/workspaces/{ws}/missions/{id} reads without
standing=True, and its body has neither key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.capabilities import CLIENT_SCOPES
from omp_work.operations.config import OperationsConfig
from omp_work.v1.server import create_app

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
MISSION = UUID("00000000-0000-7000-8000-000000000030")
DECISION_A = UUID("00000000-0000-7000-8000-000000000041")
DECISION_B = UUID("00000000-0000-7000-8000-000000000042")
JOB_ID = UUID("00000000-0000-7000-8000-000000000051")
WORK_ID = UUID("00000000-0000-7000-8000-000000000052")

_STATUS = "running"
_PENDING = (
    {"decision_id": str(DECISION_A), "status": "pending", "question": "ship?"},
    {"decision_id": str(DECISION_B), "status": "pending", "question": "wait?"},
)
_JOBS = (
    {
        "job_id": str(JOB_ID),
        "work_id": str(WORK_ID),
        "kind": "implement",
        "status": "admitted",
        "attempt": 1,
        "lease_expires_at": "2026-10-01T12:00:00+00:00",
    },
)


class _RecordingStore:
    """Fake WorkStore: records each read and returns a standing view when asked."""

    def __init__(self, pending: tuple[dict[str, object], ...] = _PENDING) -> None:
        self.pending = pending
        self.reads: list[dict[str, object]] = []

    def read(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        kind: str,
        value: str,
        **kwargs: object,
    ) -> dict[str, object]:
        standing = kwargs.get("standing", False)
        self.reads.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "kind": kind,
                "value": value,
                "kwargs": dict(kwargs),
            }
        )
        view: dict[str, object] = {
            "mission_id": value,
            "status": _STATUS,
            "objective": "ship the standing view",
        }
        if standing is True:
            view["pending_decisions"] = list(self.pending)
            view["jobs_in_flight"] = list(_JOBS)
        return view


def _capabilities(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    path = directory / "client.json"
    path.write_text(
        json.dumps(
            {
                "token": "client-token",
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(WORKSPACE)],
                "scopes": list(CLIENT_SCOPES),
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


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer client-token",
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _client_mission(client: TestClient) -> dict[str, Any]:
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/client/missions/{MISSION}",
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_client_mission_status_reads_standing_and_projects_decisions(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    body = _client_mission(_http(tmp_path, store))

    assert len(store.reads) == 1
    call = store.reads[0]
    assert call["kind"] == "mission"
    assert call["value"] == str(MISSION)
    assert call["workspace_id"] == WORKSPACE
    assert call["kwargs"]["standing"] is True

    assert body["operation"] == "mission.status"
    assert body["state"] == _STATUS
    assert body["decisions"] == [str(DECISION_A), str(DECISION_B)]
    assert body["result"]["status"] == _STATUS
    assert body["result"]["pending_decisions"] == list(_PENDING)
    assert body["result"]["jobs_in_flight"] == list(_JOBS)


def test_mission_with_no_pending_decisions_returns_empty_decisions(
    tmp_path: Path,
) -> None:
    store = _RecordingStore(pending=())
    body = _client_mission(_http(tmp_path, store))

    assert store.reads[0]["kwargs"]["standing"] is True
    assert body["state"] == _STATUS
    assert body["decisions"] == []
    assert body["result"]["pending_decisions"] == []
    assert body["result"]["jobs_in_flight"] == list(_JOBS)


def test_legacy_mission_read_omits_standing_keys(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/missions/{MISSION}",
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(store.reads) == 1
    call = store.reads[0]
    assert call["kind"] == "mission"
    assert call["value"] == str(MISSION)
    assert "standing" not in call["kwargs"]
    assert "pending_decisions" not in body
    assert "jobs_in_flight" not in body
