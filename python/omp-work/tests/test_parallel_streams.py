from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omp_work import parallel_streams as ps
from omp_work.__main__ import main


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


def test_claim_allocates_lease_and_reservation_and_transitions_status(isolated_ps: dict[str, Any]) -> None:
    """External contract: claim_one assigns admission, path lease, and budget reservation."""
    res = ps.enqueue(
        job_id="job-101",
        provider_partition="gemini_flash",
        path_lease="/repo/src/module_a.py",
        expected_max=25000,
        mission_id="mission-alpha",
        packet_path="/tmp/packet101.md",
    )
    assert res["ok"] is True
    assert res["status"] == "backlog"

    claimed = ps.claim_one()
    assert claimed is not None
    assert claimed["ok"] is True
    assert claimed["job_id"] == "job-101"
    assert claimed["status"] == "admitted"
    assert claimed["provider_partition"] == "gemini_flash"
    assert claimed["path_lease"] == "/repo/src/module_a.py"
    assert claimed["expected_max"] == 25000
    assert claimed["packet_path"] == "/tmp/packet101.md"
    reservation_id = claimed["budget_reservation_id"]
    assert reservation_id.startswith("rsv-")

    # Verify database state in leases, reservations, and jobs
    with ps._connect() as conn:
        lease_row = conn.execute("SELECT * FROM leases WHERE path_glob=?", ("/repo/src/module_a.py",)).fetchone()
        assert lease_row is not None
        assert lease_row["job_id"] == "job-101"
        assert lease_row["held_until"] > 0

        rsv_row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        assert rsv_row is not None
        assert rsv_row["job_id"] == "job-101"
        assert rsv_row["tokens"] == 25000
        assert rsv_row["provider_partition"] == "gemini_flash"
        assert rsv_row["expires_at"] > 0

        job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", ("job-101",)).fetchone()
        assert job_row is not None
        assert job_row["status"] == "admitted"
        assert job_row["budget_reservation_id"] == reservation_id
        assert job_row["blocker"] is None

    # Transition to in_flight
    ps.mark_in_flight("job-101")
    with ps._connect() as conn:
        job_row = conn.execute("SELECT status FROM jobs WHERE job_id=?", ("job-101",)).fetchone()
        assert job_row["status"] == "in_flight"


def test_path_lease_mutual_exclusion(isolated_ps: dict[str, Any]) -> None:
    """Precedence/negative contract: two jobs with the same path_lease cannot both be admitted."""
    ps.enqueue(
        job_id="job-first",
        provider_partition="gemini_flash",
        path_lease="/repo/shared_pkg/*",
        expected_max=10000,
    )
    ps.enqueue(
        job_id="job-second",
        provider_partition="gemini_flash",
        path_lease="/repo/shared_pkg/*",
        expected_max=15000,
    )

    claimed_first = ps.claim_one()
    assert claimed_first is not None
    assert claimed_first["job_id"] == "job-first"

    # Second job must be blocked by the existing lease
    claimed_second = ps.claim_one()
    assert claimed_second is None

    with ps._connect() as conn:
        row2 = conn.execute("SELECT status, blocker FROM jobs WHERE job_id=?", ("job-second",)).fetchone()
        assert row2["status"] == "backlog"
        assert row2["blocker"] == "lease"

    # When first job completes and is released, second job can now be admitted
    ps.release_at_checking_complete("job-first", final_status="sealed")

    claimed_second = ps.claim_one()
    assert claimed_second is not None
    assert claimed_second["job_id"] == "job-second"
    assert claimed_second["status"] == "admitted"


def test_in_flight_max_capacity_boundary(isolated_ps: dict[str, Any]) -> None:
    """Boundary: slot count must strictly obey in_flight_max in policy."""
    policy_path: Path = isolated_ps["policy"]
    policy_path.write_text(json.dumps({
        "in_flight_max": 2,
        "provider_partitions": {"gemini_flash": 5},
    }))

    for i in range(1, 4):
        ps.enqueue(
            job_id=f"slot-job-{i}",
            provider_partition="gemini_flash",
            path_lease=f"/repo/part_{i}",
            expected_max=10000,
        )

    c1 = ps.claim_one()
    assert c1 is not None and c1["job_id"] == "slot-job-1"
    c2 = ps.claim_one()
    assert c2 is not None and c2["job_id"] == "slot-job-2"

    # Capacity reached (2 active slots) -> claim_one returns None
    c3 = ps.claim_one()
    assert c3 is None

    st = ps.status()
    assert st["slot_count"] == 2
    assert st["in_flight_max"] == 2

    # Free one slot
    ps.release_at_checking_complete("slot-job-1", final_status="sealed")
    c3_retry = ps.claim_one()
    assert c3_retry is not None
    assert c3_retry["job_id"] == "slot-job-3"


def test_provider_partition_limit_and_closed_partition(isolated_ps: dict[str, Any]) -> None:
    """Negative/boundary contract: partition concurrency limits and closed partition rejections."""
    policy_path: Path = isolated_ps["policy"]
    policy_path.write_text(json.dumps({
        "in_flight_max": 10,
        "provider_partitions": {
            "gemini_flash": 6,
            "ollama_cloud": 1,
            "zero_limit_part": 0,
            "chatgpt_astra": 0,
        },
    }))

    # 1. Closed partition rejection at enqueue
    closed_enq = ps.enqueue(
        job_id="astra-job",
        provider_partition="chatgpt_astra",
        path_lease="/repo/path1",
        expected_max=10000,
    )
    assert closed_enq["ok"] is False
    assert closed_enq["error"] == "partition_closed"

    # 2. Partition capacity limit
    ps.enqueue(
        job_id="ollama-1",
        provider_partition="ollama_cloud",
        path_lease="/repo/p1",
        expected_max=5000,
    )
    ps.enqueue(
        job_id="ollama-2",
        provider_partition="ollama_cloud",
        path_lease="/repo/p2",
        expected_max=5000,
    )

    claimed1 = ps.claim_one()
    assert claimed1 is not None and claimed1["job_id"] == "ollama-1"

    # ollama-2 blocked by partition_full
    claimed2 = ps.claim_one()
    assert claimed2 is None

    with ps._connect() as conn:
        row = conn.execute("SELECT blocker FROM jobs WHERE job_id=?", ("ollama-2",)).fetchone()
        assert row["blocker"] == "partition_full"

    # 3. Partition with 0 limit configured in policy but allowed at enqueue
    ps.enqueue(
        job_id="zero-job",
        provider_partition="zero_limit_part",
        path_lease="/repo/p3",
        expected_max=5000,
    )
    assert ps.claim_one() is None
    with ps._connect() as conn:
        row = conn.execute("SELECT blocker FROM jobs WHERE job_id=?", ("zero-job",)).fetchone()
        assert row["blocker"] == "partition"


def test_dependency_gating_blocks_until_sealed(isolated_ps: dict[str, Any]) -> None:
    """State transition contract: dependent job stays blocked until prerequisite status is sealed."""
    ps.enqueue(
        job_id="dep-parent",
        provider_partition="gemini_flash",
        path_lease="/repo/parent",
        expected_max=10000,
    )
    ps.enqueue(
        job_id="dep-child",
        provider_partition="gemini_flash",
        path_lease="/repo/child",
        expected_max=10000,
        depends_on=["dep-parent"],
    )

    # Initially dep-parent is in backlog, so dep-child cannot be claimed
    c1 = ps.claim_one()
    assert c1 is not None and c1["job_id"] == "dep-parent"

    # dep-child is checked and blocked because parent is 'admitted', not 'sealed'
    assert ps.claim_one() is None
    with ps._connect() as conn:
        child_row = conn.execute("SELECT blocker FROM jobs WHERE job_id=?", ("dep-child",)).fetchone()
        assert child_row["blocker"] == "dep:dep-parent"

    # Parent fails -> dep-child still cannot be claimed
    ps.release_at_checking_complete("dep-parent", final_status="failed")
    assert ps.claim_one() is None
    with ps._connect() as conn:
        child_row = conn.execute("SELECT blocker FROM jobs WHERE job_id=?", ("dep-child",)).fetchone()
        assert child_row["blocker"] == "dep:dep-parent"

    # Parent marked sealed -> dep-child can now be claimed
    with ps._connect() as conn:
        conn.execute("UPDATE jobs SET status='sealed' WHERE job_id='dep-parent'")
    c_child = ps.claim_one()
    assert c_child is not None
    assert c_child["job_id"] == "dep-child"
    assert c_child["status"] == "admitted"


def test_release_at_checking_complete_frees_resources(isolated_ps: dict[str, Any]) -> None:
    """External contract: release removes leases and reservations, and sets final terminal status."""
    ps.enqueue(
        job_id="rel-1",
        provider_partition="gemini_flash",
        path_lease="/repo/rel_file.py",
        expected_max=12000,
    )
    claim = ps.claim_one()
    assert claim is not None
    rsv_id = claim["budget_reservation_id"]

    # Verify tables have rows
    with ps._connect() as conn:
        assert conn.execute("SELECT COUNT(*) as n FROM leases WHERE job_id='rel-1'").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) as n FROM reservations WHERE reservation_id=?", (rsv_id,)).fetchone()["n"] == 1

    ps.release_at_checking_complete("rel-1", final_status="failed")

    with ps._connect() as conn:
        assert conn.execute("SELECT COUNT(*) as n FROM leases WHERE job_id='rel-1'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) as n FROM reservations WHERE reservation_id=?", (rsv_id,)).fetchone()["n"] == 0
        job_row = conn.execute("SELECT status, blocker FROM jobs WHERE job_id='rel-1'").fetchone()
        assert job_row["status"] == "failed"
        assert job_row["blocker"] is None


def test_enqueue_deduplication(isolated_ps: dict[str, Any]) -> None:
    """Transformation/idempotence: enqueueing the same job_id is deduped and preserves status."""
    first = ps.enqueue(
        job_id="dedup-job",
        provider_partition="gemini_flash",
        path_lease="/repo/dedup",
        expected_max=15000,
    )
    assert first["ok"] is True
    assert first["deduped"] is False
    assert first["status"] == "backlog"

    second = ps.enqueue(
        job_id="dedup-job",
        provider_partition="gemini_flash",
        path_lease="/repo/dedup_changed_path",
        expected_max=99999,
    )
    assert second["ok"] is True
    assert second["deduped"] is True
    assert second["status"] == "backlog"

    with ps._connect() as conn:
        count = conn.execute("SELECT COUNT(*) as n FROM jobs WHERE job_id='dedup-job'").fetchone()["n"]
        assert count == 1


def test_tick_admit_loop_and_heartbeat(isolated_ps: dict[str, Any]) -> None:
    """Contract: tick admits multiple eligible jobs and updates heartbeat record."""
    policy_path: Path = isolated_ps["policy"]
    policy_path.write_text(json.dumps({
        "in_flight_max": 2,
        "provider_partitions": {"gemini_flash": 5},
    }))

    for i in range(1, 4):
        ps.enqueue(
            job_id=f"tick-job-{i}",
            provider_partition="gemini_flash",
            path_lease=f"/repo/tick_{i}",
            expected_max=10000,
        )

    tick_res = ps.tick()
    assert tick_res["ok"] is True
    assert len(tick_res["admitted"]) == 2
    assert {a["job_id"] for a in tick_res["admitted"]} == {"tick-job-1", "tick-job-2"}

    # Verify heartbeat table
    with ps._connect() as conn:
        hb = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
        assert hb is not None
        assert hb["in_flight"] == 2
        assert hb["admittable_backlog"] == 1

    # Verify live_slots_open = False halts tick admissions and zeroes heartbeat
    policy_path.write_text(json.dumps({
        "live_slots_open": False,
        "in_flight_max": 2,
        "provider_partitions": {"gemini_flash": 5},
    }))
    halted_tick = ps.tick()
    assert halted_tick["ok"] is False
    assert halted_tick["error"] == "live_slots_not_open"
    assert halted_tick["admitted"] == []

    with ps._connect() as conn:
        hb = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
        assert hb["in_flight"] == 0
        assert hb["admittable_backlog"] == 0


def test_parallel_admit_cli_integration(isolated_ps: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    """CLI contract: parallel-admit subcommands init, enqueue, tick, and status."""
    # 1. init
    rc_init = main(["parallel-admit", "init"])
    assert rc_init == 0
    out_init = json.loads(capsys.readouterr().out)
    assert out_init["ok"] is True

    # 2. enqueue missing required args
    rc_bad_enq = main(["parallel-admit", "enqueue", "--job-id", "cli-job"])
    assert rc_bad_enq == 2
    out_bad = json.loads(capsys.readouterr().out)
    assert out_bad["ok"] is False

    # 3. enqueue valid
    rc_enq = main([
        "parallel-admit", "enqueue",
        "--job-id", "cli-job-1",
        "--path-lease", "/repo/cli_file",
        "--partition", "gemini_flash",
        "--expected-max", "20000",
    ])
    assert rc_enq == 0
    out_enq = json.loads(capsys.readouterr().out)
    assert out_enq["ok"] is True
    assert out_enq["job_id"] == "cli-job-1"

    # 4. tick
    rc_tick = main(["parallel-admit", "tick"])
    assert rc_tick == 0
    out_tick = json.loads(capsys.readouterr().out)
    assert out_tick["ok"] is True
    assert len(out_tick["admitted"]) == 1
    assert out_tick["admitted"][0]["job_id"] == "cli-job-1"

    # 5. status
    rc_status = main(["parallel-admit", "status"])
    assert rc_status == 0
    out_status = json.loads(capsys.readouterr().out)
    assert out_status["ok"] is True
    assert out_status["slot_count"] == 1
    assert out_status["by_status"]["admitted"] == 1
