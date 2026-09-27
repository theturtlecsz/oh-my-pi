import asyncio
import sys
import time
from uuid import uuid4

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import (
    COGNEE_AVAILABLE,
    RealCogneeAdapter,
    snapshot_fact_node_id,
)
import cognee.infrastructure.databases.graph.get_graph_engine
import omp_knowledge.engine.cognee_adapter as adapter_module


class TimedGraphEngineProxy:
    def __init__(self, target, durations: list[tuple[str, float]]):
        self._target = target
        self._durations = durations

    async def add_edges(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await asyncio.wait_for(self._target.add_edges(*args, **kwargs), timeout=60.0)
        finally:
            elapsed = time.perf_counter() - t0
            self._durations.append(("add_edges", elapsed))

    async def query(self, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return await asyncio.wait_for(self._target.query(*args, **kwargs), timeout=60.0)
        finally:
            elapsed = time.perf_counter() - t0
            self._durations.append(("query", elapsed))

    def __getattr__(self, name):
        return getattr(self._target, name)


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_cognee_edge_ingest_scale(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SUBPROCESS_CALL_TIMEOUT", raising=False)

    call_durations: list[tuple[str, float]] = []
    gge_module = sys.modules["cognee.infrastructure.databases.graph.get_graph_engine"]
    real_get_graph_engine = gge_module.get_graph_engine

    async def timed_get_graph_engine(*args, **kwargs):
        engine = await real_get_graph_engine(*args, **kwargs)
        if isinstance(engine, TimedGraphEngineProxy):
            return engine
        return TimedGraphEngineProxy(engine, call_durations)

    monkeypatch.setattr(gge_module, "get_graph_engine", timed_get_graph_engine)
    monkeypatch.setattr(adapter_module, "get_graph_engine", timed_get_graph_engine)

    try:
        import cognee.tasks.storage.add_data_points as adp_module
        if hasattr(adp_module, "get_graph_engine"):
            monkeypatch.setattr(adp_module, "get_graph_engine", timed_get_graph_engine)
    except ImportError:
        pass

    config = KnowledgeConfig(state_dir=tmp_path / "scale_state")
    adapter = RealCogneeAdapter(config)

    workspace_id = uuid4()
    repository_id = uuid4()
    snapshot_id = "s" * 64

    # 30,000 facts, one with 5,000 relations, 20,000+ relations incl. one duplicate.
    num_facts = 30000
    facts: list[dict] = []

    # Hub fact: fact_0 has 5,000 relations + 1 duplicate = 5,001 relations
    hub_relations = [{"target_id": f"fact_{j}", "kind": "calls"} for j in range(1, 5001)]
    hub_relations.append({"target_id": "fact_1", "kind": "calls"})  # One duplicate relation

    facts.append({
        "kind": "symbol",
        "name": "hub_symbol",
        "fact_id": "fact_0",
        "file_path": "hub.py",
        "line": 1,
        "end_line": 10,
        "properties": {"symbol_kind": "function"},
        "relations": hub_relations,
    })

    # 15,000 relations across fact_1 through fact_15000 (total = 5,001 + 15,000 = 20,001)
    for i in range(1, 15001):
        facts.append({
            "kind": "symbol",
            "name": f"sym_{i}",
            "fact_id": f"fact_{i}",
            "file_path": f"module_{i // 100}.py",
            "line": 1,
            "end_line": 5,
            "properties": {"symbol_kind": "variable"},
            "relations": [{"target_id": f"fact_{i + 1}", "kind": "references"}],
        })

    for i in range(15001, num_facts):
        facts.append({
            "kind": "symbol",
            "name": f"sym_{i}",
            "fact_id": f"fact_{i}",
            "file_path": f"module_{i // 100}.py",
            "line": 1,
            "end_line": 5,
            "properties": {"symbol_kind": "variable"},
            "relations": [],
        })

    plan = await adapter.plan_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts=facts,
    )

    call_durations.clear()
    ingest_result = await adapter.ingest_snapshot(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        facts=facts,
    )

    # Acceptance assertions:
    # 1. Every timed add_edges/query call < 60 s
    assert len(call_durations) > 0, "Expected timed add_edges or query calls during ingest"
    for op, duration in call_durations:
        assert duration < 60.0, f"Timed call {op} took {duration:.2f}s >= 60.0s"

    # 2. graph_sha256 == plan's
    assert ingest_result.graph_sha256 == plan.graph_sha256

    # 3. Node count == len(plan.node_ids)
    engine = await gge_module.get_graph_engine()
    node_rows = await engine.query("MATCH (n:Node) RETURN count(n)")
    node_count = node_rows[0][0]
    assert node_count == len(plan.node_ids)

    # 4. EDGE count == len(plan.node_ids) - 1 + len(set(plan.edge_keys))
    edge_rows = await engine.query("MATCH ()-[r:EDGE]->() RETURN count(r)")
    edge_count = edge_rows[0][0]
    expected_edge_count = len(plan.node_ids) - 1 + len(set(plan.edge_keys))
    assert edge_count == expected_edge_count

    # 5. Hub fact has 5,000+ out-edges
    snapshot_label = f"{workspace_id}:{repository_id}@{snapshot_id}"
    hub_node_id = str(snapshot_fact_node_id(snapshot_label, "fact_0"))
    hub_rows = await engine.query(
        "MATCH (n:Node {id: $hub_id})-[r:EDGE]->() RETURN count(r)",
        {"hub_id": hub_node_id},
    )
    hub_out_edges = hub_rows[0][0]
    assert hub_out_edges >= 5000
