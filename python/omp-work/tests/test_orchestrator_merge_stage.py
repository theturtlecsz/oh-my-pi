"""Integration tests for the orchestrator merge stage (OMP-518-s05)."""

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
from omp_work.orchestrator.merge_stage import run_merge
from omp_work.orchestrator.service import OrchestratorHandler, submit
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
from test_workflow_service import OWNER, _command

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


def _answer(service: Any, workspace_id: UUID, decision_id: str, answer: str, **extra: Any) -> None:
    status_code, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {"decision_id": decision_id, "answer": answer, **extra},
        },
    )
    assert status_code == 200, body


def _setup_world(service: Any, tmp_path: Path, monkeypatch: Any) -> tuple[E2EWorld, FakeGitHub, str]:
    world = open_e2e_world(
        service,
        tmp_path,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=_OPERATIONS,
    )

    # Provide owner.json so submit populates project_missions
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

    # First world.store.put_standing_policy create_pull_request (world.repo_key, refs/heads/omp/*)
    pr_policy_decision = uuid4()
    world.store.put_standing_policy(
        world.workspace_id,
        OWNER,
        world.project_id,
        StandingPolicy(
            policy_id=uuid4(),
            action_class="create_pull_request",
            repositories=(world.repo_key,),
            branch_patterns=("refs/heads/omp/*",),
            decision_id=pr_policy_decision,
        ),
        _owner(pr_policy_decision),
    )

    return world, fake, merge_token


def _steps(world: E2EWorld) -> list[dict[str, Any]]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def test_merge_stage_pending_then_pass_and_signed_merge(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    world, fake, merge_token = _setup_world(service, tmp_path, monkeypatch)
    try:
        submitted = submit(world.config, world.request)
        work_id = UUID(str(submitted["work_id"]))
        tree = world.store.read(world.workspace_id, OWNER, "tree", "")
        item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
        revision_id = UUID(str(item["revision"]["revision_id"]))
        seed_published_budget(service, world.workspace_id, work_id, revision_id)

        worker = open_worker(world)

        # 1. Checks pending -> retry-bound, no PUT
        view = tick_until(
            worker,
            world,
            lambda v: any(s.get("rule_id") == "retry-bound" for s in v.get("steps", [])),
        )
        steps = view["steps"]
        retry_pause = next(s for s in steps if s.get("rule_id") == "retry-bound")
        assert retry_pause is not None

        # Verify no PUT call was made
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 0

        # Verify exactly one POST was made (to create PR)
        posts = [c for c in fake.calls if c[0] == "POST"]
        assert len(posts) == 1

        # 2. Pass checks, resume: pauses at d41
        raw_steps = _steps(world)
        push_step = next(s for s in raw_steps if s.get("stage") == "push" and s.get("kind") == "outcome")
        candidate_commit = push_step["data"]["candidate_commit"]
        fake.set_check(candidate_commit, "ci", "completed", "success")

        retry_decision_id = str(retry_pause["decision_id"])
        _answer(service, world.workspace_id, retry_decision_id, "resume")

        view = tick_until(
            worker,
            world,
            lambda v: any(s.get("rule_id") == "d41-merge-approval" for s in v.get("steps", [])),
        )
        steps = view["steps"]
        d41_pause = next(s for s in steps if s.get("rule_id") == "d41-merge-approval")
        d41_decision_id = str(d41_pause["decision_id"])

        # Still no PUT call made
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 0
        # Still only one POST call
        posts = [c for c in fake.calls if c[0] == "POST"]
        assert len(posts) == 1

        # 3. Unsigned attempt: rejected, no PUT
        status_code, body = _command(
            service,
            world.workspace_id,
            {
                "type": "answer_decision",
                "payload": {"decision_id": d41_decision_id, "answer": "approve"},
            },
        )
        assert status_code == 409
        assert body["error"]["code"] == "approval_required"
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 0

        status_code, body = _command(
            service,
            world.workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": d41_decision_id,
                    "answer": "approve",
                    "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
                },
            },
        )
        assert status_code == 409
        assert body["error"]["code"] == "approval_required"
        assert body["error"]["diagnostics"] == ["owner_signature_required"]
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 0

        # 4. Signed approval: one PUT (merge token), main = merge commit; one POST
        with psycopg.connect(
            **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn, conn.cursor() as cur:
            record = find_decision(cur, world.workspace_id, UUID(d41_decision_id))
            assert record is not None
            target_sha = str(record["target_sha256"])

        assert target_sha is not None
        signature = sign_merge_answer(
            world.owner_key_path,
            workspace_id=world.workspace_id,
            decision_id=UUID(d41_decision_id),
            action_class="merge_protected_branch",
            answer="approve",
            target_sha256=target_sha,
        )
        _answer(
            service,
            world.workspace_id,
            d41_decision_id,
            "approve",
            owner_signature=signature,
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
        )

        view = tick_until(
            worker,
            world,
            lambda _v: any(
                s.get("stage") == "merge" and bool(s.get("data", {}).get("merge_commit_sha"))
                for s in _steps(world)
            ),
        )

        # Verify one PUT call with merge token
        puts = [c for c in fake.calls if c[0] == "PUT"]
        assert len(puts) == 1
        assert puts[0][2] == merge_token

        # Verify one POST call in total
        posts = [c for c in fake.calls if c[0] == "POST"]
        assert len(posts) == 1

        # Verify main branch at bare repo equals the merge commit
        raw_steps = _steps(world)
        merge_step = next(
            s for s in reversed(raw_steps)
            if s.get("stage") == "merge" and s.get("kind") == "outcome"
        )
        merge_commit_sha = merge_step["data"]["merge_commit_sha"]
        main_sha = git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
        assert main_sha == merge_commit_sha

    finally:
        fake.close()


class _MockContext:
    def __init__(
        self,
        prior: dict[str, Any] | None = None,
        request: dict[str, Any] | None = None,
        config: Any = None,
        projects: Any = None,
    ) -> None:
        self.workspace_id = uuid4()
        self.principal = dataclasses.make_dataclass("Principal", [("actor_id", UUID)])(uuid4())
        self.project_id = uuid4()
        self.mission_id = uuid4()
        self.prior = prior or {}
        self.request = request or {}
        self.config = config
        self.projects = projects


def test_merge_stage_unit_candidate_missing() -> None:
    ctx = _MockContext(prior={}, request={"repository": "repo", "candidate_ref": "c", "target_ref": "t"})
    res = run_merge(ctx)  # type: ignore[arg-type]
    assert res.outcome == "failed"
    assert res.blocker == "candidate_missing"
    assert res.data["code"] == "candidate_missing"


def test_merge_stage_unit_repository_missing() -> None:
    class DummyProjects:
        def read_project(self, *args: Any) -> dict:
            return {"repositories": []}

    # Missing repository in request
    ctx = _MockContext(
        prior={"freeze": {"data": {"candidate_commit": "abc"}}},
        request={"repository": "", "candidate_ref": "c", "target_ref": "t"},
        projects=DummyProjects(),
    )
    res = run_merge(ctx)  # type: ignore[arg-type]
    assert res.outcome == "failed"
    assert res.blocker == "repository_missing"
    assert res.data["code"] == "repository_missing"

    # Repository not in project record
    ctx2 = _MockContext(
        prior={"freeze": {"data": {"candidate_commit": "abc"}}},
        request={"repository": "missing", "candidate_ref": "c", "target_ref": "t"},
        projects=DummyProjects(),
    )
    res2 = run_merge(ctx2)  # type: ignore[arg-type]
    assert res2.outcome == "failed"
    assert res2.blocker == "repository_missing"


def test_merge_stage_unit_pull_request_config_missing() -> None:
    class DummyProjects:
        def read_project(self, *args: Any) -> dict:
            return {"repositories": [{"key": "myrepo", "default_branch": "main"}]}

    config = dataclasses.make_dataclass(
        "Config",
        [
            ("github_repositories", dict),
            ("github_token_path", Path | None),
        ],
    )({}, None)

    ctx = _MockContext(
        prior={"freeze": {"data": {"candidate_commit": "abc"}}},
        request={"repository": "myrepo", "candidate_ref": "c", "target_ref": "t"},
        projects=DummyProjects(),
        config=config,
    )
    res = run_merge(ctx)  # type: ignore[arg-type]
    assert res.outcome == "failed"
    assert res.blocker == "pull_request_config_missing"
    assert res.data["code"] == "pull_request_config_missing"
