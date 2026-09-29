"""Parallel-streams concurrency reads the written routing policy (OMP-420-s07).

When PARALLEL-STREAMS.json is absent the admit loop's in_flight_max and
provider_partitions come from omp_work.routing.policy; an existing file still
wins. partition_in_flight() reports active jobs per provider partition.
"""

from __future__ import annotations

import json
from pathlib import Path
import types
from typing import Any

import pytest

from omp_work import parallel_streams as ps
from omp_work.routing import policy as routing_policy


@pytest.fixture
def isolated_ps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    active_dir = tmp_path / "ACTIVE"
    runtime_dir = active_dir / "parallel-runtime"
    db_path = runtime_dir / "parallel.db"
    policy_path = active_dir / "PARALLEL-STREAMS.json"

    monkeypatch.setattr(ps, "ACTIVE", active_dir)
    monkeypatch.setattr(ps, "RUNTIME", runtime_dir)
    monkeypatch.setattr(ps, "DB", db_path)
    monkeypatch.setattr(ps, "POLICY", policy_path)

    ps.init_db()
    return {
        "active": active_dir,
        "runtime": runtime_dir,
        "db": db_path,
        "policy": policy_path,
    }


def test_load_policy_without_file_uses_routing_concurrency(isolated_ps: dict[str, Any]) -> None:
    """Fallback contract: missing PARALLEL-STREAMS.json defers concurrency to the routing policy."""
    concurrency = routing_policy.load_policy().concurrency

    pol = ps.load_policy()
    assert pol["in_flight_max"] == concurrency.in_flight_max
    assert pol["provider_partitions"] == dict(concurrency.providers)
    assert pol["default_expected_max_tokens"] == 40000


def test_existing_policy_file_wins(isolated_ps: dict[str, Any]) -> None:
    """Precedence contract: a written PARALLEL-STREAMS.json is returned unchanged."""
    isolated_ps["policy"].write_text(
        json.dumps({"in_flight_max": 1, "provider_partitions": {"gemini_flash": 1}}),
        encoding="utf-8",
    )

    pol = ps.load_policy()
    assert pol["in_flight_max"] == 1
    assert pol["provider_partitions"] == {"gemini_flash": 1}


def test_claims_stop_at_routing_partition_cap(
    isolated_ps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary contract: the routing concurrency cap gates admission per partition."""
    stub = types.SimpleNamespace(
        concurrency=routing_policy.ConcurrencyConfig(
            in_flight_max=5, providers={"gemini_flash": 1}
        )
    )
    monkeypatch.setattr(ps.routing_policy, "load_policy", lambda *a, **k: stub)

    for i in range(1, 3):
        ps.enqueue(
            job_id=f"cap-{i}",
            provider_partition="gemini_flash",
            path_lease=f"/repo/cap_{i}",
            expected_max=5000,
        )

    first = ps.claim_one()
    assert first is not None
    assert first["job_id"] == "cap-1"

    assert ps.claim_one() is None
    with ps._connect() as conn:
        row = conn.execute("SELECT blocker FROM jobs WHERE job_id='cap-2'").fetchone()
        assert row["blocker"] == "partition_full"

    assert ps.partition_in_flight() == {"gemini_flash": 1}


def test_partition_in_flight_counts_only_active_statuses(isolated_ps: dict[str, Any]) -> None:
    """Transformation contract: per-partition counts cover admitted/in_flight/returned/checking only."""
    for i in range(1, 8):
        ps.enqueue(
            job_id=f"cnt-{i}",
            provider_partition="gemini_flash",
            path_lease=f"/repo/cnt_{i}",
            expected_max=5000,
        )

    assert ps.partition_in_flight() == {}

    assert ps.claim_one() is not None
    assert ps.claim_one() is not None
    ps.mark_in_flight("cnt-2")
    with ps._connect() as conn:
        conn.execute("UPDATE jobs SET status='returned' WHERE job_id='cnt-3'")
        conn.execute("UPDATE jobs SET status='checking' WHERE job_id='cnt-4'")
        conn.execute("UPDATE jobs SET status='sealed' WHERE job_id='cnt-5'")
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id='cnt-6'")
        # cnt-7 stays backlog

    assert ps.partition_in_flight() == {"gemini_flash": 4}

    ps.release_at_checking_complete("cnt-1", final_status="sealed")
    ps.release_at_checking_complete("cnt-2", final_status="sealed")
    assert ps.partition_in_flight() == {"gemini_flash": 2}
