from __future__ import annotations

from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter
from omp_knowledge.errors import SnapshotNotPublishedError
from omp_work.v1.canonical import sha256
from support.fixtures import (
    load_staged_fixture,
    make_test_enola_fixture,
)


@pytest.fixture(autouse=True)
def check_cognee_availability():
    if not COGNEE_AVAILABLE:
        pytest.fail(
            "Pinned Cognee/Ladybug environment must be available for real-engine snapshot isolation tests."
        )


@pytest.mark.asyncio
async def test_real_engine_snapshot_ab_isolation_and_publishing(tmp_path) -> None:
    """Real-engine snapshot isolation test verifying:
    1. publish A, verify hash A;
    2. publish B, verify A unchanged;
    3. aborted B unpublished (querying unpublished/aborted snapshot raises SnapshotNotPublishedError).
    """
    config = KnowledgeConfig(state_dir=tmp_path / "knowledge_state_ab")
    adapter = RealCogneeAdapter(config)

    workspace_id = uuid4()
    repository_id = uuid4()

    # =========================================================================
    # Step 1: publish A, verify hash A
    # =========================================================================
    snap_id_A = "a" * 64
    _, _, _, raw_facts_A = make_test_enola_fixture(repo_name="repo-A", snapshot_id=snap_id_A)

    # Ingest Snapshot A
    ingest_res_A = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        facts=raw_facts_A,
    )
    assert ingest_res_A.nodes_written >= 3

    # Publish Snapshot A
    assert adapter.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
    ) is True
    assert adapter.is_published(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
    ) is True

    # Query A: both 'User' facts survived without collapsing across files
    query_A = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
        require_published=True,
    )
    user_facts_A = [f for f in query_A.facts if f.name == "User"]
    assert len(user_facts_A) == 2, "Same-name facts across different files must not collapse"
    files_A = {f.file_path for f in user_facts_A}
    assert files_A == {"src/models/user.py", "src/auth/user.py"}

    # Exact lookup for both User facts using their distinct fact IDs
    lookup_1 = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        fact_id=user_facts_A[0].fact_id,
        require_published=True,
    )
    assert lookup_1.found is True
    assert lookup_1.fact is not None
    assert lookup_1.fact.name == "User"

    lookup_2 = await adapter.lookup(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        fact_id=user_facts_A[1].fact_id,
        require_published=True,
    )
    assert lookup_2.found is True
    assert lookup_2.fact is not None
    assert lookup_2.fact.name == "User"
    assert lookup_1.fact.fact_id != lookup_2.fact.fact_id

    # Compute canonical hash A
    hash_A = sha256([f.model_dump(mode="json") for f in sorted(query_A.facts, key=lambda x: x.fact_id)])
    assert len(hash_A) == 64

    # =========================================================================
    # Step 2: publish B, verify A unchanged
    # =========================================================================
    snap_id_B = "b" * 64
    _, _, _, raw_facts_B = make_test_enola_fixture(repo_name="repo-B", snapshot_id=snap_id_B)

    # Ingest Snapshot B
    ingest_res_B = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_B,
        facts=raw_facts_B,
    )
    assert ingest_res_B.nodes_written >= 3

    # Publish Snapshot B
    assert adapter.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_B,
    ) is True
    assert adapter.is_published(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_B,
    ) is True

    # Query B succeeds
    query_B = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_B,
        query_text="User",
        require_published=True,
    )
    assert query_B.total_matched >= 2

    # Query A again: must be 100% byte-for-byte identical to hash_A
    query_A_after_B = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
        require_published=True,
    )
    hash_A_after_B = sha256(
        [f.model_dump(mode="json") for f in sorted(query_A_after_B.facts, key=lambda x: x.fact_id)]
    )
    assert hash_A_after_B == hash_A, "Publishing Snapshot B must not mutate Snapshot A query results"

    # =========================================================================
    # Step 3: aborted B unpublished
    # =========================================================================
    snap_id_aborted = "f" * 64
    _, _, _, raw_facts_aborted = make_test_enola_fixture(
        repo_name="repo-aborted", snapshot_id=snap_id_aborted
    )

    # Ingest partial/aborted snapshot
    await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_aborted,
        facts=raw_facts_aborted,
    )

    # Abort publication
    adapter.abort(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_aborted,
    )

    # Verify aborted snapshot is NOT published
    assert adapter.is_published(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_aborted,
    ) is False

    # Attempting to query an unpublished/aborted snapshot raises SnapshotNotPublishedError
    with pytest.raises(SnapshotNotPublishedError, match="snapshot_not_published"):
        await adapter.query(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snap_id_aborted,
            query_text="*",
            require_published=True,
        )

    # Attempting exact lookup on unpublished/aborted snapshot also raises SnapshotNotPublishedError
    with pytest.raises(SnapshotNotPublishedError, match="snapshot_not_published"):
        await adapter.lookup(
            workspace_id=workspace_id,
            repository_id=repository_id,
            snapshot_id=snap_id_aborted,
            fact_id="any_fact_id",
            require_published=True,
        )

    # Verify Snapshot A is STILL completely unchanged
    query_A_after_aborted = await adapter.query(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_A,
        query_text="User",
        require_published=True,
    )
    hash_A_after_aborted = sha256(
        [f.model_dump(mode="json") for f in sorted(query_A_after_aborted.facts, key=lambda x: x.fact_id)]
    )
    assert hash_A_after_aborted == hash_A, "Aborting another snapshot must not affect Snapshot A"


@pytest.mark.asyncio
async def test_real_enola_fixtures_isolation_and_symbol_disambiguation(tmp_path) -> None:
    """Exercise real Enola 0.4.18 staged fixtures A and B:
    1. Ingest actual staged/A:
       Contains ts/src.normalize declared in both alpha.ts and beta.ts.
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
    _, _, _, raw_facts_a = load_staged_fixture("A")
    snap_id_a = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"

    ingest_a = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_a,
        facts=raw_facts_a,
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
    _, _, _, raw_facts_b = load_staged_fixture("B")
    snap_id_b = "b3649242961413ac43d41254871ebfd9a74bed88045d1c58972f67267216db55"

    ingest_b = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snap_id_b,
        facts=raw_facts_b,
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
