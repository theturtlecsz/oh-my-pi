"""Integration tests for the pull request runner: unknown outcome, crash, protected branch (OMP-518-s06)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from github_fake import FakeGitHub
from omp_work.orchestrator import candidate_git
from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.github_pulls import GitHubPulls, PullRequestError
from omp_work.orchestrator.service import submit
from omp_work.v1.decision_records import find_decision
from orchestrator_e2e_support import (
    E2EWorld,
    SimulatedCrash,
    open_worker,
    seed_published_budget,
    sign_merge_answer,
    tick_until,
)
from psycopg.rows import dict_row
from test_orchestrator_merge_stage import _answer, _setup_world, _steps
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _merge_commit(world: E2EWorld) -> str | None:
    for step in reversed(_steps(world)):
        if step.get("stage") == "merge" and step.get("kind") == "outcome":
            sha = step.get("data", {}).get("merge_commit_sha")
            if sha:
                return str(sha)
    return None


def _has_rule(rule_id: str):
    return lambda v: any(s.get("rule_id") == rule_id for s in v.get("steps", []))


def _approve_merge(service: Any, world: E2EWorld, fake: FakeGitHub, worker: Any) -> None:
    """Drive the world to the signed d41 approval, checks passing. No merge ticks run."""
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )

    view = tick_until(worker, world, _has_rule("retry-bound"))
    retry_pause = next(s for s in view["steps"] if s.get("rule_id") == "retry-bound")
    push_step = next(
        s for s in _steps(world) if s.get("stage") == "push" and s.get("kind") == "outcome"
    )
    fake.set_check(push_step["data"]["candidate_commit"], "ci", "completed", "success")
    _answer(service, world.workspace_id, str(retry_pause["decision_id"]), "resume")

    view = tick_until(worker, world, _has_rule("d41-merge-approval"))
    d41 = next(s for s in view["steps"] if s.get("rule_id") == "d41-merge-approval")
    decision_id = str(d41["decision_id"])
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        record = find_decision(cur, world.workspace_id, UUID(decision_id))
        assert record is not None
        target_sha = str(record["target_sha256"])
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=UUID(decision_id),
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=target_sha,
    )
    _answer(
        service,
        world.workspace_id,
        decision_id,
        "approve",
        owner_signature=signature,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
    )


def _expire_leases(world: E2EWorld) -> None:
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
            " WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def _merge_done(world: E2EWorld) -> bool:
    return _merge_commit(world) is not None


def _assert_recovered_once(world: E2EWorld, fake: FakeGitHub) -> None:
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    assert len([c for c in fake.calls if c[0] == "PUT"]) == 1
    merge_commit = _merge_commit(world)
    assert merge_commit is not None
    steps = _steps(world)
    assert any(s.get("rule_id") == "lease-recovery" for s in steps)
    dones = [s for s in steps if s.get("kind") == "external_done" and s.get("ref") == merge_commit]
    assert len(dones) == 1
    assert git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip() == merge_commit


def test_merge_timeout_recovers_without_second_merge(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    world, fake, _token = _setup_world(service, tmp_path, monkeypatch)
    try:
        worker = open_worker(world)
        _approve_merge(service, world, fake, worker)
        fake.drop_after_merge = True

        raised: PullRequestError | None = None
        for _ in range(120):
            try:
                worker.tick()
            except PullRequestError as exc:
                raised = exc
                break
        assert raised is not None and raised.code == "outcome_unknown"

        main = git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
        steps = _steps(world)
        intents = [s for s in steps if s.get("kind") == "external_intent"]
        dones = [s for s in steps if s.get("kind") == "external_done"]
        assert any(s.get("ref") == main for s in dones) is False
        assert len(intents) > len(dones)
        assert len([c for c in fake.calls if c[0] == "PUT"]) == 1

        _expire_leases(world)
        tick_until(worker, world, lambda _v: _merge_done(world))
        _assert_recovered_once(world, fake)
    finally:
        fake.close()


def test_merge_crash_after_put_recovers_without_second_merge(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    world, fake, _token = _setup_world(service, tmp_path, monkeypatch)
    try:
        worker = open_worker(world)
        _approve_merge(service, world, fake, worker)

        original = GitHubPulls.merge
        holder = {"done": False}

        def merge(self: GitHubPulls, number: int, sha: str, token_path: str | Path) -> str:
            result = original(self, number, sha, token_path)
            if not holder["done"]:
                holder["done"] = True
                raise SimulatedCrash("merge")
            return result

        monkeypatch.setattr(GitHubPulls, "merge", merge)

        with pytest.raises(SimulatedCrash):
            for _ in range(120):
                worker.tick()
        assert holder["done"]

        _expire_leases(world)
        tick_until(worker, world, lambda _v: _merge_done(world))
        _assert_recovered_once(world, fake)
    finally:
        fake.close()


def test_merge_goes_through_api_when_remote_protects_main(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    world, fake, _token = _setup_world(service, tmp_path, monkeypatch)
    try:
        hook_log = tmp_path / "update-hook.log"
        hook_log.write_text("", encoding="utf-8")
        update = world.remote_repo / "hooks" / "update"
        update.write_text(
            f"#!/bin/sh\necho \"$1\" >> '{hook_log}'\n"
            '[ "$1" = "refs/heads/main" ] && exit 1\nexit 0\n',
            encoding="utf-8",
        )
        update.chmod(0o755)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("merge must not push to the protected branch")

        monkeypatch.setattr(candidate_git, "merge", refuse)

        worker = open_worker(world)
        _approve_merge(service, world, fake, worker)
        tick_until(worker, world, lambda _v: _merge_done(world))

        refs = hook_log.read_text(encoding="utf-8").split()
        assert world.request.candidate_ref in refs
        assert "refs/heads/main" not in refs

        merge_commit = _merge_commit(world)
        main = git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
        assert main == merge_commit
        push_step = next(
            s for s in _steps(world) if s.get("stage") == "push" and s.get("kind") == "outcome"
        )
        parents = git(world.remote_repo, "rev-parse", f"{main}^2").decode("ascii").strip()
        assert parents == push_step["data"]["candidate_commit"]
    finally:
        fake.close()
