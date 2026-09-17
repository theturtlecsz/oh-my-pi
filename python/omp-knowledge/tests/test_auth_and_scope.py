from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.native.binding import ScopeViolationError, assert_retrieval_allowed
from omp_knowledge.server import create_app
from omp_work.knowledge_contracts import (
    RepositoryBinding,
    RepositoryBindingState,
)
from support.fixtures import (
    load_staged_fixture,
    make_synthetic_enola_snapshot_ref,
    write_test_capability_file,
)
from support.null_engine import NullEngine


@pytest.fixture
def auth_setup(pg_cluster: KnowledgeConfig):
    cap_dir = pg_cluster.capabilities_dir
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    engine = NullEngine()
    app = create_app(pg_cluster, engine=engine)
    with TestClient(app) as client:
        ws_allowed = uuid4()
        ws_forbidden = uuid4()
        token_read = "tok_read_only_12345"
        token_ingest = "tok_ingest_allowed_67890"

        write_test_capability_file(
            cap_dir,
            token=token_read,
            actor_id=uuid4(),
            workspaces=[ws_allowed],
            scopes=["knowledge.read"],
            name="reader",
        )
        write_test_capability_file(
            cap_dir,
            token=token_ingest,
            actor_id=uuid4(),
            workspaces=[ws_allowed],
            scopes=["knowledge.read", "knowledge.ingest"],
            name="ingester",
        )

        yield {
            "client": client,
            "cap_dir": cap_dir,
            "ws_allowed": ws_allowed,
            "ws_forbidden": ws_forbidden,
            "token_read": token_read,
            "token_ingest": token_ingest,
        }


def test_missing_and_malformed_bearer(auth_setup: dict) -> None:
    client: TestClient = auth_setup["client"]

    # Missing header
    res1 = client.get("/v1/status")
    assert res1.status_code == 401
    assert res1.json()["detail"]["error"]["code"] == "unauthenticated"

    # Malformed header
    res2 = client.get("/v1/status", headers={"Authorization": "Bearer non-existent-token"})
    assert res2.status_code == 401
    assert res2.json()["detail"]["error"]["code"] == "unauthenticated"


def test_insufficient_scope_refusal(auth_setup: dict) -> None:
    client: TestClient = auth_setup["client"]
    token_read = auth_setup["token_read"]
    ws_allowed = auth_setup["ws_allowed"]

    # Reader can access status
    res_status = client.get("/v1/status", headers={"Authorization": f"Bearer {token_read}"})
    assert res_status.status_code == 200

    repo_id = uuid4()
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("A")

    valid_payload = {
        "kind": "code_snapshot",
        "operation_id": str(uuid4()),
        "workspace_id": str(ws_allowed),
        "repository_id": str(repo_id),
        "snapshot_id": snap_ref.snapshot_id,
        "snapshot_ref": snap_ref.model_dump(mode="json"),
        "facts_jsonl": facts_b.decode("utf-8"),
        "receipt_json": receipt_b.decode("utf-8"),
        "insights_json": insights_b.decode("utf-8"),
    }

    # Reader cannot ingest (requires knowledge.ingest)
    res_ingest = client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {token_read}"},
        json=valid_payload,
    )
    assert res_ingest.status_code == 403
    assert "missing_scope_knowledge.ingest" in res_ingest.json()["detail"]["error"]["diagnostics"]


def test_workspace_isolation_refusal(auth_setup: dict) -> None:
    client: TestClient = auth_setup["client"]
    token_ingest = auth_setup["token_ingest"]
    ws_forbidden = auth_setup["ws_forbidden"]

    repo_id = uuid4()
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("A")

    # Ingester attempts to target a workspace not in its capability file
    res = client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {token_ingest}"},
        json={
            "kind": "code_snapshot",
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_forbidden),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref.snapshot_id,
            "snapshot_ref": snap_ref.model_dump(mode="json"),
            "facts_jsonl": facts_b.decode("utf-8"),
            "receipt_json": receipt_b.decode("utf-8"),
            "insights_json": insights_b.decode("utf-8"),
        },
    )
    assert res.status_code == 403
    assert "workspace_not_permitted" in res.json()["detail"]["error"]["diagnostics"]


def test_cross_repository_retrieval_unbound_refusal() -> None:
    repo_unbound = uuid4()
    repo_target = uuid4()

    unbound_binding = RepositoryBinding(
        repository_id=repo_unbound,
        native_repository_id=None,
        state=RepositoryBindingState.UNBOUND,
    )

    with pytest.raises(ScopeViolationError, match="Cross-repository retrieval forbidden"):
        assert_retrieval_allowed(unbound_binding, repo_target)


def test_rebuild_unauthorized_workspace_refusal(auth_setup: dict) -> None:
    client: TestClient = auth_setup["client"]
    cap_dir = auth_setup["cap_dir"]
    ws_allowed = auth_setup["ws_allowed"]
    ws_forbidden = auth_setup["ws_forbidden"]

    token_admin = "tok_admin_12345"
    write_test_capability_file(
        cap_dir,
        token=token_admin,
        actor_id=uuid4(),
        workspaces=[ws_allowed],
        scopes=["knowledge.read", "knowledge.admin"],
        name="admin",
    )

    # Calling rebuild with a workspace not permitted by capability returns 403 forbidden
    res = client.post(
        "/v1/rebuild",
        headers={"Authorization": f"Bearer {token_admin}"},
        json={
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_forbidden),
            "repository_id": str(uuid4()),
            "snapshot_id": "a" * 64,
        },
    )
    assert res.status_code == 403
    assert res.json()["detail"]["error"]["code"] == "forbidden"

    # Calling rebuild with permitted workspace for non-existent snapshot returns 404 snapshot_not_found
    res_not_found = client.post(
        "/v1/rebuild",
        headers={"Authorization": f"Bearer {token_admin}"},
        json={
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_allowed),
            "repository_id": str(uuid4()),
            "snapshot_id": "a" * 64,
        },
    )
    assert res_not_found.status_code == 404
    assert res_not_found.json()["detail"]["error"]["code"] == "snapshot_not_found"


def test_job_read_foreign_and_unknown_non_disclosure(auth_setup: dict, pg_cluster: KnowledgeConfig) -> None:
    """F2: Verify GET /v1/jobs/{operation_id} has generic non-disclosure (404) for foreign operation probes."""
    from omp_knowledge.storage.db import get_db_connection

    client: TestClient = auth_setup["client"]
    token_read = auth_setup["token_read"]
    ws_allowed = auth_setup["ws_allowed"]
    ws_forbidden = auth_setup["ws_forbidden"]

    foreign_op_id = uuid4()
    owner_op_id = uuid4()
    unknown_op_id = uuid4()
    repo_id = uuid4()

    conn = get_db_connection(pg_cluster)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound') ON CONFLICT DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, created_at, updated_at
                    ) VALUES
                    (%s, %s, %s, NULL, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 'completed', 1, clock_timestamp(), clock_timestamp()),
                    (%s, %s, %s, NULL, 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789', 'completed', 1, clock_timestamp(), clock_timestamp())
                    """,
                    (foreign_op_id, ws_forbidden, repo_id, owner_op_id, ws_allowed, repo_id),
                )
    finally:
        conn.close()
    headers = {"Authorization": f"Bearer {token_read}"}

    # 1. Foreign operation probe returns 404 (non-disclosure, NOT 403)
    res_foreign = client.get(f"/v1/jobs/{foreign_op_id}", headers=headers)
    assert res_foreign.status_code == 404
    assert res_foreign.json()["detail"]["error"]["code"] == "not_found"

    # 2. Unknown operation probe returns 404
    res_unknown = client.get(f"/v1/jobs/{unknown_op_id}", headers=headers)
    assert res_unknown.status_code == 404
    assert res_unknown.json()["detail"]["error"]["code"] == "not_found"

    # 3. Owner operation read returns 200 with job content
    res_owner = client.get(f"/v1/jobs/{owner_op_id}", headers=headers)
    assert res_owner.status_code == 200
    assert res_owner.json()["operation_id"] == str(owner_op_id)
    assert res_owner.json()["state"] == "completed"


def test_cross_workspace_snapshot_reservation_non_disclosure_and_no_squat(auth_setup: dict, pg_cluster: KnowledgeConfig) -> None:
    """F1: Verify cross-workspace ingest cannot learn foreign operation_id or manifest hash, returns 403, and cannot squat."""
    client: TestClient = auth_setup["client"]
    cap_dir = auth_setup["cap_dir"]
    ws_allowed = auth_setup["ws_allowed"]
    ws_forbidden = auth_setup["ws_forbidden"]
    token_ingest = auth_setup["token_ingest"]

    # Also grant ingest to foreign workspace under a different token
    foreign_token = "tok_foreign_workspace"
    write_test_capability_file(
        cap_dir,
        token=foreign_token,
        actor_id=uuid4(),
        workspaces=[ws_forbidden],
        scopes=["knowledge.read", "knowledge.ingest"],
        name="foreign_actor",
    )

    repo_id = uuid4()
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")
    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    foreign_op_id = uuid4()

    # 1. Foreign workspace ingests snapshot A
    res_foreign = client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {foreign_token}"},
        json={
            "kind": "code_snapshot",
            "operation_id": str(foreign_op_id),
            "workspace_id": str(ws_forbidden),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref.snapshot_id,
            "snapshot_ref": snap_ref.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_foreign.status_code == 200

    # 2. Permitted workspace attempts to ingest under the SAME snapshot_id with different payload
    facts_b, receipt_b, insights_b, _ = load_staged_fixture("B")
    rec_dict = json.loads(receipt_b.decode("utf-8"))
    rec_dict["snapshot_id"] = snap_ref.snapshot_id
    mutated_receipt_b = json.dumps(rec_dict).encode("utf-8")

    attacker_op_id = uuid4()
    res_attacker = client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {token_ingest}"},
        json={
            "kind": "code_snapshot",
            "operation_id": str(attacker_op_id),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref.snapshot_id,
            "snapshot_ref": snap_ref.model_dump(mode="json"),
            "facts_jsonl": facts_b.decode("utf-8"),
            "receipt_json": mutated_receipt_b.decode("utf-8"),
            "insights_json": insights_b.decode("utf-8"),
        },
    )
    # Must fail with 403 forbidden without leaking foreign_op_id or foreign manifest hash
    assert res_attacker.status_code == 403
    err_text = json.dumps(res_attacker.json())
    assert str(foreign_op_id) not in err_text
    assert "SnapshotConflictError" not in err_text

    # 3. Permitted workspace also cannot squat with identical manifest
    res_squat = client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {token_ingest}"},
        json={
            "kind": "code_snapshot",
            "operation_id": str(uuid4()),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": snap_ref.snapshot_id,
            "snapshot_ref": snap_ref.model_dump(mode="json"),
            "facts_jsonl": facts_a.decode("utf-8"),
            "receipt_json": receipt_a.decode("utf-8"),
            "insights_json": insights_a.decode("utf-8"),
        },
    )
    assert res_squat.status_code == 403
    err_squat_text = json.dumps(res_squat.json())
    assert str(foreign_op_id) not in err_squat_text


def test_rebuild_404_and_400_probes_do_not_create_durable_job_rows(
    auth_setup: dict, pg_cluster: KnowledgeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9: Rebuild 404 and 400 probes must not admit durable jobs or create FAILED rows, and must close connections."""
    import omp_knowledge.server as server_mod
    from omp_knowledge.storage.db import get_db_connection
    from omp_work.v1.canonical import sha256

    client: TestClient = auth_setup["client"]
    cap_dir = auth_setup["cap_dir"]
    ws_allowed = auth_setup["ws_allowed"]

    opened_connections: list = []
    real_get_conn = server_mod.get_db_connection

    def tracked_get_conn(cfg):
        c = real_get_conn(cfg)
        opened_connections.append(c)
        return c

    monkeypatch.setattr(server_mod, "get_db_connection", tracked_get_conn)

    token_admin = "tok_admin_rebuild_probes"
    write_test_capability_file(
        cap_dir,
        token=token_admin,
        actor_id=uuid4(),
        workspaces=[ws_allowed],
        scopes=["knowledge.read", "knowledge.admin"],
        name="admin_probe",
    )
    headers = {"Authorization": f"Bearer {token_admin}"}
    repo_id = uuid4()
    probe_op_404 = uuid4()
    probe_op_400 = uuid4()

    # 1. 404 probe: non-existent snapshot
    res_404 = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(probe_op_404),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": "f" * 64,
        },
    )
    assert res_404.status_code == 404
    assert len(opened_connections) == 1
    assert opened_connections[0].closed is True

    # 2. 400 probe: snapshot exists but has no staged/published publication
    snap_id_unpub = "0" * 64
    conn = get_db_connection(pg_cluster)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.repositories (repository_id, state)
                    VALUES (%s, 'unbound') ON CONFLICT DO NOTHING
                    """,
                    (repo_id,),
                )
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.snapshots (
                        snapshot_id, workspace_id, repository_id, base_commit, tree_sha, manifest
                    ) VALUES (%s, %s, %s, 'commit', 'tree', '{}'::jsonb)
                    """,
                    (snap_id_unpub, ws_allowed, repo_id),
                )
    finally:
        conn.close()

    res_400 = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(probe_op_400),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id_unpub,
        },
    )
    assert res_400.status_code == 400
    assert len(opened_connections) == 2
    assert opened_connections[1].closed is True

    # 3. Terminal job replay: connection must close on idempotent replay
    probe_op_replay = uuid4()
    h_replay = sha256(
        {
            "operation_id": str(probe_op_replay),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id_unpub,
        }
    )
    conn = get_db_connection(pg_cluster)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.ingestion_jobs (
                        operation_id, workspace_id, repository_id, snapshot_id,
                        request_sha256, state, attempt_count, response, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'completed', 1, '{}'::jsonb, clock_timestamp(), clock_timestamp())
                    """,
                    (probe_op_replay, ws_allowed, repo_id, snap_id_unpub, h_replay),
                )
    finally:
        conn.close()

    res_replay = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(probe_op_replay),
            "workspace_id": str(ws_allowed),
            "repository_id": str(repo_id),
            "snapshot_id": snap_id_unpub,
        },
    )
    assert res_replay.status_code == 200
    assert len(opened_connections) == 3
    assert opened_connections[2].closed is True

    # 4. Verify in DB that ZERO rows were created for both 404 and 400 probes
    conn = get_db_connection(pg_cluster)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM omp_knowledge.ingestion_jobs WHERE operation_id IN (%s, %s)",
                (probe_op_404, probe_op_400),
            )
            assert cur.fetchone()["count"] == 0
    finally:
        conn.close()
def test_ingest_snapshot_preflight_rejects_missing_source_manifest_cas(auth_setup: dict, pg_cluster: KnowledgeConfig) -> None:
    """F6: Ingest snapshot preflight must reject requests when source manifest is missing from CAS before engine admission."""
    from omp_knowledge.storage.db import get_db_connection

    client: TestClient = auth_setup["client"]
    token_ingest = auth_setup["token_ingest"]
    ws_allowed = auth_setup["ws_allowed"]
    headers = {"Authorization": f"Bearer {token_ingest}"}

    repo_id = uuid4()
    op_id = uuid4()
    snap_id = "7" * 64
    snap_ref = make_synthetic_enola_snapshot_ref(
        repository_id=repo_id, fixture_name="A", snapshot_id=snap_id
    )

    facts_a, receipt_a, insights_a, _ = load_staged_fixture("A")
    missing_sha = "9" * 64
    payload = {
        "kind": "code_snapshot",
        "operation_id": str(op_id),
        "workspace_id": str(ws_allowed),
        "repository_id": str(repo_id),
        "snapshot_id": snap_id,
        "snapshot_ref": snap_ref.model_dump(mode="json"),
        "facts_jsonl": facts_a.decode("utf-8"),
        "receipt_json": receipt_a.decode("utf-8"),
        "insights_json": insights_a.decode("utf-8"),
        "source_manifest": {
            "schema": "omp_knowledge.source_manifest/v1",
            "locator": f"blob:sha256:{missing_sha}",
            "files": [],
        },
    }

    res = client.post("/v1/ingest", headers=headers, json=payload)
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "CorruptArtifactError"
    assert f"Missing source manifest artifact ({missing_sha})" in err["message"]

    # Verify no job admitted in DB
    conn = get_db_connection(pg_cluster)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s", (op_id,))
            assert cur.fetchone()["count"] == 0
    finally:
        conn.close()
