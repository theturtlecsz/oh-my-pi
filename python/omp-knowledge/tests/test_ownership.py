from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

tests_dir = Path(__file__).resolve().parent
repo_root = tests_dir.parent
src_dir = repo_root / "src"

for p in (str(src_dir), str(repo_root), str(tests_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.ownership import WriterOwnership, WriterOwnershipLost


@pytest.mark.asyncio
async def test_owner_refused_and_release_permits_next(pg_cluster: KnowledgeConfig):
    owner1 = WriterOwnership(pg_cluster)
    assert owner1.acquire() is True
    assert owner1.backend_pid is not None
    owner1.ensure_alive()

    # Second owner on the same backend is refused
    owner2 = WriterOwnership(pg_cluster)
    assert owner2.acquire() is False

    # Releasing owner1 permits next owner to acquire
    await owner1.release()

    owner3 = WriterOwnership(pg_cluster)
    assert owner3.acquire() is True
    assert owner3.backend_pid is not None
    owner3.ensure_alive()
    await owner3.release()


@pytest.mark.asyncio
async def test_duplicate_acquire_same_instance_lifecycle(pg_cluster: KnowledgeConfig):
    owner = WriterOwnership(pg_cluster)
    assert owner.acquire() is True
    # Duplicate acquire on same alive instance returns True
    assert owner.acquire() is True

    await owner.release()
    # Re-acquire on released instance is forbidden
    with pytest.raises(RuntimeError):
        owner.acquire()


@pytest.mark.asyncio
async def test_pg_session_termination_latches_dead_flock_blocks_until_release(
    pg_cluster: KnowledgeConfig,
):
    owner1 = WriterOwnership(pg_cluster)
    assert owner1.acquire() is True
    pid1 = owner1.backend_pid
    assert pid1 is not None

    # Terminate owner1's Postgres backend connection from a separate session
    with psycopg.connect(pg_cluster.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid1,))

    time.sleep(0.05)

    # Second owner CANNOT acquire while owner1 retains flock, even though PG session is gone
    owner2 = WriterOwnership(pg_cluster)
    assert owner2.acquire() is False

    # Owner1's ensure_alive fails and latches dead permanently
    with pytest.raises(WriterOwnershipLost):
        owner1.ensure_alive()

    with pytest.raises(WriterOwnershipLost):
        owner1.ensure_alive()

    # Once owner1 releases flock, second owner can acquire
    await owner1.release()

    owner3 = WriterOwnership(pg_cluster)
    assert owner3.acquire() is True
    owner3.ensure_alive()
    await owner3.release()


@pytest.mark.asyncio
async def test_release_waits_for_inflight_mutation(pg_cluster: KnowledgeConfig):
    owner = WriterOwnership(pg_cluster)
    assert owner.acquire() is True

    mutation_started = asyncio.Event()
    release_started = asyncio.Event()
    allow_mutation_exit = asyncio.Event()
    mutation_finished = False

    async def in_flight_mutation():
        nonlocal mutation_finished
        async with owner.mutation():
            mutation_started.set()
            await allow_mutation_exit.wait()
            mutation_finished = True

    mutation_task = asyncio.create_task(in_flight_mutation())
    await mutation_started.wait()

    async def run_release():
        release_started.set()
        await owner.release()

    release_task = asyncio.create_task(run_release())
    await release_started.wait()

    # Yield execution to let release attempt acquiring the lock
    for _ in range(5):
        await asyncio.sleep(0.001)

    assert not release_task.done()
    assert not mutation_finished

    allow_mutation_exit.set()
    await mutation_task
    assert mutation_finished is True

    await release_task
    assert release_task.done()


@pytest.mark.asyncio
async def test_mutation_fails_if_ownership_lost(pg_cluster: KnowledgeConfig):
    owner = WriterOwnership(pg_cluster)
    assert owner.acquire() is True
    pid = owner.backend_pid
    assert pid is not None

    with psycopg.connect(pg_cluster.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.05)

    with pytest.raises(WriterOwnershipLost):
        async with owner.mutation():
            pass

    await owner.release()


def test_recovery_requires_ownership_and_stamps_running(pg_cluster: KnowledgeConfig):
    import uuid

    owner = WriterOwnership(pg_cluster)

    # Not owned -> refused
    with pytest.raises(WriterOwnershipLost):
        owner.recover_interrupted_jobs()

    assert owner.acquire() is True

    repo_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    workspace_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    op_running = uuid.UUID("00000000-0000-0000-0000-000000000010")
    op_completed = uuid.UUID("00000000-0000-0000-0000-000000000020")
    req_sha = "a" * 64

    with psycopg.connect(pg_cluster.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_knowledge.repositories (repository_id, state)
                VALUES (%s, 'unbound')
                ON CONFLICT (repository_id) DO NOTHING
                """,
                (repo_id,),
            )
            cur.execute(
                "DELETE FROM omp_knowledge.ingestion_jobs WHERE operation_id IN (%s, %s)",
                (op_running, op_completed),
            )
            cur.execute(
                """
                INSERT INTO omp_knowledge.ingestion_jobs (
                    operation_id, workspace_id, repository_id, request_sha256, state
                ) VALUES
                    (%s, %s, %s, %s, 'running'),
                    (%s, %s, %s, %s, 'completed')
                """,
                (op_running, workspace_id, repo_id, req_sha, op_completed, workspace_id, repo_id, req_sha),
            )

    count = owner.recover_interrupted_jobs()
    assert count >= 1

    with psycopg.connect(pg_cluster.pg_connection_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, diagnostics FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_running,),
            )
            running_row = cur.fetchone()
            assert running_row is not None
            assert running_row[0] == "interrupted"
            assert any("interrupted_by_recovery" in d for d in running_row[1])

            cur.execute(
                "SELECT state FROM omp_knowledge.ingestion_jobs WHERE operation_id = %s",
                (op_completed,),
            )
            completed_row = cur.fetchone()
            assert completed_row is not None
            assert completed_row[0] == "completed"

    asyncio.run(owner.release())

    with pytest.raises(WriterOwnershipLost):
        owner.recover_interrupted_jobs()


def test_child_process_death_takeover(pg_cluster: KnowledgeConfig):
    child_script = """
import sys, time
from pathlib import Path
from omp_knowledge.config import KnowledgeConfig
from omp_knowledge.ownership import WriterOwnership

cfg = KnowledgeConfig(
    pg_host=sys.argv[1],
    pg_port=int(sys.argv[2]),
    pg_database=sys.argv[3],
    pg_user=sys.argv[4],
    pg_password=sys.argv[5],
    state_dir=Path(sys.argv[6]),
    config_dir=Path(sys.argv[7]),
)
owner = WriterOwnership(cfg)
if not owner.acquire():
    sys.stderr.write("ACQUIRE_FAILED\\n")
    sys.exit(1)

sys.stdout.write("READY\\n")
sys.stdout.flush()

try:
    while True:
        time.sleep(1)
except BaseException:
    sys.exit(0)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(src_dir), str(repo_root)] + sys.path)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_script,
            pg_cluster.pg_host,
            str(pg_cluster.pg_port),
            pg_cluster.pg_database,
            pg_cluster.pg_user,
            pg_cluster.pg_password,
            str(pg_cluster.state_dir),
            str(pg_cluster.config_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        ready_line = proc.stdout.readline().strip()
        assert ready_line == "READY", f"Child failed to start: {proc.stderr.read()}"

        # Parent attempts acquire -> refused
        parent_owner1 = WriterOwnership(pg_cluster)
        assert parent_owner1.acquire() is False

        # Kill child process with SIGKILL
        proc.kill()
        proc.wait(timeout=5)

        # Parent acquires after child termination
        parent_owner2 = WriterOwnership(pg_cluster)
        acquired = False
        for _ in range(20):
            if parent_owner2.acquire():
                acquired = True
                break
            time.sleep(0.1)

        assert acquired is True
        parent_owner2.ensure_alive()
        asyncio.run(parent_owner2.release())

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
