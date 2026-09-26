from __future__ import annotations

import time
from uuid import UUID

import pytest

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.engine.cognee_adapter import COGNEE_AVAILABLE, RealCogneeAdapter

FACT_COUNT = 50_000
RELATIONS_PER_FACT = 4
AMBIGUOUS_NAME = "ambiguous-symbol"


def _build_snapshot_facts() -> list[dict]:
    """Synthetic snapshot with 50k facts and 200k relations.

    Half the relations carry a target id; the other half carry only a target
    name that uniquely matches one fact, so both resolution paths are
    exercised at real-repository scale.
    """
    facts: list[dict] = []
    for i in range(FACT_COUNT):
        facts.append(
            {
                "id": f"fact-{i}",
                "kind": "symbol",
                "name": f"symbol-{i}",
                "file": f"src/module_{i}.py",
                "props": {"symbol_kind": "function"},
                "relations": [
                    {"kind": "calls", "target_id": f"fact-{(i + 1) % FACT_COUNT}"},
                    {"kind": "calls", "target_id": f"fact-{(i + 2) % FACT_COUNT}"},
                    {"kind": "references", "target": f"symbol-{(i + 3) % FACT_COUNT}"},
                    {"kind": "references", "target": f"symbol-{(i + 4) % FACT_COUNT}"},
                ],
            }
        )
    # Two facts share a name: a name-only relation to it is ambiguous and must
    # not create an edge, preserving the unique-name-match rule.
    for j in range(2):
        facts.append(
            {
                "id": f"ambiguous-{j}",
                "kind": "symbol",
                "name": AMBIGUOUS_NAME,
                "file": f"src/ambiguous_{j}.py",
                "props": {},
                "relations": [],
            }
        )
    facts[0]["relations"].append({"kind": "references", "target": AMBIGUOUS_NAME})
    return facts


@pytest.mark.skipif(not COGNEE_AVAILABLE, reason="Requires cognee and ladybug installed")
@pytest.mark.asyncio
async def test_name_relation_resolution_scales(tmp_path) -> None:
    """A large snapshot resolves name-only relations through one name index.

    Regression: resolving each relation by rescanning (and re-extracting the
    fields of) every fact made the real 96k-fact/370k-relation repository never
    finish. The graph must build in seconds, and the rule that only a unique
    name match creates an edge must still hold.
    """
    adapter = RealCogneeAdapter(KnowledgeConfig(state_dir=tmp_path / "state"), available=True)
    facts = _build_snapshot_facts()

    started = time.monotonic()
    plan = await adapter.plan_snapshot(
        workspace_id=UUID(int=1),
        repository_id=UUID(int=2),
        snapshot_id="a" * 64,
        facts=facts,
    )
    elapsed = time.monotonic() - started

    # Every id-resolvable relation (half) and every uniquely name-resolvable
    # relation (the other half) becomes an edge; the ambiguous name yields none.
    assert len(plan.edge_keys) == FACT_COUNT * RELATIONS_PER_FACT
    # 50k facts + 2 ambiguous facts + 1 repository node.
    assert len(plan.node_ids) == FACT_COUNT + 3
    assert elapsed < 30.0, f"snapshot graph build took {elapsed:.1f}s"
