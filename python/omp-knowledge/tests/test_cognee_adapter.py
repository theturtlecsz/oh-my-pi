from __future__ import annotations

from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter
from omp_knowledge.engine.protocol import EngineStatus, EngineUnavailableError
from support.fixtures import make_test_enola_fixture
from support.null_engine import NullEngine


def test_cognee_adapter_unavailable_when_flagged(tmp_path) -> None:
    """When Cognee is marked unavailable, adapter operations raise EngineUnavailableError."""
    config = KnowledgeConfig(state_dir=tmp_path / "state_unavail")
    adapter = RealCogneeAdapter(config, available=False)

    with pytest.raises(EngineUnavailableError, match="engine_unavailable"):
        adapter._ensure_available()


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_real_cognee_adapter_lifecycle(tmp_path) -> None:
    """Verifies RealCogneeAdapter ingestion, scoped query, exact lookup, correction, status, and retirement."""
    config = KnowledgeConfig(state_dir=tmp_path / "adapter_lifecycle")
    adapter = RealCogneeAdapter(config)

    # 1. Processing status check
    status = await adapter.status()
    assert isinstance(status, EngineStatus)
    assert status.available is True
    assert status.engine_name == "RealCogneeAdapter"
    assert status.graph_engine == "ladybug==0.19.0"
    assert status.active_route.provider == "cognee"
    assert status.active_route.provider_version == "1.5.4"
    assert "ingest" in status.active_route.operations
    assert "query" in status.active_route.operations
    assert "lookup" in status.active_route.operations
    assert "correct" in status.active_route.operations
    assert "status" in status.active_route.operations

    workspace_id = uuid4()
    repository_id = uuid4()
    snap_id = "c" * 64

    _, _, _, raw_facts = make_test_enola_fixture(repo_name="repo-lifecycle", snapshot_id=snap_id)

    # 2. Plan preflight
    plan = await adapter.plan_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        facts=raw_facts,
    )
    assert plan.snapshot_id == snap_id
    assert len(plan.node_ids) >= 3
    assert len(plan.graph_sha256) == 64

    # 3. Ingestion
    ingest_result = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        facts=raw_facts,
        receipt=None,
        insights=[],
    )
    assert ingest_result.nodes_written >= 3
    assert len(ingest_result.graph_sha256) == 64
    assert ingest_result.graph_sha256 == plan.graph_sha256

    # 4. Scoped query
    query_result = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        query_text="User",
    )
    assert query_result.total_matched >= 1
    user_fact = query_result.facts[0]
    assert user_fact.name == "User"

    # 5. Exact lookup
    lookup_result = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        fact_id=user_fact.fact_id,
    )
    assert lookup_result.found is True
    assert lookup_result.fact is not None
    assert lookup_result.fact.fact_id == user_fact.fact_id

    # Lookup non-existent fact
    missing_lookup = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        fact_id="nonexistent_fact_id",
    )
    assert missing_lookup.found is False
    assert missing_lookup.fact is None

    # 6. Correction
    correct_result = await adapter.correct(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        fact_id=user_fact.fact_id,
        properties_update={"corrected_by": "test-suite", "rating": 5},
    )
    assert correct_result.success is True

    # 7. Retire
    retire_result = await adapter.retire(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
    )
    assert retire_result.success is True
    assert retire_result.nodes_deleted >= 3


@pytest.mark.asyncio
async def test_null_engine_parity(tmp_path) -> None:
    """Verifies that NullEngine double under tests/support/ conforms to the KnowledgeEngine protocol."""
    engine = NullEngine(available=True)

    status = await engine.status()
    assert status.available is True
    assert status.engine_name == "NullEngineDouble"

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "d" * 64

    _, _, _, raw_facts = make_test_enola_fixture(repo_name="null-repo", snapshot_id=snap_id)

    # Ingestion
    ingest = await engine.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=raw_facts,
    )
    assert ingest.nodes_written == len(raw_facts)

    # Query
    q = await engine.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text="User",
    )
    user_facts = [f for f in q.facts if f.name == "User"]
    assert len(user_facts) == 2
    assert q.total_matched >= 2

    # Lookup
    fact_0 = user_facts[0]
    lookup = await engine.lookup(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        fact_id=fact_0.fact_id,
    )
    assert lookup.found is True
    assert lookup.fact is not None

    # Correction
    correct = await engine.correct(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        fact_id=fact_0.fact_id,
        properties_update={"status": "corrected"},
    )
    assert correct.success is True

    # Retire
    retire = await engine.retire(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
    )
    assert retire.success is True
    assert retire.nodes_deleted == len(raw_facts)
