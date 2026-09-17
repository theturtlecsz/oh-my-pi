from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5

from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
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
from omp_knowledge.context.compiler import ByteBudgetCompiler
from omp_knowledge.models import ContextCompileRequest
from omp_knowledge.server import create_app
from omp_knowledge.storage.db import get_db_connection
from omp_work.knowledge_contracts import CONTEXT_BUNDLE_IDENTITY_ENCODING
from omp_work.v1.canonical import canonical_json, sha256
from support.fixtures import write_test_capability_file
from support.native_fixture import (
    insert_test_candidate,
    insert_test_receipt,
    insert_test_work_item,
    insert_test_work_revision,
)
from support.null_engine import NullEngine


class FailingQueryEngine(NullEngine):
    async def query(self, **kwargs: Any) -> QueryResult:
        raise RuntimeError("simulated optional engine query failure")

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
    token = "test_s4_token"

    write_test_capability_file(
        cap_dir,
        token=token,
        actor_id=actor_id,
        workspaces=[ws_id],
        scopes=["knowledge.read", "knowledge.ingest", "knowledge.publish", "knowledge.admin"],
        name="test-s4-actor",
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


def test_mandatory_inputs_present_when_optional_enrichment_fails(test_setup: dict[str, Any]) -> None:
    """Mandatory inputs (work_revision, receipts, snapshot_manifest) are fully retained
    even when optional engine enrichment fails.
    """
    app = create_app(test_setup["cfg"], engine=FailingQueryEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    snap_id = "a" * 64
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
                ) VALUES (%s, %s, %s, %s, 'published', 0, 0, 0, %s, %s, '{}', %s, %s)
                """,
                (uuid4(), test_setup["ws_id"], test_setup["repo_id"], snap_id, "0" * 64, "0" * 64, now, now),
            )

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

    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "snapshot_id": snap_id,
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }

    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 200
    bundle = res.json()
    assert bundle["enrichment_status"] == "degraded"
    assert "work_revision" in bundle["mandatory"]
    assert bundle["mandatory"]["work_revision"]["work_id"] == str(work_id)
    assert len(bundle["mandatory"]["receipts"]) >= 1


def test_budget_below_mandatory_returns_422_and_persists_no_bundle(test_setup: dict[str, Any]) -> None:
    """A budget limit below mandatory input bytes returns 422 budget_insufficient_for_mandatory
    and persists zero rows in context_bundles.
    """
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
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

    # Impose an impossibly small byte limit (10 bytes)
    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 10},
    }

    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 422
    err = res.json()["error"] if "error" in res.json() else res.json()["detail"]["error"]
    assert err["code"] == "budget_insufficient_for_mandatory"

    # Verify zero bundle rows persisted
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.context_bundles")
            count = cur.fetchone()[0]
            assert count == 0


def test_budget_actual_is_exact_utf8_byte_count_of_persisted_content(test_setup: dict[str, Any]) -> None:
    """The reported budget.used must match the exact UTF-8 byte count of the persisted canonical JSON content."""
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
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 50000},
    }

    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 200
    bundle = res.json()
    b_id = UUID(bundle["bundle_id"])

    # Read persisted content from DB
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content, budget_actual FROM omp_knowledge.context_bundles WHERE bundle_id = %s",
                (b_id,),
            )
            row = cur.fetchone()
            assert row is not None
            persisted_content = row[0]
            persisted_budget = row[1]

    measured_bytes = len(canonical_json(persisted_content).encode("utf-8"))
    assert bundle["budget"]["used"] == measured_bytes
    assert persisted_budget["used"] == measured_bytes


def test_row_limit_is_not_a_budget_optional_dropped_by_bytes_not_rows(test_setup: dict[str, Any]) -> None:
    """Optional elements are dropped strictly by byte limits, not row counts."""
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
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

    # Insert two proposals into knowledge DB: one small, one very large
    small_pid = uuid4()
    large_pid = uuid4()
    now = datetime.now(timezone.utc)

    small_prop = {
        "proposal_id": str(small_pid),
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Small proposal",
        "steps": ["step 1"],
    }
    large_prop = {
        "proposal_id": str(large_pid),
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "title": "Large proposal " + ("X" * 3000),
        "steps": ["step " + str(i) for i in range(100)],
    }

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
                (small_pid, test_setup["ws_id"], test_setup["repo_id"], json.dumps(small_prop), sha256(small_prop), now),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.procedure_proposals (
                    proposal_id, workspace_id, repository_id, proposal_json,
                    proposal_sha256, state, attribution, evidence_binding,
                    enrichment_status, supporting_lineage, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'proposal', '{}'::jsonb, 'verified', 'applied', '[]'::jsonb, %s)
                """,
                (large_pid, test_setup["ws_id"], test_setup["repo_id"], json.dumps(large_prop), sha256(large_prop), now),
            )

    # First compile with huge budget to discover mandatory size
    res_large = client.post(
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
    assert res_large.status_code == 200
    mandatory_used = res_large.json()["budget"]["mandatory_used"]

    # Now compile with budget enough for mandatory + small proposal (~500 bytes), but NOT large proposal
    tight_limit = mandatory_used + 700
    res_tight = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": tight_limit},
        },
    )
    assert res_tight.status_code == 200
    tight_bundle = res_tight.json()
    # Large proposal was dropped based on byte overflow, not row limits
    assert any(f"proposal:{large_pid}" in item for item in tight_bundle["budget"]["dropped_optional"])
    assert str(large_pid) in tight_bundle["excluded_proposal_ids"]


def test_bundle_identity_lineage_and_immutability(test_setup: dict[str, Any]) -> None:
    """Bundle identity is deterministic uuid5 of bundle sha256, lineage is persisted,
    and any update/delete raises immutable error.
    """
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

    res = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 50000},
        },
    )
    assert res.status_code == 200
    bundle = res.json()
    b_id = UUID(bundle["bundle_id"])
    b_sha = bundle["bundle_sha256"]

    # Verify uuid5 derivation
    expected_uuid5 = uuid5(NAMESPACE_OID, f"omp-context-bundle:{b_sha}")
    assert b_id == expected_uuid5

    # Verify immutability trigger raises on update or delete
    with psycopg.connect(test_setup["cfg"].pg_connection_string()) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error, match="immutable context bundle"):
                cur.execute(
                    "UPDATE omp_knowledge.context_bundles SET stage = 'altered' WHERE bundle_id = %s",
                    (b_id,),
                )
            conn.rollback()

            with pytest.raises(psycopg.Error, match="immutable context bundle"):
                cur.execute(
                    "DELETE FROM omp_knowledge.context_bundles WHERE bundle_id = %s",
                    (b_id,),
                )
            conn.rollback()


def test_token_budget_method_routes_to_missing_owner_422(test_setup: dict[str, Any]) -> None:
    """The token budget method routes explicitly to missing FLEET-5 owner returning 422."""
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    res = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "stage": "planning",
            "budget": {"method": "tokens", "limit": 2048},
        },
    )
    assert res.status_code == 422
    err = res.json()["error"]
    assert err["code"] == "budget_method_unavailable"
    assert err["owner"] == "FLEET-5"


def test_mandatory_native_readback_missing_revision_fails_closed_422(test_setup: dict[str, Any]) -> None:
    """Missing mandatory work_revision in native ledger fails closed with 422 work_revision_unverifiable
    and persists zero rows in context_bundles.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    missing_rev_id = uuid4()

    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(missing_rev_id),
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }

    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 422
    data = res.json()
    err = data.get("error") or data.get("detail", {}).get("error", {})
    assert err.get("code") == "work_revision_unverifiable"

    # Verify zero rows persisted in context_bundles
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.context_bundles WHERE revision_id = %s", (missing_rev_id,))
            assert cur.fetchone()[0] == 0


def test_mandatory_native_readback_unavailable_db_fails_closed_503(
    test_setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When native DB is unavailable during mandatory readback, fails closed with 503 native_source_unavailable
    and persists zero rows in context_bundles.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()

    import omp_knowledge.context.compiler as comp_mod
    from omp_knowledge.native.consumer import NativeSourceUnavailableError

    def failing_session(*args: Any, **kwargs: Any):
        raise NativeSourceUnavailableError("simulated native DB connection failure")

    monkeypatch.setattr(comp_mod, "native_readonly_session", failing_session)

    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "stage": "planning",
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }

    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 503
    data = res.json()
    err = data.get("error") or data.get("detail", {}).get("error", {})
    assert err.get("code") == "native_source_unavailable"

    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_knowledge.context_bundles WHERE work_id = %s", (work_id,))
            assert cur.fetchone()[0] == 0


def test_bundle_identity_complete_request_and_conflict_replay(test_setup: dict[str, Any]) -> None:
    """Bundle identity derives from complete request context and budget spec.
    Different stages or candidates produce distinct bundle IDs and rows,
    while exact replayed requests return the existing exact row.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand1_id = uuid4()
    cand2_id = uuid4()

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand1_id,
        )

    # 1. Compile stage 'planning'
    res_plan = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand1_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert res_plan.status_code == 200
    b_plan = res_plan.json()

    # 2. Compile stage 'execution' (identical work/rev/cand/content, but different stage)
    res_exec = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand1_id),
            "stage": "execution",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert res_exec.status_code == 200
    b_exec = res_exec.json()

    # Different stages must produce distinct bundle IDs and distinct sha256
    assert b_plan["bundle_id"] != b_exec["bundle_id"]
    assert b_plan["bundle_sha256"] != b_exec["bundle_sha256"]

    # 3. Compile candidate 2 vs candidate 1
    res_cand2 = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand2_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert res_cand2.status_code == 200
    b_cand2 = res_cand2.json()
    assert b_cand2["bundle_id"] != b_plan["bundle_id"]

    # 4. Replay identical request returns exact persisted row without modifying immutable table
    res_replay = client.post(
        "/v1/context/compile",
        headers=test_setup["headers"],
        json={
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand1_id),
            "stage": "planning",
            "budget": {"method": "utf8_bytes", "limit": 100000},
        },
    )
    assert res_replay.status_code == 200
    b_replay = res_replay.json()
    assert b_replay["bundle_id"] == b_plan["bundle_id"]
    assert b_replay["bundle_sha256"] == b_plan["bundle_sha256"]
    assert b_replay["compiled_at"] == b_plan["compiled_at"]


def test_applied_optional_enrichment_with_healthy_engine(test_setup: dict[str, Any]) -> None:
    """When a snapshot is published and engine is healthy, optional enrichment runs
    and status is 'applied'.
    """
    engine = NullEngine(available=True)
    snap_id = "snap-applied-" + str(uuid4())[:8]
    engine.snapshots[snap_id] = [
        {"id": "fact-1", "name": "planning helper symbol", "kind": "symbol", "file": "src/plan.py", "line": 10}
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

    work_id = uuid4()
    rev_id = uuid4()
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=uuid4(),
        )

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
    assert "fact:0" in bundle["optional"]


def _insert_snapshot_with_manifest(test_setup: dict[str, Any], snap_id: str, manifest: dict[str, Any]) -> None:
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.snapshots (
                snapshot_id, workspace_id, repository_id, manifest_sha256,
                base_commit, tree_sha, manifest, created_at
            ) VALUES (%s, %s, %s, %s, '0'*40, '0'*40, %s::jsonb, %s)
            """,
            (
                snap_id,
                test_setup["ws_id"],
                test_setup["repo_id"],
                "0" * 64,
                json.dumps(manifest),
                datetime.now(timezone.utc),
            ),
        )


def test_content_canonical_json_is_exact_python_bytes_bound_to_identity_and_budget(
    test_setup: dict[str, Any],
) -> None:
    """content_canonical_json is the exact canonical_json({mandatory, optional}) bytes the compiler
    budgeted, embedded verbatim inside identity_canonical_json (so bundle_sha256 covers it), persisted,
    and returned byte-identical on replay. Vectors cover integral floats, exponent formatting, and
    non-BMP keys/values where a TypeScript re-serialization would differ.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id = uuid4()
    rev_id = uuid4()
    cand_id = uuid4()
    snap_id = "c" * 64
    _insert_snapshot_with_manifest(
        test_setup,
        snap_id,
        {
            # jsonb keeps numeric scale, so 1.0 and 1e-07 round-trip as floats; large exponents
            # would collapse to integers in jsonb and are covered by the TS fixture instead.
            "ratio": 1.0,
            "tiny": 1e-07,
            "\U0001F600": "non-bmp key",
            "�": "replacement key",
            "glyph": "\U0001D518 \U0001F680",
        },
    )

    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )

    compile_body = {
        "workspace_id": str(test_setup["ws_id"]),
        "repository_id": str(test_setup["repo_id"]),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": str(cand_id),
        "stage": "planning",
        "snapshot_id": snap_id,
        "budget": {"method": "utf8_bytes", "limit": 100000},
    }
    res = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert res.status_code == 200
    bundle = res.json()

    content_bytes = bundle["content_canonical_json"]
    assert isinstance(content_bytes, str) and content_bytes

    # Exact Python canonical bytes of the served content, and the budgeted byte count
    assert content_bytes == canonical_json({"mandatory": bundle["mandatory"], "optional": bundle["optional"]})
    assert len(content_bytes.encode("utf-8")) == bundle["budget"]["used"]

    # Python formatting is preserved literally (no TS-style 1 / 1e-7 / 10000000000000000 / \u escapes)
    manifest_bytes = canonical_json(bundle["mandatory"]["snapshot_manifest"])
    assert '"ratio":1.0' in manifest_bytes
    assert '"tiny":1e-07' in manifest_bytes
    assert "\U0001D518" in manifest_bytes
    assert manifest_bytes.index('"�"') < manifest_bytes.index('"\U0001F600"')

    # Byte-range binding: content bytes sit verbatim between the sorted candidate_id and identity_encoding keys
    identity_json = bundle["identity_canonical_json"]
    assert f',"content":{content_bytes},"identity_encoding":"{CONTEXT_BUNDLE_IDENTITY_ENCODING}",' in identity_json
    assert hashlib.sha256(identity_json.encode("utf-8")).hexdigest() == bundle["bundle_sha256"]

    # Persisted column holds the identical bytes; replay and by-id read return them unchanged
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT content_canonical_json FROM omp_knowledge.context_bundles WHERE bundle_id = %s",
            (UUID(bundle["bundle_id"]),),
        ).fetchone()
    assert row is not None and row[0] == content_bytes

    replay = client.post("/v1/context/compile", headers=test_setup["headers"], json=compile_body)
    assert replay.status_code == 200
    assert replay.json()["content_canonical_json"] == content_bytes
    assert replay.json()["identity_canonical_json"] == identity_json

    by_id = client.get(f"/v1/context/bundles/{bundle['bundle_id']}", headers=test_setup["headers"])
    assert by_id.status_code == 200
    assert by_id.json()["content_canonical_json"] == content_bytes


def test_legacy_bundle_row_without_content_bytes_is_returned_unverifiable(test_setup: dict[str, Any]) -> None:
    """Rows persisted before migration 0005 carry NULL content_canonical_json. They stay readable for
    history but expose null bytes, which consumers must refuse (fail closed) rather than rebuild.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    legacy_id = uuid4()
    now = datetime.now(timezone.utc)
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.context_bundles (
                bundle_id, bundle_sha256, workspace_id, repository_id,
                work_id, revision_id, candidate_id, stage,
                snapshot_id, proposal_lineage, receipt_lineage,
                budget_spec, budget_actual, enrichment_status,
                excluded_proposal_ids, content, compiled_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NULL, 'planning', NULL, '[]', '[]', %s, %s, 'unavailable', '[]', %s, %s)
            """,
            (
                legacy_id,
                "0" * 64,
                test_setup["ws_id"],
                test_setup["repo_id"],
                uuid4(),
                uuid4(),
                json.dumps({"method": "utf8_bytes", "limit": 1000, "tokenizer_id": None}),
                json.dumps({"method": "utf8_bytes", "limit": 1000, "used": 2, "mandatory_used": 2, "dropped_optional": []}),
                json.dumps({"mandatory": {"legacy": 1.0}, "optional": {}}),
                now,
            ),
        )

    res = client.get(f"/v1/context/bundles/{legacy_id}", headers=test_setup["headers"])
    assert res.status_code == 200
    legacy = res.json()
    assert legacy["content_canonical_json"] is None
    assert legacy["identity_canonical_json"] is None
    assert legacy["mandatory"] == {"legacy": 1.0}


_LEGACY_V1_ENCODING = "omp-context-bundle-identity/v1"


def _insert_native_receipt(test_setup: dict[str, Any], work_id: UUID, rev_id: UUID, cand_id: UUID) -> None:
    with psycopg.connect(
        host=test_setup["cfg"].native_pg_host,
        port=test_setup["cfg"].native_pg_port,
        dbname=test_setup["cfg"].native_pg_database,
        user="postgres",
        autocommit=True,
    ) as native_conn:
        insert_test_receipt(
            native_conn,
            workspace_id=test_setup["ws_id"],
            work_id=work_id,
            revision_id=rev_id,
            candidate_id=cand_id,
        )


def _insert_legacy_bundle_row(
    test_setup: dict[str, Any],
    *,
    request: dict[str, Any],
    content: dict[str, Any],
    bundle_sha256: str,
    identity_encoding: str | None,
    identity_canonical_json: str | None,
    compiled_at: datetime,
) -> UUID:
    """Persist a row the way an older compiler did: identity columns NULL (pre-0004) or
    v1 encoding with identity bytes but no content bytes (pre-0005). Never backfilled."""
    bundle_id = uuid5(NAMESPACE_OID, f"omp-context-bundle:{bundle_sha256}")
    content_bytes = canonical_json(content)
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_knowledge.context_bundles (
                bundle_id, bundle_sha256, workspace_id, repository_id,
                work_id, revision_id, candidate_id, stage,
                snapshot_id, proposal_lineage, receipt_lineage,
                budget_spec, budget_actual, enrichment_status,
                excluded_proposal_ids, content, compiled_at,
                identity_encoding, identity_canonical_json, content_canonical_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '[]', '[]', %s, %s, 'unavailable', '[]', %s, %s, %s, %s, NULL)
            """,
            (
                bundle_id,
                bundle_sha256,
                UUID(request["workspace_id"]),
                UUID(request["repository_id"]),
                UUID(request["work_id"]),
                UUID(request["revision_id"]),
                UUID(request["candidate_id"]),
                request["stage"],
                request["snapshot_id"],
                json.dumps({**request["budget"], "tokenizer_id": None}),
                json.dumps(
                    {
                        **request["budget"],
                        "used": len(content_bytes.encode("utf-8")),
                        "mandatory_used": len(canonical_json(content["mandatory"]).encode("utf-8")),
                        "dropped_optional": [],
                    }
                ),
                json.dumps(content),
                compiled_at,
                identity_encoding,
                identity_canonical_json,
            ),
        )
    return bundle_id


def _select_bundle_rows(test_setup: dict[str, Any], bundle_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM omp_knowledge.context_bundles WHERE bundle_id = ANY(%s)",
                (bundle_ids,),
            )
            return {row["bundle_id"]: row for row in cur.fetchall()}


def test_identity_encoding_v2_gives_new_bundle_and_leaves_legacy_rows_untouched(
    test_setup: dict[str, Any],
) -> None:
    """Rows compiled before migrations 0004/0005 keep their bundle_id and bytes forever. After the
    upgrade the same logical request compiles to a NEW bundle (v2 identity payload carries the
    encoding tag), the legacy rows are neither rewritten nor backfilled, and replay of the current
    bundle returns exactly the persisted bytes and metadata.
    """
    app = create_app(test_setup["cfg"], engine=NullEngine())
    client = TestClient(app)

    work_id, rev_id, cand_id = uuid4(), uuid4(), uuid4()
    snap_id = "d" * 64
    _insert_snapshot_with_manifest(
        test_setup,
        snap_id,
        {"ratio": 1.0, "tiny": 1e-07, "count": -17, "glyph": "éléphant 🚀 漢字 \U0001D518", "�": "replacement key"},
    )
    _insert_native_receipt(test_setup, work_id, rev_id, cand_id)

    def request_for(stage: str) -> dict[str, Any]:
        return {
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": stage,
            "snapshot_id": snap_id,
            "budget": {"method": "utf8_bytes", "limit": 100000},
        }

    # Content is a pure function of the ledger inputs (stage is not part of content), so one
    # probe compile tells us the exact content an older compiler persisted for the same inputs.
    probe = client.post("/v1/context/compile", headers=test_setup["headers"], json=request_for("probe"))
    assert probe.status_code == 200
    content = {"mandatory": probe.json()["mandatory"], "optional": probe.json()["optional"]}
    assert '"ratio":1.0' in canonical_json(content) and "\U0001D518" in canonical_json(content)

    def v1_identity(request: dict[str, Any]) -> str:
        # Exact pre-v2 payload: no identity_encoding key
        return canonical_json(
            {
                "workspace_id": request["workspace_id"],
                "repository_id": request["repository_id"],
                "work_id": request["work_id"],
                "revision_id": request["revision_id"],
                "candidate_id": request["candidate_id"],
                "stage": request["stage"],
                "snapshot_id": request["snapshot_id"],
                "budget_spec": {**request["budget"], "tokenizer_id": None},
                "content": content,
            }
        )

    long_ago = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pre_0004_request = request_for("execution")
    pre_0004_id = _insert_legacy_bundle_row(
        test_setup,
        request=pre_0004_request,
        content=content,
        bundle_sha256=sha256(content),  # content-only hash, no identity bytes at all
        identity_encoding=None,
        identity_canonical_json=None,
        compiled_at=long_ago,
    )
    pre_0005_request = request_for("review")
    pre_0005_identity = v1_identity(pre_0005_request)
    pre_0005_id = _insert_legacy_bundle_row(
        test_setup,
        request=pre_0005_request,
        content=content,
        bundle_sha256=hashlib.sha256(pre_0005_identity.encode("utf-8")).hexdigest(),
        identity_encoding=_LEGACY_V1_ENCODING,
        identity_canonical_json=pre_0005_identity,
        compiled_at=long_ago,
    )
    legacy_before = _select_bundle_rows(test_setup, [pre_0004_id, pre_0005_id])
    assert set(legacy_before) == {pre_0004_id, pre_0005_id}

    for legacy_id, request in ((pre_0004_id, pre_0004_request), (pre_0005_id, pre_0005_request)):
        fresh = client.post("/v1/context/compile", headers=test_setup["headers"], json=request)
        assert fresh.status_code == 200
        bundle = fresh.json()

        # Same logical inputs, NEW current bundle under the v2 representation
        assert UUID(bundle["bundle_id"]) != legacy_id
        assert bundle["mandatory"] == content["mandatory"] and bundle["optional"] == content["optional"]
        assert bundle["identity_encoding"] == CONTEXT_BUNDLE_IDENTITY_ENCODING == "omp-context-bundle-identity/v2"
        identity = json.loads(bundle["identity_canonical_json"])
        assert identity["identity_encoding"] == CONTEXT_BUNDLE_IDENTITY_ENCODING
        assert identity["content"] == content
        assert bundle["identity_canonical_json"] == canonical_json(identity)
        assert hashlib.sha256(bundle["identity_canonical_json"].encode("utf-8")).hexdigest() == bundle["bundle_sha256"]
        assert UUID(bundle["bundle_id"]) == uuid5(NAMESPACE_OID, f"omp-context-bundle:{bundle['bundle_sha256']}")
        assert bundle["content_canonical_json"] == canonical_json(content)
        assert bundle["budget"]["used"] == len(bundle["content_canonical_json"].encode("utf-8"))

        # Current replay returns the persisted row verbatim: bytes, identity, and metadata
        replay = client.post("/v1/context/compile", headers=test_setup["headers"], json=request)
        assert replay.status_code == 200
        assert replay.json() == bundle
        by_id = client.get(f"/v1/context/bundles/{bundle['bundle_id']}", headers=test_setup["headers"])
        assert by_id.status_code == 200
        assert by_id.json() == bundle

    # Legacy rows: byte-for-byte unchanged, still readable, still unverifiable (no backfill)
    assert _select_bundle_rows(test_setup, [pre_0004_id, pre_0005_id]) == legacy_before
    pre_0004 = client.get(f"/v1/context/bundles/{pre_0004_id}", headers=test_setup["headers"]).json()
    assert pre_0004["identity_encoding"] is None
    assert pre_0004["identity_canonical_json"] is None
    assert pre_0004["content_canonical_json"] is None
    assert pre_0004["mandatory"] == content["mandatory"]
    pre_0005 = client.get(f"/v1/context/bundles/{pre_0005_id}", headers=test_setup["headers"]).json()
    assert pre_0005["identity_encoding"] == _LEGACY_V1_ENCODING
    assert pre_0005["identity_canonical_json"] == pre_0005_identity
    assert pre_0005["content_canonical_json"] is None

    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM omp_knowledge.context_bundles WHERE work_id = %s AND revision_id = %s",
            (work_id, rev_id),
        ).fetchone()
    assert row is not None and row[0] == 5  # probe + 2 legacy + 2 current


class _RacingCursor:
    """Cursor proxy that lets a rival commit the same bundle_id between our replay check and INSERT."""

    def __init__(self, inner: psycopg.Cursor, on_insert: Any) -> None:
        self._inner = inner
        self._on_insert = on_insert

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if "INSERT INTO omp_knowledge.context_bundles" in str(query):
            self._on_insert(params)
        return self._inner.execute(query, params, **kwargs)

    def __enter__(self) -> "_RacingCursor":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RacingConnection:
    def __init__(self, inner: psycopg.Connection, on_insert: Any) -> None:
        self._inner = inner
        self._on_insert = on_insert

    def cursor(self, *args: Any, **kwargs: Any) -> _RacingCursor:
        return _RacingCursor(self._inner.cursor(*args, **kwargs), self._on_insert)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_concurrent_duplicate_compile_returns_persisted_winner_row(test_setup: dict[str, Any]) -> None:
    """Two compiles of the same request race: the loser's INSERT hits ON CONFLICT DO NOTHING and must
    return the winner's persisted row (compiled_at, enrichment_status, budget, identity and content
    bytes), never its own in-memory bundle.
    """
    work_id, rev_id, cand_id = uuid4(), uuid4(), uuid4()
    snap_id = "e" * 64
    _insert_snapshot_with_manifest(test_setup, snap_id, {"ratio": 2.0, "glyph": "ñandú 👾", "n": -0.5})
    _insert_native_receipt(test_setup, work_id, rev_id, cand_id)

    request = ContextCompileRequest.model_validate(
        {
            "workspace_id": str(test_setup["ws_id"]),
            "repository_id": str(test_setup["repo_id"]),
            "work_id": str(work_id),
            "revision_id": str(rev_id),
            "candidate_id": str(cand_id),
            "stage": "planning",
            "snapshot_id": snap_id,
            "budget": {"method": "utf8_bytes", "limit": 100000},
        }
    )
    winner_compiled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    winner_ids: dict[str, UUID] = {}

    def rival_commits_first(params: tuple[Any, ...]) -> None:
        # Same bundle_id/hash/bytes (same logical compile), but the rival ran earlier with a degraded engine
        rival = list(params)
        rival[13] = "degraded"
        rival[16] = winner_compiled_at
        winner_ids["bundle_id"] = rival[0]
        with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO omp_knowledge.context_bundles (
                    bundle_id, bundle_sha256, workspace_id, repository_id,
                    work_id, revision_id, candidate_id, stage,
                    snapshot_id, proposal_lineage, receipt_lineage,
                    budget_spec, budget_actual, enrichment_status,
                    excluded_proposal_ids, content, compiled_at,
                    identity_encoding, identity_canonical_json, content_canonical_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rival,
            )

    conn = get_db_connection(test_setup["cfg"])
    try:
        compiler = ByteBudgetCompiler(test_setup["cfg"], _RacingConnection(conn, rival_commits_first))  # type: ignore[arg-type]
        loser = asyncio.run(compiler.compile(request, test_setup["actor_id"], NullEngine()))
    finally:
        conn.close()

    assert loser.bundle_id == winner_ids["bundle_id"]
    assert loser.enrichment_status == "degraded"  # persisted winner, not the loser's in-memory "unavailable"
    assert loser.compiled_at == winner_compiled_at

    with psycopg.connect(test_setup["cfg"].pg_connection_string(), autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT count(*) AS n FROM omp_knowledge.context_bundles WHERE work_id = %s AND revision_id = %s",
                (work_id, rev_id),
            )
            assert cur.fetchone()["n"] == 1
    rows = _select_bundle_rows(test_setup, [loser.bundle_id])
    winner = rows[loser.bundle_id]
    assert loser.identity_canonical_json == winner["identity_canonical_json"]
    assert loser.content_canonical_json == winner["content_canonical_json"]
    assert loser.bundle_sha256 == winner["bundle_sha256"]
    assert loser.identity_encoding == winner["identity_encoding"] == CONTEXT_BUNDLE_IDENTITY_ENCODING
    assert loser.budget.model_dump(mode="json") == winner["budget_actual"]
    assert loser.model_dump(mode="json")["mandatory"] == winner["content"]["mandatory"]
    assert '"ratio":2.0' in loser.content_canonical_json and "👾" in loser.content_canonical_json
