from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.backends import SnapshotScope
from omp_knowledge.engine.cognee_adapter import (
    COGNEE_AVAILABLE,
    RealCogneeAdapter,
    snapshot_repo_node_id,
)
from omp_work.knowledge_contracts import SnapshotRef
from omp_work.v1.canonical import canonical_json, sha256
from support.fixtures import (
    load_staged_fixture,
    make_test_enola_fixture,
    make_test_snapshot_ref,
)

INTEGRATION_ENABLED = os.environ.get("OMP_KNOWLEDGE_COGNEE_INTEGRATION") == "1"


@pytest.fixture(autouse=True)
def check_cognee_availability():
    if INTEGRATION_ENABLED and not COGNEE_AVAILABLE:
        pytest.fail(
            "OMP_KNOWLEDGE_COGNEE_INTEGRATION=1 is set, but cognee/ladybug could not be imported! "
            "Dependencies must be installed in the runtime environment."
        )


@pytest.mark.skipif(
    not INTEGRATION_ENABLED,
    reason="Set OMP_KNOWLEDGE_COGNEE_INTEGRATION=1 to run embedded Cognee + Ladybug integration tests",
)
@pytest.mark.asyncio
async def test_real_cognee_snapshot_isolation_and_full_fact_id_preservation(tmp_path) -> None:
    """Mandatory integration test exercising real Cognee + Ladybug embedded paths:
    1. Ingest Snapshot A containing full fact ID collision (same 'User' class across 2 files).
    2. Verify both User nodes exist independently and compute hash_A.
    3. Ingest Snapshot B. Verify query(A) is completely unchanged (query(A) == hash_A).
    4. Retire B -> verify B is removed and query(A) remains untouched.
    """
    config = KnowledgeConfig(state_dir=tmp_path / "knowledge_state")
    adapter = RealCogneeAdapter(config)

    workspace_id = uuid4()
    repository_id = uuid4()

    # 1. Ingest Snapshot A with full fact ID collision fixture
    snap_id_A = "a" * 64
    _, _, _, raw_facts_A = make_test_enola_fixture(repo_name="repo-A", snapshot_id=snap_id_A)
    snap_ref_A = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=snap_id_A)

    ingest_res_A = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref_A,
        facts=raw_facts_A,
        receipt=None,
        insights=[],
    )
    assert ingest_res_A.nodes_written >= 3

    # Query A: assert both 'User' facts survived without collapsing across files
    query_A = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
    )
    user_facts = [f for f in query_A.facts if f.name == "User"]
    assert len(user_facts) == 2, "Full fact ID collision must NOT collapse same-name facts across different files"
    files = {f.file_path for f in user_facts}
    assert files == {"src/models/user.py", "src/auth/user.py"}

    # Exact lookup for both User facts using their distinct Enola IDs
    lookup1 = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        fact_id=user_facts[0].fact_id,
    )
    assert lookup1.found is True
    assert lookup1.fact is not None
    assert lookup1.fact.name == "User"

    lookup2 = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        fact_id=user_facts[1].fact_id,
    )
    assert lookup2.found is True
    assert lookup2.fact is not None
    assert lookup2.fact.name == "User"
    assert lookup1.fact.fact_id != lookup2.fact.fact_id

    hash_A = sha256([f.model_dump(mode="json") for f in sorted(query_A.facts, key=lambda x: x.fact_id)])

    # 2. Ingest Snapshot B
    snap_id_B = "b" * 64
    _, _, _, raw_facts_B = make_test_enola_fixture(repo_name="repo-B", snapshot_id=snap_id_B)
    snap_ref_B = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=snap_id_B)

    ingest_res_B = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref_B,
        facts=raw_facts_B,
        receipt=None,
        insights=[],
    )
    assert ingest_res_B.nodes_written >= 3

    # Query A again: must be 100% byte-for-byte identical to hash_A
    query_A_after_B = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
    )
    hash_A_after_B = sha256([f.model_dump(mode="json") for f in sorted(query_A_after_B.facts, key=lambda x: x.fact_id)])
    assert hash_A_after_B == hash_A, "Ingesting Snapshot B must not mutate Snapshot A query results"

    # 3. Retire B
    retire_res = await adapter.retire(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_B,
    )
    assert retire_res.success is True

    # Query A again: retiring B must NOT delete A
    query_A_after_retire = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
    )
    hash_A_after_retire = sha256([f.model_dump(mode="json") for f in sorted(query_A_after_retire.facts, key=lambda x: x.fact_id)])
    assert hash_A_after_retire == hash_A, "Retiring Snapshot B must not delete or affect Snapshot A"


@pytest.mark.skipif(
    not INTEGRATION_ENABLED,
    reason="Set OMP_KNOWLEDGE_COGNEE_INTEGRATION=1 to run embedded Cognee + Ladybug integration tests",
)
@pytest.mark.asyncio
async def test_real_enola_fixtures_isolation_and_symbol_disambiguation(tmp_path) -> None:
    """Exercise real Enola 0.4.18 staged fixtures A and B (Failure 4):
    1. Ingest actual staged/A:
       Contains ts/src.normalize declared in both alpha.ts and beta.ts.
       ts/src.run calls ts/src.normalize without target_id.
       Verify no arbitrary binding across files (unambiguous resolution only).
    2. Query ts/src.normalize: both alpha and beta symbol facts exist.
    3. Ingest actual staged/B:
       Verify A-only symbols (ts/src.alphaOnlyA) do not leak into B query.
       Verify B-only symbols (py/pkg/beta.beta_only_b) do not leak into A query.
    4. Retire B: leaves A intact.
    """
    config = KnowledgeConfig(state_dir=tmp_path / "knowledge_state_enola")
    adapter = RealCogneeAdapter(config)

    workspace_id = uuid4()
    repository_id = uuid4()

    # Load actual staged fixture A
    facts_a, receipt_a, insights_a, raw_facts_a = load_staged_fixture("A")
    snap_id_a = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"
    snap_ref_a = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=snap_id_a)

    ingest_a = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref_a,
        facts=raw_facts_a,
        receipt=None,
        insights=[],
    )
    assert ingest_a.nodes_written == 20  # 19 facts + 1 repo node

    # Verify duplicate symbol disambiguation: ts/src.normalize exists in both alpha.ts and beta.ts
    query_norm = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_a,
        query_text="normalize",
    )
    ts_norm_facts = [f for f in query_norm.facts if f.name == "ts/src.normalize"]
    assert len(ts_norm_facts) == 2
    norm_files = {f.file_path for f in ts_norm_facts}
    assert norm_files == {"ts/src/alpha.ts", "ts/src/beta.ts"}

    # Ingest actual staged fixture B
    facts_b, receipt_b, insights_b, raw_facts_b = load_staged_fixture("B")
    snap_id_b = "b3649242961413ac43d41254871ebfd9a74bed88045d1c58972f67267216db55"
    snap_ref_b = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=snap_id_b)

    ingest_b = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref_b,
        facts=raw_facts_b,
        receipt=None,
        insights=[],
    )
    assert ingest_b.nodes_written == 20

    # Verify A-only symbol is in A but not B
    query_a_alpha = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_a,
        query_text="alphaOnlyA",
    )
    assert query_a_alpha.total_matched == 1

    query_b_alpha = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_b,
        query_text="alphaOnlyA",
    )
    assert query_b_alpha.total_matched == 0

    # Verify B-only symbol is in B but not A
    query_b_beta = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_b,
        query_text="beta_only_b",
    )
    assert query_b_beta.total_matched == 1

    query_a_beta = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_a,
        query_text="beta_only_b",
    )
    assert query_a_beta.total_matched == 0

    # Retire B
    retire_b = await adapter.retire(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_b,
    )
    assert retire_b.success is True

    # A remains completely intact
    query_a_check = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_a,
        query_text="alphaOnlyA",
    )
    assert query_a_check.total_matched == 1


@pytest.mark.skipif(
    not INTEGRATION_ENABLED,
    reason="Set OMP_KNOWLEDGE_COGNEE_INTEGRATION=1 to run embedded Cognee + Ladybug integration tests",
)
@pytest.mark.asyncio
async def test_real_cognee_repository_node_is_scoped_and_retired_with_snapshot(tmp_path) -> None:
    """The CodeRepository anchor node is part of its snapshot scope:
    1. Its identity is unchanged (first node id is snapshot_repo_node_id, equal to
       SnapshotScope.repo_node_id) and every fact node carries the scope label.
       The anchor itself has no ``repo`` field (the pinned Cognee model ignores it),
       so it is not inspected here.
    2. The dialect retire query selects the anchor by its deterministic id alongside
       the ``repo``-scoped fact nodes, retire deletes every node in scope (anchor
       included), and the scope reads back empty afterwards.
    3. The repository node never surfaces as a fact from query.
    """
    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine

    config = KnowledgeConfig(state_dir=tmp_path / "knowledge_state_repo_node")
    adapter = RealCogneeAdapter(config)

    workspace_id = uuid4()
    repository_id = uuid4()
    snap_id = "c" * 64
    scope = SnapshotScope(workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snap_id)
    _, _, _, raw_facts = make_test_enola_fixture(repo_name="repo-C", snapshot_id=snap_id)
    snap_ref = make_test_snapshot_ref(repository_id=repository_id, snapshot_id=snap_id)

    # 1. Projection: repository node identity is unchanged and it carries the scope label.
    data_points, node_ids, _, _, graph_hash = adapter._build_snapshot_graph(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref,
        facts=raw_facts,
    )
    expected_repo_node_id = snapshot_repo_node_id(scope.label, scope.repo_name)
    assert expected_repo_node_id == scope.repo_node_id()
    repo_node = data_points[0]
    assert repo_node.id == expected_repo_node_id
    assert node_ids[0] == str(expected_repo_node_id)
    assert all(dp.repo == scope.label for dp in data_points[1:]), "fact nodes must carry the scope label"
    assert all(dp.enola_id for dp in data_points[1:])
    assert len(node_ids) == 4  # 3 fixture facts + 1 repository node

    ingest = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_ref=snap_ref,
        facts=raw_facts,
        receipt=None,
        insights=[],
    )
    assert ingest.nodes_written == len(node_ids)
    assert ingest.node_ids == tuple(node_ids)
    assert ingest.graph_sha256 == graph_hash

    # 2. The dialect retire query anchors the repository node by its deterministic
    # id (OR-ed with the ``repo`` scope predicate) and returns it with the facts.
    graph_engine = await get_graph_engine()
    retire_query, retire_params = adapter._dialect.retire_query(scope)
    assert retire_params["repo_node_id"] == str(expected_repo_node_id)
    in_scope_before = {adapter._dialect.id_row(row) for row in await graph_engine.query(retire_query, retire_params)}
    assert str(expected_repo_node_id) in in_scope_before, "repository anchor must be returned by the retire query"
    assert in_scope_before == set(node_ids)

    # 3. The repository node is never reported as a fact.
    wildcard = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        query_text="*",
    )
    assert str(expected_repo_node_id) not in {str(f.node_id) for f in wildcard.facts}
    assert wildcard.total_matched == len(node_ids) - 1
    by_name = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
        query_text="repo-",
    )
    assert by_name.total_matched == 0

    # Retire removes the whole scope, repository node included.
    retired = await adapter.retire(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id,
    )
    assert retired.success is True
    assert retired.nodes_deleted == len(node_ids)
    in_scope_after = {adapter._dialect.id_row(row) for row in await graph_engine.query(retire_query, retire_params)}
    assert in_scope_after == set()
