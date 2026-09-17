from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.server import create_app
from omp_work.knowledge_contracts import KnowledgeUseOutcome
from omp_work.v1.canonical import sha256
from support.fixtures import write_test_capability_file
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
    token = "test_s5_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s5-actor",
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


def _seed_bundle(
    cfg: KnowledgeConfig,
    ws_id: UUID,
    repo_id: UUID,
    work_id: UUID,
    rev_id: UUID,
    candidate_id: UUID | None = None,
    proposal_ids: list[UUID | str] | None = None,
) -> UUID:
    bundle_id = uuid4()
    bundle_sha = sha256({"bundle": str(bundle_id)})
    now = datetime.now(timezone.utc)
    lin = [str(p) for p in (proposal_ids or [])]
    with psycopg.connect(cfg.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.context_bundles (
                    bundle_id, bundle_sha256, workspace_id, repository_id,
                    work_id, revision_id, candidate_id, stage,
                    proposal_lineage, budget_spec, budget_actual,
                    enrichment_status, content, compiled_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, 'test',
                    %s::jsonb, '{"method":"utf8_bytes","limit":1000}'::jsonb,
                    '{"method":"utf8_bytes","limit":1000,"used":100,"mandatory_used":100,"dropped_optional":[]}'::jsonb,
                    'applied', '{"test":true}'::jsonb, %s
                )
                """,
                (bundle_id, bundle_sha, ws_id, repo_id, work_id, rev_id, candidate_id, json.dumps(lin), now),
            )
    return bundle_id


def test_general_retrieval_returns_proposal_when_target_work_equals_source_work(
    test_setup: dict[str, Any]
) -> None:
    """General retrieval returns proposals even when target work_id equals the source work_id.
    Retrieved proposal includes same_source_task flag.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    source_work_id = uuid4()
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
            work_id=source_work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
            kind="audit",
            verdict="PASS",
        )

    # Create proposal with supporting evidence from source_work_id
    res_p = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Reusable fix",
            "steps": ["step 1"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(source_work_id),
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
    assert res_p.status_code == 200

    # Query with work_id == source_work_id
    res_list = client.get(
        f"/v1/proposals?workspace_id={test_setup['ws_id']}&repository_id={test_setup['repo_id']}&work_id={source_work_id}",
        headers=test_setup["headers"],
    )
    assert res_list.status_code == 200
    props = res_list.json()
    assert len(props) >= 1
    found = [p for p in props if p["title"] == "Reusable fix"]
    assert len(found) == 1
    assert found[0]["same_source_task"] is True


def test_independent_support_excludes_same_source_lineage(test_setup: dict[str, Any]) -> None:
    """Independent support calculation separates and excludes same-source lineage."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    source_work_id = uuid4()
    source_rev_id = uuid4()
    source_cand_id = uuid4()

    fresh_work_id = uuid4()
    fresh_rev_id = uuid4()
    fresh_cand_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec_source = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=source_work_id,
            revision_id=source_rev_id,
            candidate_id=source_cand_id,
            verdict="PASS",
        )
        rec_fresh = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=fresh_work_id,
            revision_id=fresh_rev_id,
            candidate_id=fresh_cand_id,
            verdict="PASS",
            independent=True,
        )

    # Create proposal with source_work_id
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Independence test proposal",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "work_id": str(source_work_id),
                    "revision_id": str(source_rev_id),
                    "candidate_id": str(source_cand_id),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec_source["payload_sha256"],
                    "native_validity_ref": str(rec_source["receipt_id"]),
                }
            ],
        },
    )
    prop_id = p_res.json()["proposal_id"]

    # 1. Record use on same-source task
    bundle_1 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        source_work_id,
        source_rev_id,
        candidate_id=source_cand_id,
        proposal_ids=[prop_id],
    )
    u1_res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(source_work_id),
            "task_revision_id": str(source_rev_id),
            "task_candidate_id": str(source_cand_id),
            "bundle_id": str(bundle_1),
        },
    )
    u1_id = u1_res.json()["use_id"]
    client.post(
        f"/v1/uses/{u1_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec_source["receipt_id"])},
    )

    # 2. Record use on fresh independent task
    bundle_2 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        fresh_work_id,
        fresh_rev_id,
        candidate_id=fresh_cand_id,
        proposal_ids=[prop_id],
    )
    u2_res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(fresh_work_id),
            "task_revision_id": str(fresh_rev_id),
            "task_candidate_id": str(fresh_cand_id),
            "bundle_id": str(bundle_2),
        },
    )
    u2_id = u2_res.json()["use_id"]
    client.post(
        f"/v1/uses/{u2_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec_fresh["receipt_id"])},
    )

    # Check support
    s_res = client.get(f"/v1/proposals/{prop_id}/support", headers=test_setup["headers"])
    assert s_res.status_code == 200
    support = s_res.json()
    assert support["uses"] == 2
    assert support["same_source_task_uses"] == 1
    assert support["independent_support"] == 1


def test_support_deduplicated_by_primary_evidence_lineage_not_task_id(test_setup: dict[str, Any]) -> None:
    """Multiple uses sharing the same primary evidence lineage key count as 1 distinct lineage."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    src_work = uuid4()
    src_rev = uuid4()
    src_cand = uuid4()

    task1_work = uuid4()
    task1_rev = uuid4()
    task1_cand = uuid4()

    task2_work = uuid4()
    task2_rev = uuid4()
    task2_cand = uuid4()

    shared_receipt_id = uuid4()
    shared_payload = {"test": "shared_audit"}

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        src_rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=src_work,
            revision_id=src_rev,
            candidate_id=src_cand,
            verdict="PASS",
        )
        rec1 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=task1_work,
            revision_id=task1_rev,
            candidate_id=task1_cand,
            payload=shared_payload,
            verdict="PASS",
            independent=True,
        )
        rec2 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=task2_work,
            revision_id=task2_rev,
            candidate_id=task2_cand,
            payload=shared_payload,
            verdict="PASS",
            independent=True,
        )

    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Deduplication proposal",
            "steps": ["step"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": src_rec["payload_sha256"],
                    "native_validity_ref": str(src_rec["receipt_id"]),
                }
            ],
        },
    )
    prop_id = p_res.json()["proposal_id"]

    bundle_1 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        task1_work,
        task1_rev,
        candidate_id=task1_cand,
        proposal_ids=[prop_id],
    )
    u1 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(task1_work),
            "task_revision_id": str(task1_rev),
            "task_candidate_id": str(task1_cand),
            "bundle_id": str(bundle_1),
        },
    ).json()["use_id"]
    client.post(
        f"/v1/uses/{u1}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec1["receipt_id"])},
    )

    bundle_2 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        task2_work,
        task2_rev,
        candidate_id=task2_cand,
        proposal_ids=[prop_id],
    )
    u2 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(task2_work),
            "task_revision_id": str(task2_rev),
            "task_candidate_id": str(task2_cand),
            "bundle_id": str(bundle_2),
        },
    ).json()["use_id"]
    client.post(
        f"/v1/uses/{u2}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec2["receipt_id"])},
    )

    s_res = client.get(f"/v1/proposals/{prop_id}/support", headers=test_setup["headers"])
    support = s_res.json()
    assert support["uses"] == 2
    # Since rec1 and rec2 have identical payload_sha256, distinct_lineages is 1
    assert support["distinct_lineages"] == 1
    assert support["independent_support"] == 1


def test_outcome_binds_to_exact_native_receipt_identity_and_payload_sha256(test_setup: dict[str, Any]) -> None:
    """Outcome record binds to exact native receipt identity and its payload_sha256."""
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

    # Seed proposal & bundle
    now = datetime.now(timezone.utc)
    p_id = uuid4()
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )

    u_res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    use_id = u_res.json()["use_id"]

    out_res = client.post(
        f"/v1/uses/{use_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec["receipt_id"])},
    )
    assert out_res.status_code == 200
    outcome = out_res.json()
    assert outcome["outcome"] == "success"
    assert outcome["outcome_receipt_id"] == str(rec["receipt_id"])
    assert outcome["outcome_receipt_sha256"] == rec["payload_sha256"]


def test_outcome_rejects_free_text_tool_exit_and_confidence(test_setup: dict[str, Any]) -> None:
    """Caller is strictly rejected from supplying free-text tool exit or confidence fields."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    use_id = uuid4()
    body_with_freetext = {
        "worker_used": True,
        "outcome_receipt_id": str(uuid4()),
        "tool_exit": "Exit code 0, all tests passed!",
        "confidence": 0.99,
        "outcome": "success",
    }
    res = client.post(f"/v1/uses/{use_id}/outcome", headers=test_setup["headers"], json=body_with_freetext)
    assert res.status_code == 422


def test_outcome_write_once_second_write_409(test_setup: dict[str, Any]) -> None:
    """Outcome is write-once; a second write returns 409 outcome_already_recorded."""
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

    # Seed proposal & bundle
    now = datetime.now(timezone.utc)
    p_id = uuid4()
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )

    u_res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    use_id = u_res.json()["use_id"]

    # First write succeeds
    first_res = client.post(
        f"/v1/uses/{use_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec["receipt_id"])},
    )
    assert first_res.status_code == 200

    # Second write returns 409
    second_res = client.post(
        f"/v1/uses/{use_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec["receipt_id"])},
    )
    assert second_res.status_code == 409
    assert second_res.json()["detail"]["error"]["code"] == "outcome_already_recorded"


def test_outcome_foreign_workspace_refusal_and_no_mutation(test_setup: dict[str, Any]) -> None:
    """POST /v1/uses/{use_id}/outcome refuses foreign workspace use with generic 404/403
    and guarantees no outcome mutation occurs.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    # 1. Setup second workspace and principal
    ws2_id = uuid4()
    actor2_id = uuid4()
    token_ws2 = "test_s5_token_ws2"
    cap_dir = test_setup["cfg"].config_dir / "capabilities"
    write_test_capability_file(
        cap_dir,
        token=token_ws2,
        actor_id=actor2_id,
        workspaces=[ws2_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s5-actor-ws2",
    )
    headers_ws2 = {"Authorization": f"Bearer {token_ws2}"}

    # 2. In workspace 2, insert native receipt, proposal, bundle, and knowledge use
    work_id_ws2 = uuid4()
    rev_id_ws2 = uuid4()
    cand_id_ws2 = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec_ws2 = insert_test_receipt(
            native_conn,
            workspace_id=ws2_id,
            work_id=work_id_ws2,
            revision_id=rev_id_ws2,
            candidate_id=cand_id_ws2,
            verdict="PASS",
        )

    now = datetime.now(timezone.utc)
    p_id_ws2 = uuid4()
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id_ws2, ws2_id, test_setup["repo_id"], "{}", "0" * 64, now),
            )

    b_id_ws2 = _seed_bundle(
        test_setup["cfg"],
        ws2_id,
        test_setup["repo_id"],
        work_id_ws2,
        rev_id_ws2,
        candidate_id=cand_id_ws2,
        proposal_ids=[p_id_ws2],
    )

    u_res = client.post(
        "/v1/uses",
        headers=headers_ws2,
        json={
            "proposal_id": str(p_id_ws2),
            "workspace_id": str(ws2_id),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id_ws2),
            "task_revision_id": str(rev_id_ws2),
            "task_candidate_id": str(cand_id_ws2),
            "bundle_id": str(b_id_ws2),
        },
    )
    assert u_res.status_code == 200
    use_id_ws2 = u_res.json()["use_id"]

    # 3. Caller principal belonging to ws_id (foreign to ws2_id) attempts to record outcome on ws2 use
    foreign_res = client.post(
        f"/v1/uses/{use_id_ws2}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec_ws2["receipt_id"])},
    )
    assert foreign_res.status_code in (403, 404)
    assert foreign_res.status_code == 404
    assert foreign_res.json()["detail"]["error"]["code"] == "use_record_not_found"

    # 4. Verify no outcome mutation occurred in the database
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, outcome_recorded_at, worker_used FROM omp_knowledge.knowledge_uses WHERE use_id = %s",
                (use_id_ws2,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[1] is None
            assert row[0] == "not_evaluated"
            assert row[2] is False

    # 5. Legitimate workspace 2 caller can record outcome successfully
    auth_res = client.post(
        f"/v1/uses/{use_id_ws2}/outcome",
        headers=headers_ws2,
        json={"worker_used": True, "outcome_receipt_id": str(rec_ws2["receipt_id"])},
    )
    assert auth_res.status_code == 200
    assert auth_res.json()["outcome"] == "success"


def test_proposal_support_foreign_workspace_refusal_and_no_lineage_disclosure(test_setup: dict[str, Any]) -> None:
    """GET /v1/proposals/{proposal_id}/support refuses foreign proposals with generic 404
    and discloses no lineage or support metrics.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    # 1. Setup second workspace and principal
    ws2_id = uuid4()
    actor2_id = uuid4()
    token_ws2 = "test_s5_token_ws2_support"
    cap_dir = test_setup["cfg"].config_dir / "capabilities"
    write_test_capability_file(
        cap_dir,
        token=token_ws2,
        actor_id=actor2_id,
        workspaces=[ws2_id],
        scopes=["knowledge.read", "knowledge.ingest"],
        name="test-ws2-support-actor",
    )
    headers_ws2 = {"Authorization": f"Bearer {token_ws2}"}

    # 2. Create proposal in workspace 2 with supporting lineage
    now = datetime.now(timezone.utc)
    p_id_ws2 = uuid4()
    secret_receipt_id = str(uuid4())
    prop_json = {
        "proposal_id": str(p_id_ws2),
        "workspace_id": str(ws2_id),
        "repository_id": str(test_setup["repo_id"]),
        "proposal_summary": "private ws2 proposal",
        "supporting_evidence": [
            {
                "work_id": str(uuid4()),
                "candidate_id": str(uuid4()),
                "native_validity_ref": secret_receipt_id,
            }
        ],
    }
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', %s, %s)
                """,
                (
                    p_id_ws2,
                    ws2_id,
                    test_setup["repo_id"],
                    json.dumps(prop_json),
                    "0" * 64,
                    json.dumps([{"work_id": str(uuid4()), "receipt_id": secret_receipt_id}]),
                    now,
                ),
            )

    # 3. Caller principal in ws_id (foreign to ws2) attempts to read support for ws2 proposal
    res_foreign = client.get(
        f"/v1/proposals/{p_id_ws2}/support",
        headers=test_setup["headers"],
    )
    assert res_foreign.status_code == 404
    assert res_foreign.json()["detail"]["error"]["code"] == "proposal_not_found"
    # Ensure no lineage or details are disclosed
    res_text = res_foreign.text
    assert secret_receipt_id not in res_text

    # 4. Non-existent proposal returns identical generic 404
    res_nonexistent = client.get(
        f"/v1/proposals/{uuid4()}/support",
        headers=test_setup["headers"],
    )
    assert res_nonexistent.status_code == 404
    assert res_nonexistent.json()["detail"]["error"]["code"] == "proposal_not_found"

    # 5. Legitimate workspace 2 principal can access proposal support
    res_auth = client.get(
        f"/v1/proposals/{p_id_ws2}/support",
        headers=headers_ws2,
    )
    assert res_auth.status_code == 200
    assert res_auth.json()["proposal_id"] == str(p_id_ws2)
    assert res_auth.json()["uses"] == 0


def test_record_use_replay_with_null_candidate_resolves_without_error(test_setup: dict[str, Any]) -> None:
    """POST /v1/uses with task_candidate_id=None succeeds on replay/conflict without 500 error,
    updating supplied_at and returning the exact use record with 1 row in the table.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed proposal
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=None,
        proposal_ids=[p_id],
    )

    use_payload = {
        "proposal_id": str(p_id),
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "task_work_id": str(work_id),
        "task_revision_id": str(rev_id),
        "task_candidate_id": None,
        "bundle_id": str(b_id),
    }

    # First record_use call
    res1 = client.post("/v1/uses", headers=test_setup["headers"], json=use_payload)
    assert res1.status_code == 200
    data1 = res1.json()
    u1_id = data1["use_id"]
    assert data1["task_candidate_id"] is None

    # Second record_use call (replay with task_candidate_id=None)
    res2 = client.post("/v1/uses", headers=test_setup["headers"], json=use_payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["use_id"] == u1_id

    # Verify exactly 1 row exists in knowledge_uses
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.knowledge_uses WHERE use_id = %s", (u1_id,))
            assert cur.fetchone()[0] == 1


def test_record_use_validates_proposal_and_bundle_lineage(test_setup: dict[str, Any]) -> None:
    """POST /v1/uses validates that the proposal and bundle exist in the same workspace
    and match the task's work_id and revision_id.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed proposal in workspace 1
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=None,
        proposal_ids=[p_id],
    )

    # 1. Non-existent proposal -> 404
    res_no_prop = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(uuid4()),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "bundle_id": str(b_id),
        },
    )
    assert res_no_prop.status_code == 404

    # 2. Non-existent bundle -> 404
    res_no_bundle = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "bundle_id": str(uuid4()),
        },
    )
    assert res_no_bundle.status_code == 404

    # 3. Mismatched work/revision in task vs bundle -> 422
    res_mismatch = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(uuid4()),
            "task_revision_id": str(rev_id),
            "bundle_id": str(b_id),
        },
    )
    assert res_mismatch.status_code == 422


def test_record_outcome_rejects_candidate_null_and_mismatch(test_setup: dict[str, Any]) -> None:
    """Failure mode: record_outcome must enforce exact receipt task identity.
    Native omp_evidence.receipts.candidate_id is NOT NULL, so task_candidate_id=None
    must reject candidate-specific receipt, and mismatched candidate must also be rejected.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_1 = uuid4()
    cand_2 = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # Insert candidate-specific receipt in native DB
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec_cand1 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_1,
            verdict="PASS",
        )

    # Seed proposal
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    # 1. Use record with task_candidate_id=None (bundle candidate_id=None)
    b_null = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=None,
        proposal_ids=[p_id],
    )
    res_use_null = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": None,
            "bundle_id": str(b_null),
        },
    )
    assert res_use_null.status_code == 200
    use_id_null = res_use_null.json()["use_id"]

    # Attempt outcome with candidate-specific receipt -> rejected with 422 receipt_task_mismatch
    res_out_null = client.post(
        f"/v1/uses/{use_id_null}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec_cand1["receipt_id"])},
    )
    assert res_out_null.status_code == 422
    assert res_out_null.json()["detail"]["error"]["code"] == "receipt_task_mismatch"

    # 2. Use record with task_candidate_id=cand_2 (mismatch with rec_cand1 which has cand_1)
    b_cand2 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_2,
        proposal_ids=[p_id],
    )
    res_use_cand2 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_2),
            "bundle_id": str(b_cand2),
        },
    )
    assert res_use_cand2.status_code == 200
    use_id_cand2 = res_use_cand2.json()["use_id"]

    # Attempt outcome with cand_1 receipt for cand_2 task -> rejected with 422 receipt_task_mismatch
    res_out_mismatch = client.post(
        f"/v1/uses/{use_id_cand2}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec_cand1["receipt_id"])},
    )
    assert res_out_mismatch.status_code == 422
    assert res_out_mismatch.json()["detail"]["error"]["code"] == "receipt_task_mismatch"


def test_record_use_rejects_candidate_mismatch_and_null_discrepancy(test_setup: dict[str, Any]) -> None:
    """Failure mode: record_use must compare bundle.candidate_id exactly to task_candidate_id.
    NULL discrepancy (bundle has candidate, task does not, or vice-versa) and candidate mismatch
    must return 422 bundle_lineage_mismatch.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_1 = uuid4()
    cand_2 = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    # Bundle with candidate_id = cand_1
    b_cand1 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_1,
        proposal_ids=[p_id],
    )

    # 1. Task candidate is None, but bundle has cand_1 -> 422
    res1 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": None,
            "bundle_id": str(b_cand1),
        },
    )
    assert res1.status_code == 422
    assert res1.json()["detail"]["error"]["code"] == "bundle_lineage_mismatch"

    # 2. Task candidate is cand_2, but bundle has cand_1 -> 422
    res2 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_2),
            "bundle_id": str(b_cand1),
        },
    )
    assert res2.status_code == 422
    assert res2.json()["detail"]["error"]["code"] == "bundle_lineage_mismatch"

    # 3. Bundle with candidate_id = None, but task has cand_1 -> 422
    b_null = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=None,
        proposal_ids=[p_id],
    )
    res3 = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_1),
            "bundle_id": str(b_null),
        },
    )
    assert res3.status_code == 422
    assert res3.json()["detail"]["error"]["code"] == "bundle_lineage_mismatch"


def test_record_use_rejects_proposal_absent_from_bundle_lineage(test_setup: dict[str, Any]) -> None:
    """Failure mode: record_use must authorize proposal relationship at write.
    proposal_id must be a member of persisted context_bundles.proposal_lineage for this bundle.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    p_included = uuid4()
    p_absent = uuid4()
    now = datetime.now(timezone.utc)

    # Seed both proposals in the same workspace & repository
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            for p_id in (p_included, p_absent):
                cur.execute(
                    """
                    INSERT INTO omp_knowledge.procedure_proposals (
                        proposal_id, workspace_id, repository_id, proposal_json,
                        proposal_sha256, state, attribution, evidence_binding,
                        enrichment_status, supporting_lineage, created_at
                    ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                    """,
                    (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
                )

    # Bundle includes only p_included in proposal_lineage
    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_included],
    )

    # Attempt to record use of p_absent with this bundle -> 422 proposal_not_in_bundle_lineage
    res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_absent),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "proposal_not_in_bundle_lineage"


def test_record_use_rejects_invalidated_proposal(test_setup: dict[str, Any]) -> None:
    """Failure mode: record_use must reject invalidated proposals.
    Even if proposal is listed in bundle lineage, write must refuse invalidated proposals.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed proposal with invalidated_at set
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, invalidated_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now, now),
            )

    # Bundle includes the proposal in lineage
    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )

    # Attempt to record use of invalidated proposal -> 422 proposal_invalidated
    res = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"]["code"] == "proposal_invalidated"


def test_record_use_rejects_repository_mismatch(test_setup: dict[str, Any]) -> None:
    """Failure mode: record_use must enforce repository tenancy.
    Proposal and bundle must belong to the request repository.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    repo2_id = uuid4()
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.repositories (repository_id, state)
            VALUES (%s, 'unbound')
            ON CONFLICT (repository_id) DO NOTHING
            """,
            (repo2_id,),
        )

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed proposal in repo 1
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    # Bundle seeded in repo 1
    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )

    # 1. Request asks for repo2_id -> proposal repository mismatch -> 403
    res_prop_mismatch = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(repo2_id),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    assert res_prop_mismatch.status_code == 403
    assert res_prop_mismatch.json()["detail"]["error"]["code"] == "forbidden"
    assert "proposal repository mismatch" in res_prop_mismatch.json()["detail"]["error"]["message"]

    # 2. Bundle seeded in repo 2 with p_id, request with repo_id -> bundle repository mismatch -> 403
    b_id_repo2 = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        repo2_id,
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )
    res_bundle_mismatch = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id_repo2),
        },
    )
    assert res_bundle_mismatch.status_code == 403
    assert res_bundle_mismatch.json()["detail"]["error"]["code"] == "forbidden"
    assert "bundle repository mismatch" in res_bundle_mismatch.json()["detail"]["error"]["message"]


def test_record_use_and_outcome_valid_relationship_lifecycle(test_setup: dict[str, Any]) -> None:
    """Failure mode: Ensure valid relationship lifecycle operates end-to-end:
    authorized proposal membership in bundle lineage, matching workspace/repository,
    active proposal, exact candidate identity, and successful authoritative native receipt readback.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    p_id = uuid4()
    now = datetime.now(timezone.utc)

    # 1. Native receipt for work_id, rev_id, cand_id
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

    # 2. Seed active proposal in knowledge DB
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (p_id, test_setup["ws_id"], test_setup["repo_id"], "{}", "0" * 64, now),
            )

    # 3. Seed bundle with exact work_id, rev_id, cand_id and proposal_lineage containing p_id
    b_id = _seed_bundle(
        test_setup["cfg"],
        test_setup["ws_id"],
        test_setup["repo_id"],
        work_id,
        rev_id,
        candidate_id=cand_id,
        proposal_ids=[p_id],
    )

    # 4. Record use with exact matching identity and relationship
    res_use = client.post(
        "/v1/uses",
        headers=test_setup["headers"],
        json={
            "proposal_id": str(p_id),
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "task_work_id": str(work_id),
            "task_revision_id": str(rev_id),
            "task_candidate_id": str(cand_id),
            "bundle_id": str(b_id),
        },
    )
    assert res_use.status_code == 200
    use_data = res_use.json()
    assert use_data["proposal_id"] == str(p_id)
    assert use_data["bundle_id"] == str(b_id)
    assert use_data["task_work_id"] == str(work_id)
    assert use_data["task_revision_id"] == str(rev_id)
    assert use_data["task_candidate_id"] == str(cand_id)
    assert use_data["outcome"] == "not_evaluated"
    use_id = use_data["use_id"]

    # 5. Record outcome with matching native receipt
    res_out = client.post(
        f"/v1/uses/{use_id}/outcome",
        headers=test_setup["headers"],
        json={"worker_used": True, "outcome_receipt_id": str(rec["receipt_id"])},
    )
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["use_id"] == use_id
    assert out_data["outcome"] == "success"
    assert out_data["outcome_receipt_id"] == str(rec["receipt_id"])
    assert out_data["outcome_receipt_sha256"] == rec["payload_sha256"]
    assert out_data["worker_used"] is True
