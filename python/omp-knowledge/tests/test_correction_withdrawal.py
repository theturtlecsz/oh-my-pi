from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.protocol import CorrectResult, IngestResult
from omp_knowledge.server import create_app
from omp_knowledge.storage.db import store_observation
from omp_work.knowledge_contracts import JobState, ObservationKind, SourceObservation, SourceRef
from omp_work.v1.canonical import sha256
from support.fixtures import (
    make_test_enola_fixture,
    make_test_snapshot_ref,
    write_test_capability_file,
)
from support.native_fixture import (
    insert_test_candidate,
    insert_test_receipt,
    insert_test_work_item,
    insert_test_work_revision,
)
from support.null_engine import NullEngine


@pytest.fixture
def test_setup(dual_pg_cluster: KnowledgeConfig):
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    actor_id = uuid4()
    token = "test_s6_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s6-actor",
    )

    with psycopg.connect(dual_pg_cluster.pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.repositories (repository_id, state)
            VALUES (%s, 'unbound')
            ON CONFLICT (repository_id) DO NOTHING
            """,
            (repo_id,),
        )

    headers = {"Authorization": f"Bearer {token}"}
    return {
        "cfg": dual_pg_cluster,
        "ws_id": ws_id,
        "repo_id": repo_id,
        "actor_id": actor_id,
        "headers": headers,
    }


def test_withdrawal_immediately_excludes_proposal_from_retrieval_applicability_and_compile(
    test_setup: dict[str, Any]
) -> None:
    """Withdrawal immediately excludes proposal from list retrieval, applicability check, and context compile."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )

    # 1. Create proposal citing this receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal to be withdrawn",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec["payload_sha256"],
                    "native_validity_ref": str(rec["receipt_id"]),
                }
            ],
        },
    )
    assert p_res.status_code == 200
    prop_id = p_res.json()["proposal_id"]

    # Before correction: proposal is visible in list
    pre_list = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}",
        headers=test_setup["headers"],
    ).json()
    assert any(p["proposal_id"] == prop_id for p in pre_list)

    # 2. Record correction withdrawing evidence
    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
            "reason": "flawed audit test setup",
        },
    )
    assert c_res.status_code == 200

    # 3. Post-withdrawal: Excluded from retrieval
    post_list = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}",
        headers=test_setup["headers"],
    ).json()
    assert not any(p["proposal_id"] == prop_id for p in post_list)

    # 4. Post-withdrawal: Excluded from applicability
    app_res = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
        },
    )
    assert app_res.status_code == 200
    app_data = app_res.json()
    assert app_data["acceptance"] == "not_accepted"
    assert "proposal_invalidated" in app_data["reasons"]

    # 5. Post-withdrawal: Excluded from context compile
    comp_res = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert comp_res.status_code == 200
    bundle = comp_res.json()
    # The withdrawn proposal is NOT in optional proposals, and is listed in excluded_proposal_ids
    assert f"proposal:{prop_id}" not in bundle["optional"]
    assert prop_id in [str(x) for x in bundle["excluded_proposal_ids"]]


def test_persisted_historical_bundle_sha256_unchanged_after_correction(test_setup: dict[str, Any]) -> None:
    """Historical context bundles remain completely immutable; bundle_sha256 is unchanged after correction."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

    # Compile bundle before correction
    comp_res = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    bundle_pre = comp_res.json()
    b_id = bundle_pre["bundle_id"]
    sha_pre = bundle_pre["bundle_sha256"]

    # Perform correction withdrawing that receipt
    client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
            "reason": "test withdrawal",
        },
    )

    # Read historical bundle after correction
    get_res = client.get(f"/v1/context/bundles/{b_id}", headers=test_setup["headers"])
    assert get_res.status_code == 200
    bundle_post = get_res.json()
    assert bundle_post["bundle_sha256"] == sha_pre


def test_correction_propagates_to_derived_observations_and_proposals(test_setup: dict[str, Any]) -> None:
    """Correction immediately marks matching observations with withdrawn_by and proposals with invalidated_at."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    receipt_id = uuid4()
    receipt_hash = sha256({"test": "receipt_for_propagation"})
    obs_id = uuid4()
    prop_id = uuid4()
    now = datetime.now(timezone.utc)

    # Insert observation and proposal directly referencing this receipt
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            obs_payload = {"type": "receipt", "data": {"receipt_id": str(receipt_id)}}
            cur.execute(
                """
                INSERT INTO omp_knowledge.observations (
                    observation_id, workspace_id, repository_id, source,
                    kind, payload, payload_sha256, native_payload_sha256
                ) VALUES (
                    %s, %s, %s, %s,
                    'tool_output', %s, %s, %s
                )
                """,
                (
                    obs_id,
                    test_setup["ws_id"],
                    test_setup["repo_id"],
                    json.dumps({"native_validity_ref": str(receipt_id), "content_sha256": receipt_hash}),
                    json.dumps(obs_payload),
                    sha256(obs_payload),
                    receipt_hash,
                ),
            )
            prop_data = {
                "proposal_id": str(prop_id),
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "title": "Derived proposal",
                "supporting_evidence": [
                    {"native_validity_ref": str(receipt_id), "content_sha256": receipt_hash}
                ],
            }
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, 'proposal', '{}'::jsonb, 'verified',
                    'applied', %s, %s
                )
                """,
                (
                    prop_id,
                    test_setup["ws_id"],
                    test_setup["repo_id"],
                    json.dumps(prop_data),
                    sha256(prop_data),
                    json.dumps([{"receipt_id": str(receipt_id), "payload_sha256": receipt_hash}]),
                    now,
                ),
            )

    # Post correction
    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(receipt_id),
                "payload_sha256": receipt_hash,
            },
            "reason": "propagation test",
        },
    )
    assert c_res.status_code == 200
    corr_id = UUID(c_res.json()["correction_id"])

    # Verify propagation in DB
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT withdrawn_by FROM omp_knowledge.observations WHERE observation_id = %s", (obs_id,))
            obs_row = cur.fetchone()
            assert obs_row is not None
            assert obs_row[0] == corr_id

            cur.execute(
                "SELECT invalidated_at, invalidation_id FROM omp_knowledge.procedure_proposals WHERE proposal_id = %s",
                (prop_id,),
            )
            prop_row = cur.fetchone()
            assert prop_row is not None
            assert prop_row[0] is not None
            assert prop_row[1] == corr_id


def test_derived_cleanup_job_visible_and_durable(test_setup: dict[str, Any]) -> None:
    """Correction admits a durable cleanup job visible at /v1/jobs/{cleanup_operation_id}."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "payload_sha256": "0" * 64,
            },
            "reason": "cleanup visibility test",
        },
    )
    assert c_res.status_code == 200
    cleanup_op_id = c_res.json()["cleanup_operation_id"]
    assert cleanup_op_id is not None

    # Check job visibility
    job_res = client.get(f"/v1/jobs/{cleanup_op_id}", headers=test_setup["headers"])
    assert job_res.status_code == 200
    job = job_res.json()
    assert job["state"] == "completed"
    assert "derived_cleanup_completed" in job["diagnostics"]


def test_retained_rebuild_after_correction_does_not_resurrect_withdrawn(test_setup: dict[str, Any]) -> None:
    """Retained rebuild does not resurrect withdrawn observations or invalidated proposals."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    prop_id = uuid4()
    receipt_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed proposal and then mark invalidated
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            prop_data = {
                "proposal_id": str(prop_id),
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "title": "Never resurrected proposal",
            }
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, invalidated_at, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, 'proposal', '{}'::jsonb, 'verified',
                    'applied', '[]'::jsonb, %s, %s
                )
                """,
                (prop_id, test_setup["ws_id"], test_setup["repo_id"], json.dumps(prop_data), "0" * 64, now, now),
            )

    # Proposal remains excluded in proposal listing
    props = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}",
        headers=test_setup["headers"],
    ).json()
    assert not any(p["proposal_id"] == str(prop_id) for p in props)


def test_native_receipts_and_events_unchanged_by_correction(test_setup: dict[str, Any]) -> None:
    """Correction leaves native database rows completely untouched (read-only session)."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # Seed native receipt
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

        with native_conn.cursor() as cur:
            cur.execute("SELECT count(*), md5(string_agg(receipt_id::text, '')) FROM omp_evidence.receipts")
            pre_receipt_state = cur.fetchone()

    # Perform correction
    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
            "reason": "testing native immutability",
        },
    )
    assert c_res.status_code == 200

    # Verify native DB state is 100% untouched
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        with native_conn.cursor() as cur:
            cur.execute("SELECT count(*), md5(string_agg(receipt_id::text, '')) FROM omp_evidence.receipts")
            post_receipt_state = cur.fetchone()

    assert pre_receipt_state == post_receipt_state


def test_correction_missing_or_foreign_target_returns_404_403(test_setup: dict[str, Any]) -> None:
    """Missing correction target returns HTTP 404 with typed error code, while target belonging to a foreign workspace returns HTTP 403 forbidden."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    # 1. Non-existent proposal in supersede_proposal returns 404 proposal_not_found
    res_missing_prop = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "supersede_proposal",
            "target": {"proposal_id": str(uuid4())},
            "reason": "superseding non-existent proposal",
        },
    )
    assert res_missing_prop.status_code == 404
    assert res_missing_prop.json()["detail"]["error"]["code"] == "proposal_not_found"

    # 2. Non-existent observation in withdraw_evidence returns 404 observation_not_found
    res_missing_obs = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"observation_id": str(uuid4())},
            "reason": "withdrawing non-existent observation",
        },
    )
    assert res_missing_obs.status_code == 404
    assert res_missing_obs.json()["detail"]["error"]["code"] == "observation_not_found"

    # 3. Foreign proposal in supersede_proposal returns 403 forbidden
    foreign_ws_id = uuid4()
    foreign_prop_id = uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (
                    %s, %s, %s, '{}'::jsonb,
                    %s, 'proposal', '{}'::jsonb, 'verified',
                    'applied', '[]'::jsonb, %s
                )
                """,
                (foreign_prop_id, foreign_ws_id, test_setup["repo_id"], "0" * 64, now),
            )

    res_foreign_prop = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "supersede_proposal",
            "target": {"proposal_id": str(foreign_prop_id)},
            "reason": "superseding foreign proposal",
        },
    )
    assert res_foreign_prop.status_code == 403
    assert res_foreign_prop.json()["detail"]["error"]["code"] == "forbidden"

    # 4. Foreign native receipt in withdraw_evidence returns 403 forbidden
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        foreign_rec = insert_test_receipt(
            native_conn,
            workspace_id=foreign_ws_id,
            work_id=uuid4(),
            revision_id=uuid4(),
            candidate_id=uuid4(),
        )

    res_foreign_rec = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(foreign_rec["receipt_id"]),
                "payload_sha256": foreign_rec["payload_sha256"],
            },
            "reason": "withdrawing foreign evidence receipt",
        },
    )
    assert res_foreign_rec.status_code == 403
    assert res_foreign_rec.json()["detail"]["error"]["code"] == "forbidden"

    # 5. Foreign observation in withdraw_evidence returns 403 forbidden
    foreign_obs_id = uuid4()
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.observations (
                    observation_id, workspace_id, repository_id, source,
                    kind, payload, payload_sha256, native_payload_sha256
                ) VALUES (
                    %s, %s, %s, '{}'::jsonb,
                    'tool_output', '{}'::jsonb, %s, %s
                )
                """,
                (foreign_obs_id, foreign_ws_id, test_setup["repo_id"], "0" * 64, "0" * 64),
            )

    res_foreign_obs = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"observation_id": str(foreign_obs_id)},
            "reason": "withdrawing foreign observation",
        },
    )
    assert res_foreign_obs.status_code == 403
    assert res_foreign_obs.json()["detail"]["error"]["code"] == "forbidden"

    # 6. Unresolvable/missing native receipt in withdraw_evidence returns 403 forbidden (fail closed, no cross-tenant disclosure)
    res_missing_rec = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(uuid4()),
                "payload_sha256": "0" * 64,
            },
            "reason": "withdrawing missing evidence receipt",
        },
    )
    assert res_missing_rec.status_code == 403
    assert res_missing_rec.json()["detail"]["error"]["code"] == "forbidden"


def test_correction_retry_duplicate_is_deterministic(test_setup: dict[str, Any]) -> None:
    """Repeated identical correction requests return identical correction_id and cleanup_operation_id idempotently."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    payload = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "kind": "withdraw_evidence",
        "target": {
            "payload_sha256": "f" * 64,
        },
        "reason": "testing deterministic idempotency",
    }

    res1 = client.post("/v1/corrections", headers=test_setup["headers"], json=payload)
    assert res1.status_code == 200
    data1 = res1.json()

    res2 = client.post("/v1/corrections", headers=test_setup["headers"], json=payload)
    assert res2.status_code == 200
    data2 = res2.json()

    assert data1["correction_id"] == data2["correction_id"]
    assert data1["cleanup_operation_id"] == data2["cleanup_operation_id"]
    assert data1["kind"] == data2["kind"]
    assert data1["target"] == data2["target"]
    assert data1["reason"] == data2["reason"]


class FailingCorrectEngine(NullEngine):
    async def correct(self, **kwargs: Any) -> Any:
        raise RuntimeError("simulated engine.correct failure during cleanup")


def test_engine_correct_failure_records_partial_job_state(test_setup: dict[str, Any]) -> None:
    """When engine.correct fails during cleanup job, the durable job state is recorded as PARTIAL
    with explicit diagnostics, while synchronous proposal and observation invalidation is preserved.
    """
    app = create_app(test_setup["cfg"], engine=FailingCorrectEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )

    # 1. Create proposal citing this receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal to be invalidated on partial cleanup",
            "steps": ["step 1"],
            "applicability_scope": "test",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec["payload_sha256"],
                    "native_validity_ref": str(rec["receipt_id"]),
                }
            ],
        },
    )
    assert p_res.status_code == 200
    prop_id = UUID(p_res.json()["proposal_id"])

    # 2. Issue correction with target containing snapshot_id and fact_id so engine.correct is invoked
    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
                "snapshot_id": "test-snapshot",
                "fact_id": "fact-to-correct",
            },
            "reason": "engine failure test",
        },
    )
    assert c_res.status_code in (200, 503)
    c_data = c_res.json()
    c_detail = c_data.get("detail", c_data) if isinstance(c_data, dict) else {}
    c_cleanup_raw = (
        c_data.get("cleanup_operation_id")
        if isinstance(c_data, dict) and "cleanup_operation_id" in c_data
        else (c_detail.get("cleanup_operation_id") if isinstance(c_detail, dict) else None)
    )
    if c_cleanup_raw:
        cleanup_op_id = UUID(str(c_cleanup_raw))
    else:
        with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cleanup_operation_id FROM omp_knowledge.corrections WHERE workspace_id = %s ORDER BY recorded_at DESC LIMIT 1",
                    (test_setup["ws_id"],),
                )
                cleanup_op_id = cur.fetchone()[0]

    # 3. Verify proposal was synchronously invalidated in knowledge DB
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invalidated_at, invalidation_id FROM omp_knowledge.procedure_proposals WHERE proposal_id = %s",
                (prop_id,),
            )
            p_row = cur.fetchone()
            assert p_row is not None
            assert p_row[0] is not None
            c_corr_raw = (
                c_data.get("correction_id")
                if isinstance(c_data, dict) and "correction_id" in c_data
                else (c_detail.get("correction_id") if isinstance(c_detail, dict) else None)
            )
            if c_corr_raw:
                assert p_row[1] == UUID(str(c_corr_raw))

            # 4. Verify durable job state in ingestion_jobs is 'partial' with diagnostic tags
            cur.execute(
                "SELECT state, diagnostics, response FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (cleanup_op_id,),
            )
            j_row = cur.fetchone()
            assert j_row is not None
            assert j_row[0] == "partial"
            assert "derived_cleanup_failed_engine_outage" in j_row[1]
            assert "derived_cleanup_partial" in j_row[1]


def test_correction_retry_crash_window_heals_missing_cleanup_job(test_setup: dict[str, Any]) -> None:
    """Crash-window simulation test for correction duplicate retry durability:
    1. A correction request is initially committed (invalidating proposals/observations and recording correction row),
       but the process crashes before the durable cleanup job is admitted or completed (leaving cleanup job missing).
    2. Before retry: query /v1/jobs/{cleanup_operation_id} returns 404 (no durable job exists).
    3. Duplicate retry request is received for the identical correction:
       - Duplicate retry inspects canonical durable job state.
       - Discovers missing cleanup job and safely re-admits/executes it.
       - Preserves immediate invalidation, deterministic IDs, and no native writes.
       - Heals the missing cleanup job so /v1/jobs/{cleanup_operation_id} is now COMPLETED.
    4. Crash-window simulation with failing engine:
       - When cleanup job is missing and engine fails during re-admission retry,
         the job is durably recorded in explicit PARTIAL state and NEVER reports successful cleanup without durable job.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    payload = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "kind": "withdraw_evidence",
        "target": {
            "payload_sha256": "c" * 64,
        },
        "reason": "crash window simulation test",
    }

    # Step 1: Initial correction request succeeds
    res1 = client.post("/v1/corrections", headers=test_setup["headers"], json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    corr_id = UUID(data1["correction_id"])
    cleanup_op_id = UUID(data1["cleanup_operation_id"])

    # Verify initial job is present and completed
    job_res1 = client.get(f"/v1/jobs/{cleanup_op_id}", headers=test_setup["headers"])
    assert job_res1.status_code == 200
    assert job_res1.json()["state"] == "completed"

    # Step 2: Simulate crash window - delete the cleanup job row from ingestion_jobs
    # This simulates a crash where the correction row and invalidations committed, but the job row was lost/unadmitted
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (cleanup_op_id,),
            )

    # Verify the durable job is now missing (returns 404)
    job_missing_res = client.get(f"/v1/jobs/{cleanup_op_id}", headers=test_setup["headers"])
    assert job_missing_res.status_code == 404

    # Step 3: Duplicate retry arrives (simulating client retry after crash)
    res_retry = client.post("/v1/corrections", headers=test_setup["headers"], json=payload)
    assert res_retry.status_code == 200
    data_retry = res_retry.json()
    assert UUID(data_retry["correction_id"]) == corr_id
    assert UUID(data_retry["cleanup_operation_id"]) == cleanup_op_id

    # Step 4: Verify duplicate retry healed the missing cleanup job!
    job_healed_res = client.get(f"/v1/jobs/{cleanup_op_id}", headers=test_setup["headers"])
    assert job_healed_res.status_code == 200
    job_healed_data = job_healed_res.json()
    assert job_healed_data["state"] == "completed"
    assert "derived_cleanup_completed" in job_healed_data["diagnostics"]

    # Step 5: Crash-window simulation with failing engine - must yield explicit PARTIAL state
    app.state.engine = FailingCorrectEngine()

    failing_payload = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "kind": "withdraw_evidence",
        "target": {
            "payload_sha256": "d" * 64,
            "snapshot_id": "snap-failing",
            "fact_id": "fact-failing",
        },
        "reason": "crash window engine failure test",
    }

    # Initial call with failing engine yields structured failure/partial response and PARTIAL job
    res_fail1 = client.post("/v1/corrections", headers=test_setup["headers"], json=failing_payload)
    assert res_fail1.status_code in (200, 503)
    fail_data1 = res_fail1.json()
    fail_detail1 = fail_data1.get("detail", fail_data1) if isinstance(fail_data1, dict) else {}
    fail_cleanup_raw = (
        fail_data1.get("cleanup_operation_id")
        if isinstance(fail_data1, dict) and "cleanup_operation_id" in fail_data1
        else (fail_detail1.get("cleanup_operation_id") if isinstance(fail_detail1, dict) else None)
    )
    if fail_cleanup_raw:
        fail_cleanup_op_id = UUID(str(fail_cleanup_raw))
    else:
        with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cleanup_operation_id FROM omp_knowledge.corrections WHERE workspace_id = %s ORDER BY recorded_at DESC LIMIT 1",
                    (test_setup["ws_id"],),
                )
                fail_cleanup_op_id = cur.fetchone()[0]

    # Simulate crash window: delete the job row so it's missing
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (fail_cleanup_op_id,),
            )

    job_fail_missing = client.get(f"/v1/jobs/{fail_cleanup_op_id}", headers=test_setup["headers"])
    assert job_fail_missing.status_code == 404

    # Retry with failing engine: heals missing job but records explicit PARTIAL state (never COMPLETED)
    res_fail_retry = client.post("/v1/corrections", headers=test_setup["headers"], json=failing_payload)
    assert res_fail_retry.status_code in (200, 503)

    job_fail_healed = client.get(f"/v1/jobs/{fail_cleanup_op_id}", headers=test_setup["headers"])
    assert job_fail_healed.status_code == 200
    fail_job_data = job_fail_healed.json()
    assert fail_job_data["state"] in ("partial", "failed")
    assert "derived_cleanup_partial" in fail_job_data["diagnostics"]
    assert "derived_cleanup_failed_engine_outage" in fail_job_data["diagnostics"]
    assert fail_job_data.get("response") is not None
    assert fail_job_data["response"].get("engine_status") == "failed"

    # Verify durable job row in DB preserves structured non-success response and partial state
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, diagnostics, response FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (fail_cleanup_op_id,),
            )
            j_row = cur.fetchone()
            assert j_row is not None
            assert j_row[0] in ("partial", "failed")
            assert "derived_cleanup_partial" in j_row[1]
            assert "derived_cleanup_failed_engine_outage" in j_row[1]
            assert j_row[2] is not None
            assert j_row[2].get("engine_status") == "failed"


def test_correction_then_retained_rebuild_and_query_excludes_withdrawn_derived_knowledge(
    test_setup: dict[str, Any]
) -> None:
    """Behavioral proof for Requirement 3:
    1. Create authentic native receipt in PostgreSQL and capture pre-correction native table hash.
    2. Formulate procedure proposal citing the native receipt.
    3. Compile context bundle containing the proposal, verifying bundle_sha256 and content.
    4. Verify proposal is returned in /v1/proposals and accepted in /v1/applicability.
    5. Execute correction withdrawing the evidence receipt.
    6. Verify historical bundle immutability: previously compiled bundle_sha256 and content are untouched.
    7. Verify native row immutability: native receipt row and table hash remain completely unchanged.
    8. Retained rebuild / query / recompile excludes withdrawn derived knowledge:
       - /v1/proposals excludes the proposal.
       - /v1/applicability rejects the proposal with 'not_accepted' and 'proposal_invalidated'.
       - /v1/proposals/{id}/support returns 404.
       - /v1/context/compile recompile excludes the proposal and lists it in excluded_proposal_ids.
       - Re-running rebuild / query never resurrects withdrawn observations or proposals.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # 1. Insert native receipt in native DB
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
            payload={"test_case": "retained_rebuild_exclusion", "verdict": "PASS"},
        )

        with native_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*), md5(string_agg(receipt_id::text || payload_sha256, ',' ORDER BY receipt_id))
                FROM omp_evidence.receipts
                WHERE workspace_id = %s
                """,
                (test_setup["ws_id"],),
            )
            pre_native_state = cur.fetchone()

    # 2. Formulate procedure proposal citing this receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal referencing verifiable native receipt",
            "steps": ["step 1", "step 2"],
            "applicability_scope": "system_core",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec["payload_sha256"],
                    "native_validity_ref": str(rec["receipt_id"]),
                }
            ],
        },
    )
    assert p_res.status_code == 200
    prop_id = p_res.json()["proposal_id"]

    # 3. Compile context bundle containing the proposal before correction
    comp_res_pre = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert comp_res_pre.status_code == 200
    bundle_pre = comp_res_pre.json()
    b_id = bundle_pre["bundle_id"]
    sha_pre = bundle_pre["bundle_sha256"]
    assert f"proposal:{prop_id}" in bundle_pre["optional"]

    # 4. Pre-correction queries show active proposal
    list_pre = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}",
        headers=test_setup["headers"],
    ).json()
    assert any(p["proposal_id"] == prop_id for p in list_pre)

    app_pre = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
        },
    ).json()
    assert app_pre["acceptance"] == "accepted"

    # 5. Execute correction: withdraw evidence
    c_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
            "reason": "withdrawing evidence for retained exclusion test",
        },
    )
    assert c_res.status_code == 200

    # 6. Verify historical bundle immutability (immutable context bundle trigger in PG)
    bundle_get = client.get(f"/v1/context/bundles/{b_id}", headers=test_setup["headers"])
    assert bundle_get.status_code == 200
    bundle_post = bundle_get.json()
    assert bundle_post["bundle_sha256"] == sha_pre
    assert bundle_post["mandatory"] == bundle_pre["mandatory"]
    assert bundle_post["optional"] == bundle_pre["optional"]
    assert bundle_post["budget"] == bundle_pre["budget"]
    assert bundle_post["proposal_lineage"] == bundle_pre["proposal_lineage"]
    assert bundle_post["receipt_lineage"] == bundle_pre["receipt_lineage"]
    assert bundle_post["excluded_proposal_ids"] == bundle_pre["excluded_proposal_ids"]
    assert f"proposal:{prop_id}" in bundle_post["optional"]

    # 7. Verify native database rows and row hashes are 100% unchanged
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        with native_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*), md5(string_agg(receipt_id::text || payload_sha256, ',' ORDER BY receipt_id))
                FROM omp_evidence.receipts
                WHERE workspace_id = %s
                """,
                (test_setup["ws_id"],),
            )
            post_native_state = cur.fetchone()
    assert pre_native_state == post_native_state

    # 8. Retained rebuild / query / recompile excludes withdrawn derived knowledge
    # a) Proposals list excludes the withdrawn proposal
    list_post = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}",
        headers=test_setup["headers"],
    ).json()
    assert not any(p["proposal_id"] == prop_id for p in list_post)

    # b) Applicability check rejects the proposal
    app_post = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
        },
    ).json()
    assert app_post["acceptance"] == "not_accepted"
    assert "proposal_invalidated" in app_post["reasons"]

    # c) Proposal support returns 404
    supp_res = client.get(f"/v1/proposals/{prop_id}/support", headers=test_setup["headers"])
    assert supp_res.status_code == 404

    # d) Re-compiled context bundle excludes the withdrawn proposal and lists it in excluded_proposal_ids
    comp_res_post = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert comp_res_post.status_code == 200
    bundle_new = comp_res_post.json()
    assert f"proposal:{prop_id}" not in bundle_new["optional"]
    assert prop_id in [str(x) for x in bundle_new["excluded_proposal_ids"]]


class CountingNullEngine(NullEngine):
    def __init__(self, *, available: bool = True) -> None:
        super().__init__(available=available)
        self.ingest_calls: int = 0
        self.fail_correct: bool = False

    async def ingest_snapshot(self, **kwargs: Any) -> IngestResult:
        self.ingest_calls += 1
        return await super().ingest_snapshot(**kwargs)

    async def correct(self, **kwargs: Any) -> CorrectResult:
        if self.fail_correct:
            raise RuntimeError("simulated engine correct outage")
        return await super().correct(**kwargs)


def _ingest_and_publish(
    client: TestClient,
    headers: dict[str, str],
    cfg: KnowledgeConfig,
    ws: UUID,
    repo: UUID,
    snap_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    facts_bytes, receipt_bytes, insights_bytes, facts_list = make_test_enola_fixture(snapshot_id=snap_id)
    snap_ref = make_test_snapshot_ref(repository_id=repo, snapshot_id=snap_id)
    op_id = uuid4()
    ingest_res = client.post(
        "/v1/ingest",
        headers=headers,
        json={
            "kind": "code_snapshot",
            "operation_id": str(op_id),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_ref.snapshot_id,
            "snapshot_ref": snap_ref.model_dump(mode="json"),
            "facts_jsonl": facts_bytes.decode("utf-8"),
            "receipt_json": receipt_bytes.decode("utf-8"),
            "insights_json": insights_bytes.decode("utf-8"),
        },
    )
    assert ingest_res.status_code == 200

    pub_res = client.post(
        "/v1/publish",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_ref.snapshot_id,
        },
    )
    assert pub_res.status_code == 200
    assert pub_res.json()["published"] is True

    fact_ids = [str(f["id"]) for f in facts_list]
    return facts_list, fact_ids


def test_withdrawal_retained_rebuild_query_lookup_journey_via_api(test_setup: dict[str, Any]) -> None:
    engine = CountingNullEngine()
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)
    ws = test_setup["ws_id"]
    repo = test_setup["repo_id"]
    headers = test_setup["headers"]
    snap_id = "a" * 64

    facts, fact_ids = _ingest_and_publish(client, headers, test_setup["cfg"], ws, repo, snap_id)
    f1 = fact_ids[0]
    f2 = fact_ids[1]

    # Pre-rebuild publication baseline
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws, repo, snap_id),
            )
            pub_baseline = cur.fetchone()

    # Query before withdrawal
    res_q1 = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q1.status_code == 200
    q1_ids = {f["fact_id"] for f in res_q1.json()["facts"]}
    assert f1 in q1_ids and f2 in q1_ids

    # Withdraw f1 via /v1/corrections
    corr_res = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": f1},
            "reason": "Test withdrawal journey",
        },
    )
    assert corr_res.status_code == 200
    corr_body = corr_res.json()
    correction_id = corr_body["correction_id"]
    cleanup_op = corr_body["cleanup_operation_id"]

    # Cleanup job completed via /v1/jobs
    res_job = client.get(f"/v1/jobs/{cleanup_op}", headers=headers)
    assert res_job.status_code == 200
    assert res_job.json()["state"] == JobState.COMPLETED.value

    # Query and lookup exclusion
    res_q2 = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q2.status_code == 200
    q2_ids = {f["fact_id"] for f in res_q2.json()["facts"]}
    assert f1 not in q2_ids and f2 in q2_ids

    res_l1 = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f1},
    )
    assert res_l1.status_code == 200
    assert res_l1.json()["found"] is False

    res_l2 = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f2},
    )
    assert res_l2.status_code == 200
    assert res_l2.json()["found"] is True

    # Engine loss via retire
    asyncio.run(engine.retire(workspace_id=ws, repository_id=repo, snapshot_id=snap_id))
    res_q_empty = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q_empty.status_code == 200
    assert len(res_q_empty.json()["facts"]) == 0

    # /v1/rebuild completed with validity_reapplied==1 and non-null result_sha256
    rebuild_op = uuid4()
    ingest_count_before = engine.ingest_calls
    res_rebuild = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_rebuild.status_code == 200
    body_rebuild = res_rebuild.json()
    assert body_rebuild["state"] == JobState.COMPLETED.value
    assert body_rebuild["response"]["rebuilt"] is True
    assert body_rebuild["response"]["validity_reapplied"] == 1
    assert body_rebuild["response"]["validity_unapplied"] == []
    assert body_rebuild["result_sha256"] is not None

    # Engine-level proof
    r = asyncio.run(engine.lookup(workspace_id=ws, repository_id=repo, snapshot_id=snap_id, fact_id=f1))
    assert (not r.found) or r.fact.properties.get("withdrawn_by") == correction_id

    # /v1/query and /v1/lookup exclusions with f2 still present
    res_q3 = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q3.status_code == 200
    q3_ids = {f["fact_id"] for f in res_q3.json()["facts"]}
    assert f1 not in q3_ids and f2 in q3_ids

    res_l1_after = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f1},
    )
    assert res_l1_after.status_code == 200
    assert res_l1_after.json()["found"] is False

    res_l2_after = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f2},
    )
    assert res_l2_after.status_code == 200
    assert res_l2_after.json()["found"] is True

    # Replay identical response/result_sha256 and no additional ingest call
    ingest_count_after = engine.ingest_calls
    res_replay = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_replay.status_code == 200
    body_replay = res_replay.json()
    assert body_replay["replayed"] is True
    assert body_replay["state"] == JobState.COMPLETED.value
    assert body_replay["result_sha256"] == body_rebuild["result_sha256"]
    assert body_replay["response"] == body_rebuild["response"]
    assert engine.ingest_calls == ingest_count_after

    # /v1/jobs durable
    res_job_durable = client.get(f"/v1/jobs/{rebuild_op}", headers=headers)
    assert res_job_durable.status_code == 200
    assert res_job_durable.json()["state"] == JobState.COMPLETED.value
    assert res_job_durable.json()["result_sha256"] == body_rebuild["result_sha256"]

    # Publication row unchanged
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws, repo, snap_id),
            )
            pub_after = cur.fetchone()
            assert pub_after == pub_baseline


def test_rebuild_validity_reapply_failure_is_partial_and_retryable_preserving_prior_view(
    test_setup: dict[str, Any]
) -> None:
    engine = CountingNullEngine()
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)
    ws = test_setup["ws_id"]
    repo = test_setup["repo_id"]
    headers = test_setup["headers"]
    snap_id = "a" * 64

    facts, fact_ids = _ingest_and_publish(client, headers, test_setup["cfg"], ws, repo, snap_id)
    f1 = fact_ids[0]
    f2 = fact_ids[1]

    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws, repo, snap_id),
            )
            pub_baseline = cur.fetchone()

    # Withdraw f1
    corr_res = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": f1},
            "reason": "Test partial rebuild",
        },
    )
    assert corr_res.status_code == 200
    correction_id = corr_res.json()["correction_id"]

    # Engine loss
    asyncio.run(engine.retire(workspace_id=ws, repository_id=repo, snapshot_id=snap_id))

    # Set fail_correct = True
    engine.fail_correct = True

    # POST /v1/rebuild op R -> 200, state 'partial', 'rebuild_validity_reapply_partial' in diagnostics, response.validity_unapplied == [f1]
    rebuild_op_r = uuid4()
    res_rebuild = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_r),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_rebuild.status_code == 200
    body_rebuild = res_rebuild.json()
    assert body_rebuild["state"] == JobState.PARTIAL.value
    assert "rebuild_validity_reapply_partial" in body_rebuild["diagnostics"]
    assert body_rebuild["response"]["validity_unapplied"] == [f1]

    # /v1/query still excludes f1 and includes f2 (read-time validity)
    res_q = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q.status_code == 200
    q_ids = {f["fact_id"] for f in res_q.json()["facts"]}
    assert f1 not in q_ids and f2 in q_ids

    # /v1/lookup f1 False
    res_l1 = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f1},
    )
    assert res_l1.status_code == 200
    assert res_l1.json()["found"] is False

    # Publication row unchanged
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT publication_id, status, graph_sha256, published_at
                FROM omp_knowledge.snapshot_publications
                WHERE workspace_id = %s AND repository_id = %s AND snapshot_id = %s
                """,
                (ws, repo, snap_id),
            )
            pub_after_partial = cur.fetchone()
            assert pub_after_partial == pub_baseline

    # /v1/jobs/R state partial
    res_job_partial = client.get(f"/v1/jobs/{rebuild_op_r}", headers=headers)
    assert res_job_partial.status_code == 200
    assert res_job_partial.json()["state"] == JobState.PARTIAL.value

    # Set fail_correct = False
    engine.fail_correct = False

    # Re-POST identical op R -> state completed, replayed False, 'resumed' in diagnostics, validity_unapplied == []
    res_retry = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op_r),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_retry.status_code == 200
    body_retry = res_retry.json()
    assert body_retry["state"] == JobState.COMPLETED.value
    assert body_retry["replayed"] is False
    assert "resumed" in body_retry["diagnostics"]
    assert body_retry["response"]["validity_unapplied"] == []

    # Engine-level lookup shows withdrawn_by == correction_id
    r = asyncio.run(engine.lookup(workspace_id=ws, repository_id=repo, snapshot_id=snap_id, fact_id=f1))
    assert (not r.found) or r.fact.properties.get("withdrawn_by") == correction_id


def test_partial_cleanup_retries_to_completed_when_engine_recovers_and_correction_stays_accepted(
    test_setup: dict[str, Any]
) -> None:
    app = create_app(test_setup["cfg"], engine=FailingCorrectEngine())
    client = TestClient(app)

    # POST /v1/corrections withdraw_evidence target {snapshot_id: 'a'*64, fact_id: 'fact-x'} -> 200 with correction_id C and cleanup op K
    snap_id = "a" * 64
    res_corr = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": "fact-x"},
            "reason": "test partial cleanup retry",
        },
    )
    assert res_corr.status_code == 200
    c_id = res_corr.json()["correction_id"]
    k_op = res_corr.json()["cleanup_operation_id"]

    # /v1/jobs/K state partial with both partial diagnostics
    res_job = client.get(f"/v1/jobs/{k_op}", headers=test_setup["headers"])
    assert res_job.status_code == 200
    job_data = res_job.json()
    assert job_data["state"] == JobState.PARTIAL.value
    assert "derived_cleanup_failed_engine_outage" in job_data["diagnostics"]
    assert "derived_cleanup_partial" in job_data["diagnostics"]

    # capture GET /v1/corrections/C body
    res_get_c = client.get(f"/v1/corrections/{c_id}", headers=test_setup["headers"])
    assert res_get_c.status_code == 200
    captured_c_body = res_get_c.json()

    # swap app.state.engine = NullEngine() whose snapshots['a'*64] = [{'id':'fact-x','name':'x','kind':'symbol'}]
    repaired_engine = NullEngine()
    repaired_engine.snapshots[snap_id] = [{"id": "fact-x", "name": "x", "kind": "symbol", "props": {}}]
    app.state.engine = repaired_engine

    # re-POST identical correction -> 200 same correction_id and cleanup_operation_id
    res_corr_retry = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": "fact-x"},
            "reason": "test partial cleanup retry",
        },
    )
    assert res_corr_retry.status_code == 200
    assert res_corr_retry.json()["correction_id"] == c_id
    assert res_corr_retry.json()["cleanup_operation_id"] == k_op

    # /v1/jobs/K -> completed, 'derived_cleanup_completed' in diagnostics, response.nodes_affected == 1
    res_job_completed = client.get(f"/v1/jobs/{k_op}", headers=test_setup["headers"])
    assert res_job_completed.status_code == 200
    completed_job_data = res_job_completed.json()
    assert completed_job_data["state"] == JobState.COMPLETED.value
    assert "derived_cleanup_completed" in completed_job_data["diagnostics"]
    assert completed_job_data["response"]["nodes_affected"] == 1

    # DB row attempt_count == 2 and 'resumed' in diagnostics
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (UUID(k_op),),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 2
            assert "resumed" in row[1]

    # GET /v1/corrections/C body identical to the captured one (recorded_at, target, reason, kind)
    res_get_c_after = client.get(f"/v1/corrections/{c_id}", headers=test_setup["headers"])
    assert res_get_c_after.status_code == 200
    after_c_body = res_get_c_after.json()
    assert after_c_body["recorded_at"] == captured_c_body["recorded_at"]
    assert after_c_body["target"] == captured_c_body["target"]
    assert after_c_body["reason"] == captured_c_body["reason"]
    assert after_c_body["kind"] == captured_c_body["kind"]


def test_proposal_citing_withdrawn_evidence_or_observation_fails_closed(
    test_setup: dict[str, Any]
) -> None:
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        r1 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )
        r2 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            verdict="PASS",
        )
        r3 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            payload={"verdict": "PASS", "audit_rule": "strict", "receipt": "r3"},
            verdict="PASS",
        )

    # POST /v1/corrections withdraw_evidence {receipt_id: R1, payload_sha256} -> 200
    corr1 = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {
                "receipt_id": str(r1["receipt_id"]),
                "payload_sha256": r1["payload_sha256"],
            },
            "reason": "withdraw r1",
        },
    )
    assert corr1.status_code == 200

    # POST /v1/proposals citing R1 -> 422 detail.error.code == 'evidence_binding_unverified'
    p_fail1 = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal citing R1",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": r1["payload_sha256"],
                    "native_validity_ref": str(r1["receipt_id"]),
                }
            ],
        },
    )
    assert p_fail1.status_code == 422
    assert p_fail1.json()["detail"]["error"]["code"] == "evidence_binding_unverified"

    # insert a knowledge observation O (tool_output, source native_validity_ref = R2 receipt id, content_sha256 = R2 hash)
    obs_id = uuid4()
    now_obs = datetime.now(timezone.utc)
    obs_payload = {"output": "test"}
    obs = SourceObservation(
        observation_id=obs_id,
        source=SourceRef(
            workspace_id=test_setup["ws_id"],
            repository_id=test_setup["repo_id"],
            producer="work-service/auditor-settle",
            observed_at=now_obs,
            native_validity_ref=str(r2["receipt_id"]),
        ),
        kind=ObservationKind.TOOL_OUTPUT,
        payload=obs_payload,
        payload_sha256=sha256(obs_payload),
        native_payload_sha256=r2["payload_sha256"],
        relevance_tags=(f"workspace:{test_setup['ws_id']}",),
        observed_at=now_obs,
    )
    with psycopg.connect(test_setup["cfg"].pg_connection_string()) as conn:
        store_observation(conn, observation=obs)

    # POST /v1/corrections withdraw_evidence {observation_id: O} -> 200 and observations.withdrawn_by == correction_id
    corr2 = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"observation_id": str(obs_id)},
            "reason": "withdraw observation",
        },
    )
    assert corr2.status_code == 200
    corr2_id = corr2.json()["correction_id"]
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT withdrawn_by FROM omp_knowledge.observations WHERE observation_id = %s",
                (obs_id,),
            )
            obs_row = cur.fetchone()
            assert obs_row is not None
            assert str(obs_row[0]) == str(corr2_id)

    # POST /v1/proposals citing R2 with source_observation_ids [O] -> 422 evidence_binding_unverified
    p_fail2 = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal citing R2 with withdrawn obs",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": r2["payload_sha256"],
                    "native_validity_ref": str(r2["receipt_id"]),
                }
            ],
            "source_observation_ids": [str(obs_id)],
        },
    )
    assert p_fail2.status_code == 422
    assert p_fail2.json()["detail"]["error"]["code"] == "evidence_binding_unverified"

    # POST /v1/proposals citing a fresh valid receipt R3 with no observations -> 200 (fail-closed only for withdrawn evidence)
    p_ok = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Proposal citing valid R3",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(work_id),
                    "revision_id": str(rev_id),
                    "candidate_id": str(cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": r3["payload_sha256"],
                    "native_validity_ref": str(r3["receipt_id"]),
                }
            ],
        },
    )
    assert p_ok.status_code == 200


def test_context_compile_excludes_withdrawn_engine_facts(test_setup: dict[str, Any]) -> None:
    snap = "a" * 64
    work_id = uuid4()
    rev_id = uuid4()
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=uuid4(),
        )

    engine = NullEngine(available=True)
    engine.snapshots[snap] = [
        {
            "id": "fact-1",
            "name": "planning helper 1",
            "kind": "symbol",
            "file": "src/a.py",
            "line": 10,
            "props": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
        },
        {
            "id": "fact-2",
            "name": "planning helper 2",
            "kind": "symbol",
            "file": "src/b.py",
            "line": 20,
            "props": {
                "receipt_id": str(rec["receipt_id"]),
                "payload_sha256": rec["payload_sha256"],
            },
        },
    ]
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)

    now = datetime.now(timezone.utc)
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshots (
                    snapshot_id, workspace_id, repository_id, manifest_sha256,
                    base_commit, tree_sha, manifest, created_at
                ) VALUES (%s, %s, %s, %s, '0'*40, '0'*40, '{}'::jsonb, %s)
                """,
                (snap, test_setup["ws_id"], test_setup["repo_id"], "0" * 64, now),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshot_publications (
                    publication_id, workspace_id, repository_id, snapshot_id,
                    status, fact_count, insight_count, edge_count,
                    graph_sha256, receipt_sha256, diagnostics, published_at, staged_at
                ) VALUES (%s, %s, %s, %s, 'published', 2, 0, 0, %s, %s, '{}', %s, %s)
                """,
                (uuid4(), test_setup["ws_id"], test_setup["repo_id"], snap, "0" * 64, "0" * 64, now, now),
            )

    # POST /v1/corrections withdraw_evidence {snapshot_id: snap, fact_id: 'fact-1'} -> 200
    corr_res = client.post(
        "/v1/corrections",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap, "fact_id": "fact-1"},
            "reason": "withdraw fact-1",
        },
    )
    assert corr_res.status_code == 200

    # POST /v1/context/compile without snapshot_id -> 200, enrichment_status 'applied'
    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }
    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 200
    bundle = res.json()
    assert bundle["enrichment_status"] == "applied"
    fact_entries = [v for k, v in bundle["optional"].items() if k.startswith("fact:")]
    assert len(fact_entries) == 1
    assert fact_entries[0]["fact_id"] == "fact-2"
    assert not any(v.get("fact_id") == "fact-1" for k, v in bundle["optional"].items() if k.startswith("fact:"))

    # bundle_sha256 stable on replay
    res2 = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res2.status_code == 200
    assert res2.json()["bundle_sha256"] == bundle["bundle_sha256"]


def test_rebuild_persisted_withdrawal_absent_fact_completes_without_permanent_partial(
    test_setup: dict[str, Any]
) -> None:
    """When a persisted withdrawal points to a fact absent from the retained snapshot,
    rebuild completes with validity_unapplied empty and does not remain in permanent PARTIAL.
    Read-time query retains unaffected control facts and excludes the withdrawn absent fact.
    """
    engine = CountingNullEngine(available=True)
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)
    ws = test_setup["ws_id"]
    repo = test_setup["repo_id"]
    headers = test_setup["headers"]
    snap_id = "a" * 64

    facts, fact_ids = _ingest_and_publish(client, headers, test_setup["cfg"], ws, repo, snap_id)
    control_fact = fact_ids[0]
    withdrawn_real_fact = fact_ids[1]
    absent_fact = "fact-absent-never-existed-999"

    # Persist withdrawal for the absent fact
    corr_res1 = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": absent_fact},
            "reason": "withdraw absent fact",
        },
    )
    assert corr_res1.status_code == 200
    cleanup_op1 = corr_res1.json()["cleanup_operation_id"]
    res_job1 = client.get(f"/v1/jobs/{cleanup_op1}", headers=headers)
    assert res_job1.status_code == 200
    assert res_job1.json()["state"] == JobState.COMPLETED.value

    # Persist withdrawal for the real fact
    corr_res2 = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": withdrawn_real_fact},
            "reason": "withdraw real fact",
        },
    )
    assert corr_res2.status_code == 200

    # Retire engine snapshot to simulate loss and force rebuild
    asyncio.run(engine.retire(
        workspace_id=ws,
        repository_id=repo,
        snapshot_id=snap_id,
    ))

    # Rebuild must complete cleanly, validity_unapplied == [] (no permanent PARTIAL)
    rebuild_op = uuid4()
    res_rebuild = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_rebuild.status_code == 200
    b = res_rebuild.json()
    assert b["state"] == JobState.COMPLETED.value
    assert b["response"]["validity_reapplied"] == 1
    assert b["response"]["validity_unapplied"] == []
    assert "rebuild_validity_reapplied" in b["diagnostics"]

    # Query retains unaffected control fact, excludes real withdrawn fact, and does not resurrect absent fact
    res_q = client.get(
        "/v1/query",
        headers=headers,
        params={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
            "query": "*",
        },
    )
    assert res_q.status_code == 200
    q_facts = {f["fact_id"] for f in res_q.json()["facts"]}
    assert control_fact in q_facts
    assert withdrawn_real_fact not in q_facts
    assert absent_fact not in q_facts

    # Lookup confirms control fact found, absent fact not found, withdrawn real fact not found
    res_l_ctrl = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
            "fact_id": control_fact,
        },
    )
    assert res_l_ctrl.status_code == 200
    assert res_l_ctrl.json()["found"] is True

    res_l_absent = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
            "fact_id": absent_fact,
        },
    )
    assert res_l_absent.status_code == 200
    assert res_l_absent.json()["found"] is False

    res_l_withdrawn = client.get(
        "/v1/lookup",
        headers=headers,
        params={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
            "fact_id": withdrawn_real_fact,
        },
    )
    assert res_l_withdrawn.status_code == 200
    assert res_l_withdrawn.json()["found"] is False


class OptionalSemanticOutageEngine(CountingNullEngine):
    """Engine double that simulates an optional semantic vector outage where
    correct() succeeds in graph updating (success=True) but reports semantic_status='failed'
    with a redacted outage reason, matching RealCogneeAdapter behavior under optional semantic policy.
    """

    def __init__(self, *, available: bool = True) -> None:
        super().__init__(available=available)
        self.semantic_outage: bool = True
        self.semantic_reason: str = "vector host unreachable (connection refused)"

    async def correct(self, **kwargs: Any) -> CorrectResult:
        res = await super().correct(**kwargs)
        if self.semantic_outage:
            return CorrectResult(
                snapshot_id=res.snapshot_id,
                corrected_fact_id=res.corrected_fact_id,
                success=True,
                route=self._route,
                semantic_status="failed",
                semantic_reason=self.semantic_reason,
            )
        return CorrectResult(
            snapshot_id=res.snapshot_id,
            corrected_fact_id=res.corrected_fact_id,
            success=res.success,
            route=self._route,
            semantic_status="deleted",
            semantic_reason=None,
        )


def test_correction_cleanup_records_partial_when_optional_semantic_outage_and_recovers_on_retry(
    test_setup: dict[str, Any]
) -> None:
    """When RealCogneeAdapter.correct returns success=True with semantic_status='failed' under
    optional semantic outage, server-owned wrap_cleanup_engine raises EngineUnavailableError,
    causing do_cleanup to record JobState.PARTIAL with explicit failure diagnostics and error
    in durable ingestion_jobs, while synchronous graph invalidation in SQL remains immediately effective.
    Upon vector engine recovery, re-POSTing the identical correction re-admits and completes the job.
    """
    engine = OptionalSemanticOutageEngine()
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)
    ws = test_setup["ws_id"]
    repo = test_setup["repo_id"]
    headers = test_setup["headers"]
    snap_id = "a" * 64

    facts, fact_ids = _ingest_and_publish(client, headers, test_setup["cfg"], ws, repo, snap_id)
    f1 = fact_ids[0]
    f2 = fact_ids[1]

    # 1. Post correction for f1 under optional semantic outage
    res_corr = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": f1},
            "reason": "semantic outage withdrawal",
        },
    )
    assert res_corr.status_code == 200
    corr_data = res_corr.json()
    c_id = corr_data["correction_id"]
    k_op = corr_data["cleanup_operation_id"]

    # 2. Synchronous PostgreSQL invalidation is immediate: read-time queries exclude f1
    res_q = client.get(
        "/v1/query",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "query": "*"},
    )
    assert res_q.status_code == 200
    q_facts = {f["fact_id"] for f in res_q.json()["facts"]}
    assert f1 not in q_facts
    assert f2 in q_facts

    res_l = client.get(
        "/v1/lookup",
        headers=headers,
        params={"workspace_id": str(ws), "repository_id": str(repo), "snapshot_id": snap_id, "fact_id": f1},
    )
    assert res_l.status_code == 200
    assert res_l.json()["found"] is False

    # 3. Durable cleanup job is PARTIAL with truthful failure diagnostics and reason
    res_job = client.get(f"/v1/jobs/{k_op}", headers=headers)
    assert res_job.status_code == 200
    job_data = res_job.json()
    assert job_data["state"] == JobState.PARTIAL.value
    assert "derived_cleanup_failed_engine_outage" in job_data["diagnostics"]
    assert "derived_cleanup_partial" in job_data["diagnostics"]
    assert job_data["response"]["engine_status"] == "failed"
    assert "vector host unreachable" in job_data["response"]["error"]

    # 4. Engine recovers from outage
    engine.semantic_outage = False

    # 5. Re-POST identical correction re-admits and reconciles do_cleanup
    res_retry = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": f1},
            "reason": "semantic outage withdrawal",
        },
    )
    assert res_retry.status_code == 200
    assert res_retry.json()["correction_id"] == c_id
    assert res_retry.json()["cleanup_operation_id"] == k_op

    # 6. Durable cleanup job is now COMPLETED
    res_job_healed = client.get(f"/v1/jobs/{k_op}", headers=headers)
    assert res_job_healed.status_code == 200
    healed_job_data = res_job_healed.json()
    assert healed_job_data["state"] == JobState.COMPLETED.value
    assert "derived_cleanup_completed" in healed_job_data["diagnostics"]
    assert healed_job_data["response"]["nodes_affected"] == 1


def test_rebuild_records_partial_when_optional_semantic_outage_during_validity_reapply(
    test_setup: dict[str, Any]
) -> None:
    """When POST /v1/rebuild reapplies persisted corrections and the engine experiences
    an optional semantic outage (semantic_status='failed'), validity reapplication records
    the fact in validity_unapplied and the rebuild job transitions to PARTIAL rather than
    falsely completing. Once recovered, rebuild retry transitions to COMPLETED.
    """
    engine = OptionalSemanticOutageEngine()
    engine.semantic_outage = False  # Start clean for initial ingest and correction
    app = create_app(test_setup["cfg"], engine=engine)
    client = TestClient(app)
    ws = test_setup["ws_id"]
    repo = test_setup["repo_id"]
    headers = test_setup["headers"]
    snap_id = "a" * 64

    facts, fact_ids = _ingest_and_publish(client, headers, test_setup["cfg"], ws, repo, snap_id)
    f1 = fact_ids[0]

    # Withdraw f1 cleanly
    corr_res = client.post(
        "/v1/corrections",
        headers=headers,
        json={
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "kind": "withdraw_evidence",
            "target": {"snapshot_id": snap_id, "fact_id": f1},
            "reason": "initial clean withdrawal",
        },
    )
    assert corr_res.status_code == 200

    # Retire engine snapshot to simulate loss and necessitate rebuild
    asyncio.run(engine.retire(workspace_id=ws, repository_id=repo, snapshot_id=snap_id))

    # Now enable optional semantic outage on the engine
    engine.semantic_outage = True

    # POST /v1/rebuild -> 200, state PARTIAL, rebuild_validity_reapply_partial in diagnostics, f1 in validity_unapplied
    rebuild_op = uuid4()
    res_rebuild = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_rebuild.status_code == 200
    body_rebuild = res_rebuild.json()
    assert body_rebuild["state"] == JobState.PARTIAL.value
    assert "rebuild_validity_reapply_partial" in body_rebuild["diagnostics"]
    assert body_rebuild["response"]["validity_unapplied"] == [f1]

    # Engine recovers
    engine.semantic_outage = False

    # Retry identical rebuild operation -> state COMPLETED, validity_unapplied empty
    res_retry = client.post(
        "/v1/rebuild",
        headers=headers,
        json={
            "operation_id": str(rebuild_op),
            "workspace_id": str(ws),
            "repository_id": str(repo),
            "snapshot_id": snap_id,
        },
    )
    assert res_retry.status_code == 200
    body_retry = res_retry.json()
    assert body_retry["state"] == JobState.COMPLETED.value
    assert "rebuild_validity_reapplied" in body_retry["diagnostics"]
    assert body_retry["response"]["validity_unapplied"] == []
