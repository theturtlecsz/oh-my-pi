"""Postgres tests for implement, evaluate, freeze, and push (OMP-417-s07-s05).

The candidate ref on the remote equals the frozen commit, and one push receipt
is stored. A crash after append_evidence leaves that one update and receipt.
Evaluate reports identity worker. A test command that writes outside the
worktree fails and leaves the host file absent. A failing test opens a repair
round whose new worktree is at base_commit. A second freeze is stale_evidence
and does not add a candidate. An out-of-envelope worker fails freeze with no
push. A new scope pauses d21-new-scope. A killed worker crashes, and the retry
resets the same worktree and succeeds. A push the standing policy does not
cover is not allowed.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.orchestrator import operations, service
from omp_work.orchestrator.service import OrchestratorHandler, submit
from omp_work.v1.models import EvidenceKind
from orchestrator_e2e_support import (
    SimulatedCrash,
    crash_after_command,
    kill_first_run,
    open_e2e_world,
    open_worker,
    remote_updates,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_ESCAPE = Path("/usr/omp-417-s07-s05-escape")
_ESCAPE_COMMAND = (
    "/usr/bin/python3",
    "-c",
    "import pathlib, sys\n"
    "target = pathlib.Path('/usr/omp-417-s07-s05-escape')\n"
    "try:\n"
    "    target.write_text('escape')\n"
    "except OSError:\n"
    "    sys.exit(1)\n"
    "sys.exit(0)\n",
)
_FAIL_COMMAND = ("/usr/bin/python3", "-c", "import sys; sys.exit(1)")


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore service._REGISTRY after each test."""
    saved = dict(service._REGISTRY)
    try:
        yield
    finally:
        service._REGISTRY.clear()
        service._REGISTRY.update(saved)


def _seed_item_budget(world, service_fixture, work_id: UUID) -> UUID:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)
    return revision_id


def _expire_lease(world) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def _steps(world) -> list[dict]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _outcomes(world, stage: str) -> list[dict]:
    return [
        step
        for step in _steps(world)
        if step.get("stage") == stage and step.get("kind") == "outcome"
    ]


def _open(service_fixture, root: Path, monkeypatch, **kwargs):
    world = open_e2e_world(
        service_fixture,
        root,
        monkeypatch,
        verifier_mode="pass",
        worker_mode=kwargs.get("worker_mode", "ok"),
        extra_capability=None,
        test_command=kwargs.get("test_command"),
        operations=("omp_work.orchestrator.operations:register",),
    )
    if kwargs.get("candidate_ref"):
        world.request = dataclasses.replace(
            world.request, candidate_ref=kwargs["candidate_ref"]
        )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service_fixture, work_id)
    return world, open_worker(world), work_id


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _rows(world, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _candidates(world, work_id: UUID) -> list[dict]:
    return _rows(
        world,
        """
        SELECT candidate_id::text AS candidate_id, kind, commit_sha, candidate_sha256
        FROM omp_work.candidates
        WHERE workspace_id = %s AND work_id = %s
        ORDER BY allocated_at, candidate_id
        """,
        (world.workspace_id, work_id),
    )


def _push_receipts(world, work_id: UUID) -> list[dict]:
    return _rows(
        world,
        """
        SELECT receipt_id::text AS receipt_id, kind, payload, candidate_commit,
               remote_ref, remote_commit, issuer, issued_at
        FROM omp_evidence.receipts
        WHERE workspace_id = %s AND work_id = %s AND kind = %s
        ORDER BY issued_at, receipt_id
        """,
        (world.workspace_id, work_id, EvidenceKind.PUSH.value),
    )


def _payload(value) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    raise AssertionError(f"receipt payload is {type(value).__name__}")


def _patch_second_freeze(monkeypatch) -> dict:
    """Run freeze once for the pipeline, then once more on a new step index."""
    holder: dict = {}
    real = operations.run_freeze

    def wrapping(ctx):
        first = real(ctx)
        if first.outcome != "succeeded":
            holder["second"] = first
            return first
        saved = ctx.step_index
        ctx.step_index = saved + 50
        try:
            holder["second"] = real(ctx)
        finally:
            ctx.step_index = saved
        return first

    monkeypatch.setattr(operations, "run_freeze", wrapping)
    return holder


def test_push_receipt_matches_frozen_commit_and_second_freeze_is_stale(
    service, tmp_path: Path, monkeypatch
) -> None:
    """The remote tip is the frozen commit, one receipt is stored, and a second freeze is stale."""
    holder = _patch_second_freeze(monkeypatch)
    world, worker, work_id = _open(service, tmp_path / "push", monkeypatch)

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)

    frozen = _outcomes(world, "freeze")[-1]
    commit = frozen["data"]["candidate_commit"]
    assert _git(world.remote_repo, "rev-parse", world.candidate_ref) == commit
    assert remote_updates(world, world.candidate_ref) == 1

    receipts = _push_receipts(world, work_id)
    assert len(receipts) == 1
    body = _payload(receipts[0]["payload"])
    assert body["remote_url"] == str(world.remote_repo)
    assert body["refs"] == [world.candidate_ref]
    assert body["tips"] == [commit]
    assert body["issued_at"]
    assert receipts[0]["remote_ref"] == world.candidate_ref
    assert receipts[0]["remote_commit"] == commit
    assert receipts[0]["issuer"] == "orchestrator"

    push = _outcomes(world, "push")[-1]
    kinds = {
        step.get("kind")
        for step in _steps(world)
        if step.get("step_index") == push["step_index"]
    }
    assert {"action_allowed", "external_intent", "external_done"} <= kinds
    assert push["action"]["allowed"] is True

    evaluated = _outcomes(world, "evaluate")[-1]["data"]
    assert evaluated["identity"] == "worker"
    assert evaluated["exit_code"] == 0
    assert evaluated["command"]
    assert evaluated["finished_at"]

    second = holder["second"]
    assert second.outcome == "failed"
    assert second.data["code"] == "stale_evidence"
    finals = [row for row in _candidates(world, work_id) if row["kind"] == "final"]
    assert len(finals) == 1
    assert finals[0]["commit_sha"] == commit


def test_crash_after_append_evidence_pushes_once(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A crash after the push receipt still leaves one remote update and one receipt."""
    world, worker, work_id = _open(service, tmp_path / "crash-push", monkeypatch)

    def frozen(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "freeze"))

    tick_until(worker, world, frozen, limit=40)
    crash_after_command(monkeypatch, "append_evidence")
    with pytest.raises(SimulatedCrash):
        worker.tick()
    _expire_lease(world)

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=10)
    commit = _outcomes(world, "freeze")[-1]["data"]["candidate_commit"]
    assert _git(world.remote_repo, "rev-parse", world.candidate_ref) == commit
    assert remote_updates(world, world.candidate_ref) == 1
    assert len(_push_receipts(world, work_id)) == 1


def test_evaluate_host_write_fails_and_file_stays_absent(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A test command that writes a host path outside the worktree fails, and the file is absent."""
    world, worker, _work_id = _open(
        service,
        tmp_path / "escape",
        monkeypatch,
        test_command=_ESCAPE_COMMAND,
    )
    try:
        def evaluated(_view: dict) -> bool:
            return bool(_outcomes(world, "evaluate"))

        tick_until(worker, world, evaluated, limit=40)
        outcome = _outcomes(world, "evaluate")[-1]
        assert outcome["outcome"] == "failed"
        assert outcome["data"]["identity"] == "worker"
        assert outcome["data"]["exit_code"] != 0
        assert not _ESCAPE.exists()
    finally:
        if _ESCAPE.exists():
            _ESCAPE.unlink()


def test_failing_test_opens_repair_worktree_at_base(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A failing test records repair-round and resets a new worktree at base_commit."""
    world, worker, _work_id = _open(
        service,
        tmp_path / "repair",
        monkeypatch,
        test_command=_FAIL_COMMAND,
    )

    def repaired(_view: dict) -> bool:
        steps = _steps(world)
        repairs = [
            step
            for step in steps
            if step.get("kind") == "repair" and step.get("rule_id") == "repair-round"
        ]
        if not repairs:
            return False
        index = int(repairs[0]["step_index"])
        return any(
            step.get("stage") == "implement"
            and step.get("kind") == "outcome"
            and int(step["step_index"]) > index
            for step in steps
        )

    tick_until(worker, world, repaired, limit=40)
    steps = _steps(world)
    repair = next(
        step
        for step in steps
        if step.get("kind") == "repair" and step.get("rule_id") == "repair-round"
    )
    assert repair["next_stage"] == "implement"
    first = next(
        step
        for step in steps
        if step.get("stage") == "implement" and step.get("kind") == "outcome"
    )
    second = next(
        step
        for step in steps
        if step.get("stage") == "implement"
        and step.get("kind") == "outcome"
        and int(step["step_index"]) > int(repair["step_index"])
    )
    first_tree = Path(first["data"]["worktree"])
    second_tree = Path(second["data"]["worktree"])
    assert first_tree != second_tree
    assert second_tree == world.worktrees_dir / f"{world.mission_id}-{second['step_index']}"
    assert _git(second_tree, "rev-parse", "HEAD") == world.base_commit


def test_outside_worker_fails_freeze_without_push(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A worker that writes outside the envelope fails freeze and does not push."""
    world, worker, work_id = _open(
        service, tmp_path / "outside", monkeypatch, worker_mode="outside"
    )

    def froze(_view: dict) -> bool:
        return bool(_outcomes(world, "freeze"))

    tick_until(worker, world, froze, limit=40)
    frozen = _outcomes(world, "freeze")[-1]
    assert frozen["outcome"] == "failed"
    assert "outside_allowed:outside.txt" in frozen["data"]["codes"]
    assert _outcomes(world, "push") == []
    assert _push_receipts(world, work_id) == []
    assert remote_updates(world, world.candidate_ref) == 0


def test_new_scope_pauses_before_evaluate(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A new-scope file is removed and the mission pauses on d21-new-scope."""
    world, worker, _work_id = _open(
        service, tmp_path / "scope", monkeypatch, worker_mode="new_scope"
    )

    def paused(view: dict) -> bool:
        return any(
            step.get("kind") == "pause" and step.get("rule_id") == "d21-new-scope"
            for step in view["steps"]
        )

    tick_until(worker, world, paused, limit=30)
    implemented = _outcomes(world, "implement")[-1]
    assert implemented["outcome"] == "succeeded"
    assert implemented["new_scope"] is True
    scope = Path(implemented["data"]["worktree"]) / ".omp" / "new-scope.json"
    assert not scope.exists()
    assert _outcomes(world, "evaluate") == []


def test_killed_worker_retries_on_the_same_worktree(
    service, tmp_path: Path, monkeypatch
) -> None:
    """kill_first_run crashes the worker. The retry resets that worktree and succeeds."""
    kill_first_run(monkeypatch, "worker")
    world, worker, _work_id = _open(service, tmp_path / "kill", monkeypatch)

    def retried(_view: dict) -> bool:
        outcomes = _outcomes(world, "implement")
        crashed = [step for step in outcomes if step.get("outcome") == "crashed"]
        succeeded = [step for step in outcomes if step.get("outcome") == "succeeded"]
        retried_step = [
            step
            for step in _steps(world)
            if step.get("kind") == "retry" and step.get("next_stage") == "implement"
        ]
        return bool(crashed and succeeded and retried_step)

    tick_until(worker, world, retried, limit=40)
    outcomes = _outcomes(world, "implement")
    crashed = next(step for step in outcomes if step.get("outcome") == "crashed")
    succeeded = next(step for step in outcomes if step.get("outcome") == "succeeded")
    tree = Path(succeeded["data"]["worktree"])
    assert tree == Path(crashed["data"]["worktree"])
    assert tree == world.worktrees_dir / f"{world.mission_id}-{crashed['step_index']}"
    assert _git(tree, "rev-parse", "HEAD") == world.base_commit
    assert (tree / "feature.txt").is_file()


def test_uncovered_push_is_not_allowed(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A push of a protected ref is held and records allowed False."""
    world, worker, work_id = _open(
        service,
        tmp_path / "held",
        monkeypatch,
        candidate_ref="refs/heads/main",
    )

    def pushed(_view: dict) -> bool:
        return bool(_outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)
    outcome = _outcomes(world, "push")[-1]
    assert outcome["outcome"] == "failed"
    assert outcome["action"]["allowed"] is False
    assert _push_receipts(world, work_id) == []
    assert remote_updates(world, "refs/heads/main") == 1
