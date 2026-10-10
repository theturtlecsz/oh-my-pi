"""PostgreSQL mission lifecycle and end-to-end orchestrator tests (OMP-417-s07).

Worker ticks drive the pipeline from intake through close under default operations.
Owner acts are limited to answering the intake draft with edited_draft and signing
the d41 merge decision with the owner key.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator.audit_chain import verify_domain_chain, verify_step_chain
from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.service import (
    OrchestratorHandler,
    load_config,
    status,
    submit,
)
from omp_work.orchestrator.stages import STAGE_ORDER
from omp_work.v1.service import Principal, WorkService
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import (
    E2EWorld,
    kill_first_run,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    sign_merge_answer,
    tick_until,
)
from test_workflow_service import OWNER, _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _open(service: Any, root: Path, monkeypatch: Any, **kwargs: Any) -> E2EWorld:
    """Build one E2EWorld with default operations."""
    root.mkdir(parents=True, exist_ok=True)
    world = open_e2e_world(
        service,
        root,
        monkeypatch,
        verifier_mode=kwargs.pop("verifier_mode", "pass"),
        worker_mode=kwargs.pop("worker_mode", "ok"),
        extra_capability=kwargs.pop("extra_capability", "egress"),
        test_command=kwargs.pop("test_command", None),
        operations=(),
        **kwargs,
    )
    # Remove the "operations" key from orchestrator.json to test default operations
    doc = json.loads(world.config_path.read_text(encoding="utf-8"))
    doc.pop("operations", None)
    world.config_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    world.config = load_config(world.config_path, service.config)
    assert world.config.operations == ("omp_work.orchestrator.operations:register",)
    return world


def _seed_item_budget(world: E2EWorld, service_fixture: Any, work_id: UUID) -> UUID:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)
    return revision_id


def _steps(world: E2EWorld) -> list[dict[str, Any]]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _answer_intake_edited(world: E2EWorld) -> None:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()
    intake_step = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    world.intake_decision_id = UUID(str(intake_step["data"]["decision_id"]))
    world.mission_revision = int(intake_step["data"]["mission_revision"])
    from orchestrator_e2e_support import answer_intake

    answered = answer_intake(world, "edited_draft")
    assert answered["outcome"] == "approved"


def test_mission_lifecycle_intake_to_close(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Full mission lifecycle: intake through close with default operations."""
    world = _open(service, tmp_path / "lifecycle", monkeypatch)
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    worker = open_worker(world)

    # 1. Intake runs and confirm stage pauses for the owner
    view = tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    confirm_pause = next(
        s for s in view["steps"] if s.get("rule_id") == "d23-owner-confirm"
    )
    assert confirm_pause is not None

    # Owner acts: answer_intake(edited_draft)
    _answer_intake_edited(world)

    # 2. Worker resumes: runs plan, grant, implement, evaluate, freeze, push, audit;
    # then pauses at merge under d41-merge-approval.
    view = tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d41-merge-approval" for s in v.get("steps", [])),
    )
    raw_steps = _steps(world)
    push_outcome = next(
        s
        for s in raw_steps
        if s.get("stage") == "push" and s.get("kind") == "outcome"
    )
    candidate_commit = str(push_outcome["data"]["candidate_commit"])

    # 3. WorkService.client_read project.decisions: merge decision has d41 question,
    # approve/decline options, and classify() target_sha256 digest of that commit.
    work_service = WorkService(PostgresWorkStore(service.config))
    principal = Principal(
        actor_id=OWNER,
        actor_kind="owner",
        workspaces=frozenset({world.workspace_id}),
        scopes=frozenset({"work.read", "work.client"}),
    )
    decisions_view = work_service.client_read(
        principal, world.workspace_id, "project.decisions", world.project_id
    )
    decisions = decisions_view.get("decisions") or []
    assert len(decisions) >= 1
    merge_decision = next(
        d for d in decisions if d.get("action_class") == "merge_protected_branch"
    )

    question = str(merge_decision.get("question") or "")
    assert f"Candidate {candidate_commit[:12]}" in question
    assert "ready to merge into main. Merge it?" in question
    assert set(merge_decision.get("options") or ()) == {"approve", "decline"}

    op = {
        "kind": "git_push",
        "repository": world.repo_key,
        "branch": "main",
        "commit": candidate_commit,
    }
    resolved = ResolvedTarget(
        repository=world.repository_record(),
        commit=candidate_commit,
        changed_paths=(),
    )
    expected_digest = classify(parse_submission(op), resolved).target_sha256
    assert merge_decision.get("target_sha256") == expected_digest

    # 4. Owner acts: signed merge answer only
    d41_decision_id = UUID(str(merge_decision["decision_id"]))
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=d41_decision_id,
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=expected_digest,
        expires_at=_FUTURE,
    )
    status_code, body = _command(
        service,
        world.workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(d41_decision_id),
                "answer": "approve",
                "owner_signature": signature,
                "expires_at": _FUTURE.isoformat(),
            },
        },
    )
    assert status_code == 200, body

    # 5. Worker resumes: runs merge, close; mission completes
    view = tick_until(
        worker,
        world,
        lambda v: v.get("status") == "completed",
    )
    assert view["status"] == "completed"

    # Remote refs/heads/main equals candidate commit
    main_tip = git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
    assert main_tip == candidate_commit

    # 6. Verify steps:
    steps = _steps(world)

    # Every STAGE_ORDER stage is present with rule ids
    stages_present = {s.get("stage") for s in steps if s.get("stage")}
    assert set(STAGE_ORDER).issubset(stages_present)
    for step in steps:
        assert step.get("rule_id"), f"step missing rule_id: {step}"

    # Action authorization, intent, and done for push and merge
    for stage_name, rule_name in (("push", "push-candidate"), ("merge", "d41-merge-approval")):
        assert any(
            s.get("kind") == "action_allowed" and s.get("rule_id") == rule_name
            for s in steps
        ), f"missing action_allowed for {stage_name}"
        assert any(
            s.get("kind") == "external_intent"
            for s in steps
        ), f"missing external_intent for {stage_name}"
        assert any(
            s.get("kind") == "external_done"
            for s in steps
        ), f"missing external_done for {stage_name}"

    # Merge pause and resume
    assert any(
        s.get("kind") == "pause" and s.get("rule_id") == "d41-merge-approval"
        for s in steps
    )

    # Chains verify
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        assert verify_step_chain(cur, world.workspace_id, str(world.mission_id)) == (True, None)
        assert verify_domain_chain(cur, world.workspace_id, str(world.mission_id)) == (True, None)
        assert verify_domain_chain(cur, world.workspace_id, str(work_id)) == (True, None)


def test_kill_first_run_verifier_completes_and_delivers(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A crashed verifier retries, completes, and leaves no undelivered requires_delivery event."""
    world = _open(service, tmp_path / "kill-verifier", monkeypatch)
    kill_first_run(monkeypatch, "verifier")
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    worker = open_worker(world)

    # Confirm pause
    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    _answer_intake_edited(world)

    # The verifier dies on its first run; audit retries, succeeds, and pauses at merge
    view = tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d41-merge-approval" for s in v.get("steps", [])),
    )
    d41_step = next(s for s in view["steps"] if s.get("rule_id") == "d41-merge-approval")
    d41_decision_id = UUID(str(d41_step["decision_id"]))

    raw_steps = _steps(world)
    push_outcome = next(
        s for s in raw_steps if s.get("stage") == "push" and s.get("kind") == "outcome"
    )
    candidate_commit = str(push_outcome["data"]["candidate_commit"])
    op = {
        "kind": "git_push",
        "repository": world.repo_key,
        "branch": "main",
        "commit": candidate_commit,
    }
    resolved = ResolvedTarget(
        repository=world.repository_record(),
        commit=candidate_commit,
        changed_paths=(),
    )
    digest = classify(parse_submission(op), resolved).target_sha256

    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=d41_decision_id,
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=digest,
        expires_at=_FUTURE,
    )
    _command(
        service,
        world.workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(d41_decision_id),
                "answer": "approve",
                "owner_signature": signature,
                "expires_at": _FUTURE.isoformat(),
            },
        },
    )

    tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert status(world.config, world.mission_id)["status"] == "completed"

    # Assert no undelivered requires_delivery event across all attempts
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.event_id, e.attempt_id
            FROM omp_work.close_attempt_events e
            LEFT JOIN omp_work.checkpoint_deliveries d
              ON d.workspace_id = e.workspace_id
             AND d.event_id = e.event_id
             AND d.status IN ('delivered', 'waived')
            WHERE e.workspace_id = %s
              AND e.requires_delivery = true
              AND d.delivery_id IS NULL
            """,
            (world.workspace_id,),
        )
        undelivered = cur.fetchall()
        assert undelivered == []


def test_evaluate_step_identity_and_host_escape_fails(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Evaluate step names identity 'worker' and writing outside worktree fails."""
    escape_file = tmp_path / "host_file.txt"
    escape_cmd = (
        "/usr/bin/python3",
        "-c",
        f"from pathlib import Path; Path({str(escape_file)!r}).write_text('bad'); exit(0)",
    )
    world = _open(
        service,
        tmp_path / "eval-escape",
        monkeypatch,
        test_command=escape_cmd,
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    _answer_intake_edited(world)

    # Worker ticks through plan, grant, implement, and evaluate fails
    tick_until(
        worker,
        world,
        lambda _v: any(
            s.get("stage") == "evaluate" and s.get("kind") == "outcome"
            for s in _steps(world)
        ),
    )

    steps = _steps(world)
    eval_outcome = next(
        s for s in steps if s.get("stage") == "evaluate" and s.get("kind") == "outcome"
    )
    assert eval_outcome["data"]["identity"] == "worker"
    assert eval_outcome["outcome"] == "failed"
    assert not escape_file.exists()


@pytest.mark.parametrize("refusal_mode", ["skip", "fail", "forge_skip"])
def test_verifier_refusal_leaves_mission_running_and_nothing_settled(
    service: Any, tmp_path: Path, monkeypatch: Any, refusal_mode: str
) -> None:
    """Verifier skip, fail, or forge+skip leaves mission running, item open, and nothing settled."""
    worker_mode = "forge" if refusal_mode == "forge_skip" else "ok"
    verifier_mode = "skip" if refusal_mode in ("skip", "forge_skip") else "fail"

    world = _open(
        service,
        tmp_path / f"refusal-{refusal_mode}",
        monkeypatch,
        worker_mode=worker_mode,
        verifier_mode=verifier_mode,
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    worker = open_worker(world)

    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    _answer_intake_edited(world)

    # Ticks until audit outcome is recorded
    tick_until(
        worker,
        world,
        lambda _v: any(
            s.get("stage") == "audit" and s.get("kind") == "outcome"
            for s in _steps(world)
        ),
    )

    steps = _steps(world)
    audit_outcome = next(
        s for s in reversed(steps) if s.get("stage") == "audit" and s.get("kind") == "outcome"
    )
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"

    # Mission status as before audit: still running
    current_status = status(world.config, world.mission_id)
    assert current_status["status"] == "running"

    # Execution item stays open (not completed)
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state, accepted_report_count FROM omp_work.close_attempts WHERE workspace_id=%s AND work_id=%s",
            (world.workspace_id, work_id),
        )
        attempts = cur.fetchall()
        assert all(a["accepted_report_count"] == 0 for a in attempts)

        cur.execute(
            "SELECT COUNT(*) as count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='audit'",
            (world.workspace_id, work_id),
        )
        assert cur.fetchone()["count"] == 0

    # No merge happened
    assert not any(s.get("stage") == "merge" and s.get("kind") == "outcome" for s in steps)


def test_unlisted_key_answer_refuses_merge(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """An answer signed by a key unlisted in orchestrator allowed_signers blocks merge."""
    world = _open(service, tmp_path / "unlisted-key", monkeypatch)
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    _answer_intake_edited(world)

    view = tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d41-merge-approval" for s in v.get("steps", [])),
    )
    d41_step = next(s for s in view["steps"] if s.get("rule_id") == "d41-merge-approval")
    d41_decision_id = UUID(str(d41_step["decision_id"]))

    raw_steps = _steps(world)
    push_outcome = next(
        s for s in raw_steps if s.get("stage") == "push" and s.get("kind") == "outcome"
    )
    candidate_commit = str(push_outcome["data"]["candidate_commit"])
    op = {
        "kind": "git_push",
        "repository": world.repo_key,
        "branch": "main",
        "commit": candidate_commit,
    }
    resolved = ResolvedTarget(
        repository=world.repository_record(),
        commit=candidate_commit,
        changed_paths=(),
    )
    digest = classify(parse_submission(op), resolved).target_sha256

    # Generate a foreign key that is NOT in world.config.allowed_signers
    foreign_key = tmp_path / "foreign_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(foreign_key)],
        check=True,
        capture_output=True,
    )
    foreign_pub = Path(f"{foreign_key}.pub").read_text(encoding="utf-8").strip()

    # Append foreign key to service's owner_allowed_signers so the command endpoint accepts it,
    # but leave world.config.allowed_signers without it!
    service_signers = service.config.config_dir / "owner_allowed_signers"
    existing_signers = service_signers.read_text(encoding="utf-8")
    service_signers.write_text(existing_signers + f"owner {foreign_pub}\n", encoding="utf-8")

    foreign_sig = sign_merge_answer(
        foreign_key,
        workspace_id=world.workspace_id,
        decision_id=d41_decision_id,
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=digest,
        expires_at=_FUTURE,
    )

    status_code, body = _command(
        service,
        world.workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(d41_decision_id),
                "answer": "approve",
                "owner_signature": foreign_sig,
                "expires_at": _FUTURE.isoformat(),
            },
        },
    )
    assert status_code == 200, body

    # Worker ticks merge -> blocker merge_credential_refused
    tick_until(
        worker,
        world,
        lambda _v: len([
            s for s in _steps(world)
            if s.get("stage") == "merge" and s.get("kind") == "outcome"
        ]) >= 2,
    )

    merge_outcome = [
        s for s in _steps(world) if s.get("stage") == "merge" and s.get("kind") == "outcome"
    ][-1]
    assert merge_outcome["outcome"] == "failed"
    assert merge_outcome["blocker"] == "merge_credential_refused"

    # Main branch on remote was NOT updated
    main_tip = git(world.remote_repo, "rev-parse", "refs/heads/main").decode("ascii").strip()
    assert main_tip == world.base_commit
