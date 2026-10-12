"""Tests for task capability mode bound to native job lease authority (OMP-595-s04).

Proves:
1. Disposable DB and HTTP workflow reads prove allowlisted candidate read beyond
   initial lease interval after healthy renewal.
2. Wrong candidate returns 403 Forbidden.
3. Wrong workspace returns 403 Forbidden.
4. Mutation attempt (POST commands or client endpoints) returns 403 Forbidden without effects.
5. Stale fence returns 401 Unauthenticated.
6. Job cancellation returns 401 Unauthenticated.
7. Job settlement (sealed or failed) returns 401 Unauthenticated.
8. Hard task expiry returns 401 Unauthenticated even while lease is live in DB.
9. Agent stop engagement returns 401 Unauthenticated without effects.
10. Same resumed holder can read after stop release.
11. Reassignment cannot revive old token (remains 401).
12. DB-read failure refuses (returns 401).
13. Malformed/partial binding refuses at generation and authentication; never falls back to unbounded token.
14. Legacy fixed-expiry mode without binding behaves unchanged (valid until expiry, then 401; timeless valid).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.jobs.admission import claim_job
from omp_work.jobs.cancel import cancel_job
from omp_work.jobs.lease import current_authority, renew_lease, settle_job
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.operations.capabilities import (
    capabilities_dir,
    provision_candidate_reader,
    provision_task_capability,
    write_capability,
)
from omp_work.v1.server import create_app
from test_jobs_worker_loop import _connect_admin, _enqueue, _receipt, _store
from test_stop_pause_resume import _engage, _release

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service", "native_jobs_support")


def _setup_candidate(native_jobs: Any) -> UUID:
    candidate_id = uuid4()
    candidate_sha = (uuid4().hex * 2)[:64]
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates"
            "(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, kind, allocated_at) "
            "VALUES (%s, %s, %s, %s, %s, NULL, 'planned', %s)",
            (
                candidate_id,
                native_jobs.workspace_id,
                native_jobs.item["work_id"],
                native_jobs.item["revision_id"],
                candidate_sha,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "UPDATE omp_work.work_items SET current_candidate_id = %s WHERE work_id = %s",
            (candidate_id, native_jobs.item["work_id"]),
        )
    return candidate_id


def _setup_job(
    native_jobs: Any, store: NativeJobStore, lease_seconds: int = 2
) -> tuple[str, str, int]:
    job_id = f"job-{uuid4()}"
    worker_id = f"worker-{uuid4().hex[:8]}"
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        component_sha256=native_jobs.components["worker"],
        capabilities=["compute.cpu"],
        capacity=4,
    )
    _enqueue(native_jobs, store, job_id=job_id, lease_seconds=lease_seconds)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        job_id=job_id,
    )
    fence = int(claimed["job"]["fence"])
    return job_id, worker_id, fence


def _headers(token: str, workspace_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-OMP-Contract-SHA256": contract_sha256(),
        "X-OMP-Workspace-ID": str(workspace_id),
    }


def _client(native_jobs: Any) -> tuple[TestClient, Path]:
    cap_dir = capabilities_dir(native_jobs.service.config)
    app = create_app(
        native_jobs.service.config,
        capabilities_dir=cap_dir,
    )
    return TestClient(app), cap_dir


def test_allowlisted_read_beyond_initial_interval_after_renewal(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=1)

    client, _ = _client(native_jobs)
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=expires_at,
        name=f"task-renew-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    # Renew lease before initial lease expires, extending lease_expires_at
    renew = renew_lease(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert renew["status"] == "applied"
    assert int(renew["job"]["fence"]) == fence

    # Advance beyond initial 1-second interval
    time.sleep(1.2)

    # Hard expiry is hours away; renewal kept lease live in DB
    authority = current_authority(
        store,
        workspace_id=native_jobs.workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert authority.is_live is True

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["item"]["work_id"] == str(native_jobs.item["work_id"])
    assert data["item"]["candidate"]["candidate_id"] == str(candidate_id)


def test_wrong_candidate_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    wrong_candidate_id = uuid4()
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(wrong_candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-wrong-cand-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_wrong_workspace_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-ws-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    other_workspace = uuid4()
    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, other_workspace))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_mutation_refuses_without_effects(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-mutate-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    # Attempt engage_stop mutation command
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(native_jobs.workspace_id),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": "engage_stop", "payload": {"reason": "unauthorized stop"}},
    }
    response = client.post(
        "/v1/commands",
        headers=_headers(token, native_jobs.workspace_id),
        json=envelope,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"

    # Task capability cannot read stop status (403)
    stop_resp = client.get(
        f"/v1/workspaces/{native_jobs.workspace_id}/stop",
        headers=_headers(token, native_jobs.workspace_id),
    )
    assert stop_resp.status_code == 403

    # Check database directly: stop was not engaged (no effects)
    authority = current_authority(
        store,
        workspace_id=native_jobs.workspace_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    assert authority.is_live is True
    assert authority.is_frozen is False


def test_stale_fence_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    # Provision with mismatched fence (fence + 1)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-fence-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence + 1,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"


def test_cancellation_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-cancel-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    cancel_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        reason="cancelled_for_test",
    )

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"


def test_settlement_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-settle-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    receipt = _receipt(native_jobs, role="audit")
    settle_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
        outcome="succeeded",
        receipts=[receipt],
    )

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"


def test_hard_expiry_refuses(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    # Hard expiry 5 seconds in the past even though lease in DB is live for 60s
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        name=f"task-expired-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"


def test_stop_refuses_without_effects_and_resumes_after_release(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-stop-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"

    # Before stop: reads OK
    resp_before = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert resp_before.status_code == 200

    # Engage stop
    _engage(native_jobs.service, native_jobs.workspace_id, uuid4())

    try:
        # While stopped: refuses (401) without effects
        resp_stopped = client.get(url, headers=_headers(token, native_jobs.workspace_id))
        assert resp_stopped.status_code == 401
        assert resp_stopped.json()["detail"] == "unauthenticated"
    finally:
        # Release stop
        _release(native_jobs.service, native_jobs.workspace_id)

    # After release: same resumed holder can read again
    resp_after = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert resp_after.status_code == 200


def test_reassignment_cannot_revive_old_token(native_jobs: Any) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id_1, fence_1 = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path_1 = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-reassign-1-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id_1,
        fence=fence_1,
    )
    token_1 = str(json.loads(cap_path_1.read_text())["token"])

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    assert client.get(url, headers=_headers(token_1, native_jobs.workspace_id)).status_code == 200

    # Reassign job to worker_2 with fence_2 (fence bumped)
    worker_id_2 = f"worker-{uuid4().hex[:8]}"
    fence_2 = fence_1 + 1
    with _connect_admin(native_jobs) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET worker_id = %s, fence = %s, lease_expires_at = clock_timestamp() + interval '60 seconds' WHERE job_id = %s",
            (worker_id_2, fence_2, job_id),
        )

    # Worker 2 has a live active lease
    auth_2 = current_authority(
        store,
        workspace_id=native_jobs.workspace_id,
        job_id=job_id,
        worker_id=worker_id_2,
        fence=fence_2,
    )
    assert auth_2.is_live is True

    # But worker 1's old token cannot read: reassignment cannot revive old token
    resp_old = client.get(url, headers=_headers(token_1, native_jobs.workspace_id))
    assert resp_old.status_code == 401
    assert resp_old.json()["detail"] == "unauthenticated"


def test_db_read_failure_refuses(native_jobs: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(native_jobs)
    candidate_id = _setup_candidate(native_jobs)
    job_id, worker_id, fence = _setup_job(native_jobs, store, lease_seconds=60)

    client, _ = _client(native_jobs)
    cap_path = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"task-db-err-{uuid4().hex[:8]}",
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
    )
    token = str(json.loads(cap_path.read_text())["token"])

    from omp_work.v1 import server as server_module

    def failing_authority(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("simulated database read failure")

    monkeypatch.setattr(server_module, "current_authority", failing_authority)

    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    response = client.get(url, headers=_headers(token, native_jobs.workspace_id))
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"


def test_malformed_and_partial_binding_refuses(native_jobs: Any) -> None:
    candidate_id = _setup_candidate(native_jobs)
    client, cap_dir = _client(native_jobs)

    # 1. Generation checks raise ValueError
    with pytest.raises(ValueError, match="complete lease identity"):
        provision_task_capability(
            native_jobs.service.config,
            workspace_id=native_jobs.workspace_id,
            candidate_ids=(candidate_id,),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            name="partial-1",
            job_id="job-1",  # worker_id and fence missing
        )

    with pytest.raises(ValueError, match="complete lease identity"):
        provision_task_capability(
            native_jobs.service.config,
            workspace_id=native_jobs.workspace_id,
            candidate_ids=(candidate_id,),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            name="partial-2",
            job_id="job-1",
            worker_id="worker-1",
            fence=-1,  # negative fence
        )

    with pytest.raises(ValueError, match="task-agent"):
        write_capability(
            native_jobs.service.config,
            name="bad-kind",
            actor_id=uuid4(),
            actor_kind="owner",
            workspaces=(native_jobs.workspace_id,),
            scopes=("work.candidate.read",),
            candidate_ids=(candidate_id,),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            job_id="job-1",
            worker_id="worker-1",
            fence=0,
        )

    with pytest.raises(ValueError, match="only 'work.candidate.read' scope"):
        write_capability(
            native_jobs.service.config,
            name="bad-scope",
            actor_id=uuid4(),
            actor_kind="task-agent",
            workspaces=(native_jobs.workspace_id,),
            scopes=("work.candidate.read", "work.mutate"),
            candidate_ids=(candidate_id,),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            job_id="job-1",
            worker_id="worker-1",
            fence=0,
        )

    with pytest.raises(ValueError, match="exactly one workspace"):
        write_capability(
            native_jobs.service.config,
            name="bad-ws",
            actor_id=uuid4(),
            actor_kind="task-agent",
            workspaces=(native_jobs.workspace_id, uuid4()),
            scopes=("work.candidate.read",),
            candidate_ids=(candidate_id,),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            job_id="job-1",
            worker_id="worker-1",
            fence=0,
        )

    with pytest.raises(ValueError, match="candidate_ids allowlist"):
        write_capability(
            native_jobs.service.config,
            name="bad-cand",
            actor_id=uuid4(),
            actor_kind="task-agent",
            workspaces=(native_jobs.workspace_id,),
            scopes=("work.candidate.read",),
            candidate_ids=(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            job_id="job-1",
            worker_id="worker-1",
            fence=0,
        )

    with pytest.raises(ValueError, match="finite expires_at"):
        write_capability(
            native_jobs.service.config,
            name="bad-exp",
            actor_id=uuid4(),
            actor_kind="task-agent",
            workspaces=(native_jobs.workspace_id,),
            scopes=("work.candidate.read",),
            candidate_ids=(candidate_id,),
            expires_at=None,
            job_id="job-1",
            worker_id="worker-1",
            fence=0,
        )

    # 2. Server authentication: files with malformed/partial binding refuse (never unbounded)
    malformed_cases: list[dict[str, object]] = [
        {"job_id": "job-1"},  # missing worker_id and fence
        {"job_id": "job-1", "worker_id": "w1", "fence": "nan"},  # fence not int
        {"job_id": "job-1", "worker_id": "w1", "fence": -2},  # negative fence
        {"job_id": "", "worker_id": "w1", "fence": 0},  # empty job_id
        {"job_id": "job-1", "worker_id": "", "fence": 0},  # empty worker_id
        {"job_id": "job-1", "worker_id": "w1", "fence": 0, "actor_kind": "client"},  # wrong actor_kind
        {"job_id": "job-1", "worker_id": "w1", "fence": 0, "scopes": ["work.read"]},  # wrong scope
        {"job_id": "job-1", "worker_id": "w1", "fence": 0, "workspaces": []},  # zero workspaces
        {
            "job_id": "job-1",
            "worker_id": "w1",
            "fence": 0,
            "workspaces": [str(native_jobs.workspace_id), str(uuid4())],
        },  # multiple workspaces
        {"job_id": "job-1", "worker_id": "w1", "fence": 0, "candidate_ids": []},  # empty candidates
        {"job_id": "job-1", "worker_id": "w1", "fence": 0, "expires_at": None},  # missing expires_at
    ]

    for idx, extra in enumerate(malformed_cases):
        token = f"malformed-token-{idx}"
        data: dict[str, object] = {
            "token": token,
            "actor_id": str(uuid4()),
            "actor_kind": "task-agent",
            "workspaces": [str(native_jobs.workspace_id)],
            "scopes": ["work.candidate.read"],
            "candidate_ids": [str(candidate_id)],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        data.update(extra)
        file_path = cap_dir / f"malformed-{idx}.json"
        file_path.write_text(json.dumps(data))
        file_path.chmod(0o600)

        url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
        resp = client.get(url, headers=_headers(token, native_jobs.workspace_id))
        assert resp.status_code == 401, f"case {idx} did not refuse: {resp.status_code}"


def test_legacy_expiry_cases(native_jobs: Any) -> None:
    candidate_id = _setup_candidate(native_jobs)
    client, _ = _client(native_jobs)

    # 1. Legacy task capability with future expires_at: 200 OK
    leg_future = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        name=f"legacy-future-{uuid4().hex[:8]}",
    )
    token_future = str(json.loads(leg_future.read_text())["token"])
    url = f"/v1/work-items/{native_jobs.item['key']}/workflow"
    resp = client.get(url, headers=_headers(token_future, native_jobs.workspace_id))
    assert resp.status_code == 200

    # 2. Legacy task capability with past expires_at: 401 Unauthenticated
    leg_past = provision_task_capability(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
        name=f"legacy-past-{uuid4().hex[:8]}",
    )
    token_past = str(json.loads(leg_past.read_text())["token"])
    resp = client.get(url, headers=_headers(token_past, native_jobs.workspace_id))
    assert resp.status_code == 401

    # 3. Legacy timeless candidate reader (no expires_at): 200 OK
    leg_timeless = provision_candidate_reader(
        native_jobs.service.config,
        workspace_id=native_jobs.workspace_id,
        candidate_ids=(candidate_id,),
        name=f"legacy-timeless-{uuid4().hex[:8]}",
    )
    token_timeless = str(json.loads(leg_timeless.read_text())["token"])
    resp = client.get(url, headers=_headers(token_timeless, native_jobs.workspace_id))
    assert resp.status_code == 200
