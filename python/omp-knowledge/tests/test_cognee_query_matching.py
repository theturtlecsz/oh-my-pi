from __future__ import annotations

from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter
from support.fixtures import make_test_enola_fixture


def _build_test_facts() -> list[dict]:
    return [
        {
            "id": "fact-01-session-migration",
            "kind": "symbol",
            "name": "session_migration_handler",
            "file": "src/session/migration.py",
            "props": {"symbol_kind": "function"},
            "relations": [],
        },
        {
            "id": "fact-02-session-only",
            "kind": "symbol",
            "name": "create_session",
            "file": "src/session/auth.py",
            "props": {"symbol_kind": "function"},
            "relations": [],
        },
        {
            "id": "fact-03-migration-only-z",
            "kind": "symbol",
            "name": "run_migration",
            "file": "src/db/migrate.py",
            "props": {"symbol_kind": "function"},
            "relations": [],
        },
        {
            "id": "fact-04-migration-only-a",
            "kind": "symbol",
            "name": "apply_migration",
            "file": "src/db/apply.py",
            "props": {"symbol_kind": "function"},
            "relations": [],
        },
        {
            "id": "fact-05-unrelated-user",
            "kind": "symbol",
            "name": "user_profile",
            "file": "src/models/user.py",
            "props": {"symbol_kind": "class"},
            "relations": [],
        },
        {
            "id": "fact-06-exact-name",
            "kind": "symbol",
            "name": "ExactTargetSymbol",
            "file": "src/core/target.py",
            "props": {"symbol_kind": "class"},
            "relations": [],
        },
    ]


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_multi_word_query_ranking_and_deterministic_order(tmp_path) -> None:
    """A multi-word query matches facts on any term, orders by matched-term count

    descending, then fact_id ascending, and is byte-for-byte deterministic.
    """
    config = KnowledgeConfig(state_dir=tmp_path / "query_multi_word")
    adapter = RealCogneeAdapter(config)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "e" * 64

    facts = _build_test_facts()
    await adapter.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=facts,
    )

    # Multi-word query matching both 'session' and 'migration'
    query_text = "session migration"
    res1 = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text=query_text,
    )

    # 4 facts match: fact-01 (2 terms), fact-02, fact-03, fact-04 (1 term each).
    # fact-05 and fact-06 do not match.
    assert res1.total_matched == 4
    matched_ids = [f.fact_id for f in res1.facts]

    # Highest term count (2 terms) must be first
    assert matched_ids[0] == "fact-01-session-migration"
    assert res1.facts[0].name == "session_migration_handler"

    # Tied term count (1 term) must be ordered by fact_id ascending
    tied_ids = matched_ids[1:]
    assert tied_ids == [
        "fact-02-session-only",
        "fact-03-migration-only-z",
        "fact-04-migration-only-a",
    ]

    # Two identical queries must return identical results in identical order
    res2 = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text=query_text,
    )
    assert [f.fact_id for f in res1.facts] == [f.fact_id for f in res2.facts]
    assert [f.name for f in res1.facts] == [f.name for f in res2.facts]


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_multi_word_query_limit_capping(tmp_path) -> None:
    """Multi-word query caps results at limit while preserving ranking order."""
    config = KnowledgeConfig(state_dir=tmp_path / "query_limit")
    adapter = RealCogneeAdapter(config)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "f" * 64

    facts = _build_test_facts()
    await adapter.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=facts,
    )

    res = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text="session migration",
        limit=2,
    )
    assert res.total_matched == 2
    assert len(res.facts) == 2
    assert res.facts[0].fact_id == "fact-01-session-migration"
    assert res.facts[1].fact_id == "fact-02-session-only"


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_stopword_only_query_returns_no_facts(tmp_path) -> None:
    """A stopword-only query or short-tokens-only query returns no facts."""
    config = KnowledgeConfig(state_dir=tmp_path / "query_stopwords")
    adapter = RealCogneeAdapter(config)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "1" * 64

    facts = _build_test_facts()
    await adapter.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=facts,
    )

    # Multi-word stopword queries
    for stopword_query in [
        "the and with",
        "to be or not to be",
        "in on at of by",
        "for that this those",
    ]:
        res = await adapter.query(
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id=snap_id,
            query_text=stopword_query,
        )
        assert res.total_matched == 0
        assert len(res.facts) == 0

    # Single stopword query
    res_single_sw = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text="the",
    )
    assert res_single_sw.total_matched == 0
    assert len(res_single_sw.facts) == 0


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_single_exact_name_and_star_parity(tmp_path) -> None:
    """A single exact-name query still returns the same facts as before, and '*' is unchanged."""
    config = KnowledgeConfig(state_dir=tmp_path / "query_parity")
    adapter = RealCogneeAdapter(config)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "2" * 64

    facts = _build_test_facts()
    await adapter.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=facts,
    )

    # Single exact-name query
    res_exact = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text="ExactTargetSymbol",
    )
    assert res_exact.total_matched == 1
    assert res_exact.facts[0].name == "ExactTargetSymbol"
    assert res_exact.facts[0].fact_id == "fact-06-exact-name"

    # Wildcard '*' query
    res_star = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text="*",
        limit=10,
    )
    assert res_star.total_matched >= len(facts)
    star_ids = {f.fact_id for f in res_star.facts}
    for f in facts:
        assert f["id"] in star_ids


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_title_shaped_query_over_enola_fixture(tmp_path) -> None:
    """A realistic work-item title query over the standard Enola test fixture."""
    config = KnowledgeConfig(state_dir=tmp_path / "query_title_enola")
    adapter = RealCogneeAdapter(config)

    ws_id = uuid4()
    repo_id = uuid4()
    snap_id = "3" * 64

    _, _, _, raw_facts = make_test_enola_fixture(repo_name="repo-title", snapshot_id=snap_id)
    await adapter.ingest_snapshot(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        facts=raw_facts,
    )

    # Realistic multi-word title with stopwords and punctuation
    title = "OMP-280: User authentication and models migration updates"
    res = await adapter.query(
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        query_text=title,
    )
    assert res.total_matched >= 1
    # Facts with 'User' and 'models' should match
    names = [f.name for f in res.facts]
    assert any("User" in n for n in names)
