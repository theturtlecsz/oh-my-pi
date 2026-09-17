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
from omp_knowledge.engine.protocol import (
    CorrectResult,
    EngineStatus,
    IngestPlan,
    IngestResult,
    KnowledgeEngine,
    LookupResult,
    QueryResult,
    RetireResult,
)
from omp_knowledge.server import create_app
from omp_work.knowledge_contracts import SourceRef
from support.fixtures import write_test_capability_file
from support.native_fixture import (
    insert_test_candidate,
    insert_test_receipt,
    insert_test_work_item,
    insert_test_work_revision,
)
from support.null_engine import NullEngine


class FailingQueryEngine(NullEngine):
    """Engine whose query method fails, to verify optional enrichment degradation."""

    async def query(self, **kwargs: Any) -> QueryResult:
        raise RuntimeError("simulated engine query failure")

    async def status(self) -> EngineStatus:
        return await super().status()


@pytest.fixture
def test_setup(dual_pg_cluster: KnowledgeConfig):
    cap_dir = dual_pg_cluster.config_dir / "capabilities"
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_dir.chmod(0o700)

    ws_id = uuid4()
    repo_id = uuid4()
    actor_id = uuid4()
    token = "test_s3_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s3-actor",
    )

    # Ensure repository row exists in knowledge DB
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


def test_proposal_requires_verified_evidence_binding_422_when_unresolvable(test_setup: dict[str, Any]) -> None:
    """Proposal creation refuses with 422 evidence_binding_unverified when supporting evidence
    cannot be verified against native receipts.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    fake_receipt_id = str(uuid4())
    fake_sha = "a" * 64

    body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Use verified sorting algorithm",
        "preconditions": ["list non-empty"],
        "steps": ["quicksort list"],
        "expected_observations": ["sorted output"],
        "limits": ["not for linked lists"],
        "applicability_scope": "algorithms",
        "supporting_evidence": [
            {
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "producer": "test-runner",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": fake_sha,
                "native_validity_ref": fake_receipt_id,
            }
        ],
    }

    res = client.post("/v1/proposals", headers=test_setup["headers"], json=body)
    assert res.status_code == 422
    err = res.json()["detail"]["error"]
    assert err["code"] == "evidence_binding_unverified"


def test_proposal_creation_refuses_503_when_native_readback_unavailable(
    test_setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proposal creation refuses with 503 native_unavailable when native read session fails."""
    # Point native PG port to an unreachable closed port
    bad_cfg = test_setup["cfg"].model_copy(update={"native_pg_port": 1})
    app = create_app(bad_cfg, engine=NullEngine())
    client = TestClient(app)

    body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Sorting procedure",
        "steps": ["sort elements"],
        "applicability_scope": "algorithms",
        "supporting_evidence": [
            {
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "producer": "test-runner",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": "a" * 64,
                "native_validity_ref": str(uuid4()),
            }
        ],
    }

    res = client.post("/v1/proposals", headers=test_setup["headers"], json=body)
    assert res.status_code == 503
    err = res.json()["detail"]["error"]
    assert err["code"] == "native_unavailable"


def test_optional_enrichment_failure_degrades_with_explicit_status(test_setup: dict[str, Any]) -> None:
    """Optional enrichment failure degrades gracefully with explicit status 'degraded',
    and does not block proposal creation.
    """
    app = create_app(test_setup["cfg"], engine=FailingQueryEngine())
    client = TestClient(app)
    # Seed published snapshot
    snap_id = "test-snap-" + str(uuid4())[:8]
    now = datetime.now(timezone.utc)
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshots (
                    snapshot_id, workspace_id, repository_id, manifest_sha256,
                    base_commit, tree_sha, manifest, created_at
                ) VALUES (%s, %s, %s, %s, '0'*40, '0'*40, '{}'::jsonb, %s)
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                (snap_id, test_setup["ws_id"], test_setup["repo_id"], "0" * 64, now),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.snapshot_publications (
                    publication_id, workspace_id, repository_id, snapshot_id,
                    status, fact_count, insight_count, edge_count,
                    graph_sha256, receipt_sha256, diagnostics, published_at, staged_at
                ) VALUES (%s, %s, %s, %s, 'published', 1, 0, 0, %s, %s, '{}', %s, %s)
                """,
                (uuid4(), test_setup["ws_id"], test_setup["repo_id"], snap_id, "0" * 64, "0" * 64, now, now),
            )

    # Seed native receipt
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

    body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Enrichment degraded test",
        "steps": ["execute step"],
        "applicability_scope": "algorithms",
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
    }

    res = client.post("/v1/proposals", headers=test_setup["headers"], json=body)
    assert res.status_code == 200
    data = res.json()
    assert data["evidence_binding"] == "verified"
    assert data["enrichment_status"] == "degraded"
    assert len(data["supporting_lineage"]) == 1


def test_optional_enrichment_unavailable_when_no_snapshot_published(test_setup: dict[str, Any]) -> None:
    """When no snapshot is published in the workspace/repository, create_proposal skips
    engine.query and records enrichment_status as 'unavailable'.
    """
    app = create_app(test_setup["cfg"], engine=FailingQueryEngine())
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

    body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Enrichment unavailable test",
        "steps": ["execute step"],
        "applicability_scope": "algorithms",
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
    }

    res = client.post("/v1/proposals", headers=test_setup["headers"], json=body)
    assert res.status_code == 200
    data = res.json()
    assert data["evidence_binding"] == "verified"
    assert data["enrichment_status"] == "unavailable"


def test_native_validity_ref_is_citation_only_and_cannot_grant_acceptance(test_setup: dict[str, Any]) -> None:
    """A citation in supporting evidence is citation only and cannot grant acceptance.
    Non-audit or non-settle receipt results in not_accepted.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # Seed receipt with kind='plan' and verdict=None
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
            kind="plan",
            verdict=None,
            issuer="planner",
            independent=False,
        )

    # Create proposal citing this plan receipt
    body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Citation only proposal",
        "steps": ["follow plan"],
        "applicability_scope": "planning",
        "supporting_evidence": [
            {
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "producer": "planner",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": rec["payload_sha256"],
                "native_validity_ref": str(rec["receipt_id"]),
            }
        ],
    }
    p_res = client.post("/v1/proposals", headers=test_setup["headers"], json=body)
    assert p_res.status_code == 200
    prop_id = p_res.json()["proposal_id"]

    # Check applicability
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
    assert "no_qualifying_audit_receipt" in app_data["reasons"]


def test_applicability_accepted_only_with_independent_pass_auditor_settle_receipt_on_exact_ids(
    test_setup: dict[str, Any]
) -> None:
    """Applicability is accepted only when an authoritative native receipt exists
    with kind='audit', verdict='PASS', independent=true, issuer='work-service/auditor-settle'
    for exact work_id, revision_id, candidate_id matching current work item.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # Seed qualifying audit receipt
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
            kind="audit",
            verdict="PASS",
            independent=True,
            issuer="work-service/auditor-settle",
        )

    # Create proposal citing this receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Audited procedure",
            "steps": ["execute validated code"],
            "applicability_scope": "production",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
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

    # Check applicability
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
    assert app_data["acceptance"] == "accepted"
    assert "authoritative_pass_receipt_verified" in app_data["reasons"]
    assert app_data["native_readback_sha256"] == rec["payload_sha256"]


def test_applicability_superseded_when_current_revision_or_candidate_differs(
    test_setup: dict[str, Any]
) -> None:
    """Applicability reports superseded_revision when current work item revision/candidate differs."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_old = uuid4()
    cand_old = uuid4()
    rev_new = uuid4()
    cand_new = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        # Receipt exists for rev_old
        rec = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_old,
            candidate_id=cand_old,
            kind="audit",
            verdict="PASS",
            independent=True,
            issuer="work-service/auditor-settle",
        )
        # Advance work item to rev_new, cand_new
        insert_test_work_revision(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_new,
            revision_number=2,
        )
        insert_test_candidate(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_new,
            candidate_id=cand_new,
        )
        insert_test_work_item(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            current_revision_id=rev_new,
            current_candidate_id=cand_new,
        )

    # Create proposal citing old receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Old rev procedure",
            "steps": ["step 1"],
            "applicability_scope": "scope",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec["payload_sha256"],
                    "native_validity_ref": str(rec["receipt_id"]),
                }
            ],
        },
    )
    prop_id = p_res.json()["proposal_id"]

    # Check applicability for rev_old when current is rev_new
    app_res = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_old),
            "candidate_id": str(cand_old),
        },
    )
    assert app_res.status_code == 200
    app_data = app_res.json()
    assert app_data["acceptance"] == "superseded_revision"
    assert "current_revision_or_candidate_differs" in app_data["reasons"]


def test_caller_cannot_supply_native_acceptance_or_confidence_fields(
    test_setup: dict[str, Any]
) -> None:
    """Caller is strictly forbidden from supplying native acceptance or confidence fields.
    Requests carrying extra fields are rejected with 422.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    # 1. Attempt on proposal create
    body_with_acceptance = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Malicious acceptance attempt",
        "steps": ["step"],
        "applicability_scope": "scope",
        "supporting_evidence": [
            {
                "workspace_id": str(test_setup["ws_id"]),
                "repository_id": str(test_setup["repo_id"]),
                "producer": "agent",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": "a" * 64,
                "native_validity_ref": str(uuid4()),
            }
        ],
        "native_acceptance": True,
        "confidence": 0.99,
        "accepted": True,
    }
    p_res = client.post("/v1/proposals", headers=test_setup["headers"], json=body_with_acceptance)
    assert p_res.status_code == 422

    # 2. Attempt on applicability check
    app_with_confidence = {
        "proposal_id": str(uuid4()),
        "work_id": str(uuid4()),
        "revision_id": str(uuid4()),
        "confidence": 0.95,
        "accepted": True,
    }
    a_res = client.post("/v1/applicability", headers=test_setup["headers"], json=app_with_confidence)
    assert a_res.status_code == 422


def test_candidate_less_request_does_not_accept_candidate_specific_receipt(
    test_setup: dict[str, Any]
) -> None:
    """A candidate-less applicability request (candidate_id=None or omitted) does not accept
    a candidate-specific receipt.

    Case 1: When work item current candidate is candidate-specific (cand_id), a candidate-less
    request differs from current candidate and returns superseded_revision.
    Case 2: When work item current candidate is NULL, a candidate-specific receipt in native DB
    does not match exact triple NULL candidate semantics, returning not_accepted.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()

    # Seed qualifying audit receipt for specific cand_id
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
            kind="audit",
            verdict="PASS",
            independent=True,
            issuer="work-service/auditor-settle",
        )

    # Create proposal citing this receipt
    p_res = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Candidate-specific procedure",
            "steps": ["execute candidate code"],
            "applicability_scope": "production",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
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

    # Case 1: Caller sends candidate-less request (candidate_id omitted)
    # Work item currently has current_candidate_id = cand_id.
    # Must NOT accept: preserves superseded_revision because current candidate differs.
    app_res_1 = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id,
            "work_id": str(work_id),
            "revision_id": str(rev_id),
        },
    )
    assert app_res_1.status_code == 200
    app_data_1 = app_res_1.json()
    assert app_data_1["acceptance"] == "superseded_revision"
    assert "current_revision_or_candidate_differs" in app_data_1["reasons"]

    # Case 2: Work item itself is candidate-less (current_candidate_id=NULL).
    # Caller sends candidate-less request (candidate_id=None).
    # Receipt in native DB is candidate-specific (cand_id).
    # Exact triple query (candidate_id IS NOT DISTINCT FROM NULL) must not match cand_id,
    # returning not_accepted with no_qualifying_audit_receipt.
    work_id_2 = uuid4()
    rev_id_2 = uuid4()
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        rec_2 = insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id_2,
            revision_id=rev_id_2,
            candidate_id=cand_id,
            kind="audit",
            verdict="PASS",
            independent=True,
            issuer="work-service/auditor-settle",
        )
        # Reset work item to candidate-less state
        with native_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE omp_work.work_items
                SET current_candidate_id = NULL
                WHERE workspace_id = %s AND work_id = %s
                """,
                (test_setup["ws_id"], work_id_2),
            )

    p_res_2 = client.post(
        "/v1/proposals",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "title": "Candidate-less work item procedure",
            "steps": ["execute step"],
            "applicability_scope": "production",
            "supporting_evidence": [
                {
                    "workspace_id": str(test_setup["ws_id"]),
                    "repository_id": str(test_setup["repo_id"]),
                    "producer": "work-service/auditor-settle",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": rec_2["payload_sha256"],
                    "native_validity_ref": str(rec_2["receipt_id"]),
                }
            ],
        },
    )
    assert p_res_2.status_code == 200
    prop_id_2 = p_res_2.json()["proposal_id"]

    app_res_2 = client.post(
        "/v1/applicability",
        headers=test_setup["headers"],
        json={
            "proposal_id": prop_id_2,
            "work_id": str(work_id_2),
            "revision_id": str(rev_id_2),
            "candidate_id": None,
        },
    )
    assert app_res_2.status_code == 200
    app_data_2 = app_res_2.json()
    assert app_data_2["acceptance"] == "not_accepted"
    assert "no_qualifying_audit_receipt" in app_data_2["reasons"]
