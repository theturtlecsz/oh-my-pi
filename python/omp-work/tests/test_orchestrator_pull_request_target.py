"""Runner tests for pull request policy and target scope (OMP-518-s07)."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from github_fake import FakeGitHub
from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.service import submit
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.decision_records import find_decision
from orchestrator_e2e_support import (
    E2EWorld,
    _owner,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    sign_merge_answer,
    tick_until,
)
from psycopg.rows import dict_row
from test_orchestrator_merge_stage import _answer, _steps
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_OPERATIONS = (
    "orchestrator_test_ops:register",
    "orchestrator_pr_ops:register",
    "omp_work.orchestrator.merge_stage:register",
)


def _setup_world(service: Any, tmp_path: Path, monkeypatch: Any) -> tuple[E2EWorld, FakeGitHub]:
    world = open_e2e_world(
        service,
        tmp_path,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=_OPERATIONS,
    )

    owner_path = world.config.automation_capability_path.parent / "owner.json"
    owner_path.write_text(
        json.dumps(
            {
                "actor_id": str(OWNER),
                "actor_kind": "owner",
                "workspaces": [str(world.workspace_id)],
                "scopes": ["work.read", "work.mutate", "work.approve", "work.close", "work.execute"],
            }
        ),
        encoding="utf-8",
    )

    read_token_path = tmp_path / "github_read_token"
    read_token_path.write_text("github-read-token\n", encoding="utf-8")
    merge_token_path = tmp_path / "github_merge_token"
    merge_token_path.write_text("github-merge-token\n", encoding="utf-8")
    merge_token = "github-merge-token"

    fake = FakeGitHub(
        world.remote_repo,
        "owner/repo",
        tokens={"github-read-token", merge_token},
    )

    world.config = dataclasses.replace(
        world.config,
        github_api_url=fake.url,
        github_repositories={world.repo_key: "owner/repo"},
        github_token_path=read_token_path,
        merge_credential_path=merge_token_path,
        required_checks=("ci",),
    )

    return world, fake


def _seed_pull_policy(world: E2EWorld) -> None:
    decision_id = uuid4()
    world.store.put_standing_policy(
        world.workspace_id,
        OWNER,
        world.project_id,
        StandingPolicy(
            policy_id=uuid4(),
            action_class="create_pull_request",
            repositories=(world.repo_key,),
            branch_patterns=("refs/heads/omp/*",),
            decision_id=decision_id,
        ),
        _owner(decision_id),
    )


def _start(service: Any, world: E2EWorld, fake: FakeGitHub) -> Any:
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )
    worker = open_worker(world)
    tick_until(
        worker,
        world,
        lambda _view: any(
            step.get("stage") == "push" and step.get("kind") == "outcome"
            for step in _steps(world)
        ),
    )
    push = next(
        step for step in _steps(world)
        if step.get("stage") == "push" and step.get("kind") == "outcome"
    )
    fake.set_check(push["data"]["candidate_commit"], "ci", "completed", "success")
    return worker


def _pause(
    worker: Any, world: E2EWorld, rule_id: str, *, after: str | None = None
) -> dict[str, Any]:
    def new_pause(view: dict[str, Any]) -> bool:
        pauses = [step for step in view["steps"] if step.get("kind") == "pause"]
        return (
            view["status"] == "paused"
            and bool(pauses)
            and str(pauses[-1].get("decision_id")) != after
        )

    view = tick_until(worker, world, new_pause)
    pause = next(step for step in reversed(view["steps"]) if step.get("kind") == "pause")
    assert pause["rule_id"] == rule_id
    return pause


def test_pull_opens_only_after_standing_policy_and_resume(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Without policy, the runner must pause before it creates a pull."""
    world, fake = _setup_world(service, tmp_path, monkeypatch)
    try:
        worker = _start(service, world, fake)
        pause = _pause(worker, world, "d35-tier2-no-policy")
        assert not any(call[0] == "POST" for call in fake.calls)

        _seed_pull_policy(world)
        _answer(service, world.workspace_id, str(pause["decision_id"]), "resume")
        _pause(worker, world, "d41-merge-approval", after=str(pause["decision_id"]))
        assert len([call for call in fake.calls if call[0] == "POST"]) == 1
        assert not any(call[0] == "PUT" for call in fake.calls)
    finally:
        fake.close()


def test_unmapped_repository_pauses_without_github_calls(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A missing repository mapping must not select another configured repository."""
    world, fake = _setup_world(service, tmp_path, monkeypatch)
    try:
        _seed_pull_policy(world)
        world.config = dataclasses.replace(
            world.config, github_repositories={"other-repo": fake.repository}
        )
        worker = _start(service, world, fake)
        _pause(worker, world, "unrecoverable-blocker")
        outcome = next(
            step for step in reversed(_steps(world))
            if step.get("stage") == "merge" and step.get("kind") == "outcome"
        )
        assert outcome["blocker"] == "pull_request_config_missing"
        assert fake.calls == []
        assert git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip() == world.base_commit
    finally:
        fake.close()


def test_signed_approval_cannot_merge_retargeted_pull(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A signed approval for main must not merge a pull after its base changes."""
    world, fake = _setup_world(service, tmp_path, monkeypatch)
    try:
        _seed_pull_policy(world)
        worker = _start(service, world, fake)
        pause = _pause(worker, world, "d41-merge-approval")
        outcome = next(
            step for step in reversed(_steps(world))
            if step.get("stage") == "merge" and step.get("kind") == "outcome"
        )
        number = outcome["data"]["pull_number"]
        assert len([call for call in fake.calls if call[0] == "POST"]) == 1

        git(
            world.control_repo, "push", str(world.remote_repo),
            f"{world.base_commit}:refs/heads/other",
        )
        fake.retarget(number, "other")
        calls_before_approval = len(fake.calls)
        decision_id = UUID(str(pause["decision_id"]))
        with psycopg.connect(
            **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn, conn.cursor() as cur:
            record = find_decision(cur, world.workspace_id, decision_id)
            assert record is not None
            target_sha = str(record["target_sha256"])
        signature = sign_merge_answer(
            world.owner_key_path,
            workspace_id=world.workspace_id,
            decision_id=decision_id,
            action_class="merge_protected_branch",
            answer="approve",
            target_sha256=target_sha,
        )
        _answer(
            service,
            world.workspace_id,
            str(decision_id),
            "approve",
            owner_signature=signature,
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
        )
        _pause(worker, world, "unrecoverable-blocker", after=str(decision_id))
        outcome = next(
            step for step in reversed(_steps(world))
            if step.get("stage") == "merge" and step.get("kind") == "outcome"
        )
        assert outcome["blocker"] == "target_mismatch"
        assert any(
            method == "GET" and path == f"/repos/{fake.repository}/pulls/{number}"
            for method, path, _token in fake.calls[calls_before_approval:]
        )
        assert len([call for call in fake.calls if call[0] == "POST"]) == 1
        assert not any(call[0] == "PUT" for call in fake.calls)
        for branch in ("main", "other"):
            assert git(world.remote_repo, "rev-parse", f"refs/heads/{branch}").decode("ascii").strip() == world.base_commit
    finally:
        fake.close()
