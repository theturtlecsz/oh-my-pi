"""The disposable orchestrator world routes, classifies, and judges (OMP-417-s07-s03)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.action_classify import Operation, ResolvedTarget, classify
from omp_work.mission_intake_rules import confirmation_route
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler, StageContext
from omp_work.orchestrator.verifier import accept_candidate, sign_verdict
from omp_work.standing_mandate import MissionScopeDraft, mission_scope
from omp_work.v1.owner_signature import (
    decision_signature_message,
    verify_owner_signature,
)
from omp_work.v1.semantics import normalize_auditor_report
from orchestrator_e2e_support import (
    CANDIDATE_SHA256,
    FORGED_ATTEMPT,
    FORGED_CANDIDATE,
    OTHER_ATTEMPT,
    SimulatedCrash,
    answer_intake,
    crash_after_command,
    crash_on_step,
    draft_intake,
    kill_first_run,
    open_e2e_world,
    push_ref,
    remote_updates,
    run_verifier_report,
    run_verifier_script,
    run_worker_script,
    seed_published_budget,
    set_verifier_mode,
    sign_merge_answer,
    tick_until,
)
from test_workflow_service import _batch, _command

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_ATTEMPT = "a-1"
_PRODUCER = "worker"
_EXPIRES = datetime(2099, 1, 1, tzinfo=timezone.utc)
_SUICIDE = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"


def _world(service, root: Path, monkeypatch, **kwargs):
    root.mkdir(parents=True, exist_ok=True)
    return open_e2e_world(
        service,
        root,
        monkeypatch,
        verifier_mode=kwargs.pop("verifier_mode", "pass"),
        worker_mode=kwargs.pop("worker_mode", "ok"),
        operations=kwargs.pop("operations", ()),
        **kwargs,
    )


def _without_extra(world):
    extra = world.extra_capability
    return world.draft.model_copy(
        update={
            "requested_capabilities": tuple(
                item for item in world.draft.requested_capabilities if item != extra
            )
        }
    )


def _scope(draft):
    budget = draft.budget_policy
    return MissionScopeDraft(
        goals=frozenset({draft.objective}),
        repositories=frozenset(draft.repositories),
        capabilities=frozenset(draft.requested_capabilities),
        tier3_classes=frozenset(draft.approval_classes),
        budget_ceiling_usd=None if budget is None else Decimal(budget.usd),
    )


def _accept(
    world, record, *, attempt_id: str = _ATTEMPT, candidate: str = CANDIDATE_SHA256
) -> bool:
    return accept_candidate(
        world.verifier_key,
        record,
        candidate_sha256=candidate,
        attempt_id=attempt_id,
        producer_id=_PRODUCER,
    )


def test_confirmation_route(service, tmp_path: Path, monkeypatch) -> None:
    world = _world(service, tmp_path / "extra", monkeypatch)
    assert world.extra_capability == "egress"
    assert world.extra_capability not in world.mandate.capabilities
    assert world.objective in world.mandate.goals
    assert world.draft.objective in world.mandate.goals
    assert "merge_protected_branch" in world.mandate.tier3_classes
    assert not (world.config_path.parent / "owner.json").exists()

    route = confirmation_route(None, world.draft, world.mandate, world.ceiling)
    assert route.kind == "owner_decision"

    edited = _without_extra(world)
    assert "egress" not in edited.requested_capabilities
    standing = confirmation_route(None, edited, world.mandate, world.ceiling)
    assert standing.kind == "standing_mandate"
    assert (
        mission_scope(world.mandate, _scope(edited), world.ceiling).status == "approved"
    )

    drafted = draft_intake(world)
    assert drafted["outcome"] == "awaiting_owner"
    assert drafted["decision_id"]
    answered = answer_intake(world, "edited_draft")
    assert answered["outcome"] == "approved"
    assert "egress" not in answered["mission"]["requested_capabilities"]

    bare = _world(service, tmp_path / "bare", monkeypatch, extra_capability=None)
    assert bare.extra_capability is None
    assert (
        confirmation_route(None, bare.draft, bare.mandate, bare.ceiling).kind
        == "standing_mandate"
    )
    assert (
        mission_scope(bare.mandate, _scope(bare.draft), bare.ceiling).status
        == "approved"
    )
    proceeded = draft_intake(bare)
    assert proceeded["outcome"] == "proceeded"
    assert proceeded["basis"] == "standing_mandate"
    assert proceeded["decision_id"] is None


def test_answer_intake_confirm_and_reject(service, tmp_path: Path, monkeypatch) -> None:
    confirmed = _world(service, tmp_path / "confirm", monkeypatch)
    draft_intake(confirmed)
    assert answer_intake(confirmed, "confirm")["outcome"] == "approved"

    rejected = _world(service, tmp_path / "reject", monkeypatch)
    draft_intake(rejected)
    assert answer_intake(rejected, "reject")["outcome"] == "rejected"


def test_git_push_candidate_is_push_branch_and_main_is_tier3(
    service, tmp_path: Path, monkeypatch
) -> None:
    world = _world(service, tmp_path / "git", monkeypatch)
    record = world.repository_record()
    assert world.candidate_ref.startswith("refs/heads/omp/")
    assert world.target_ref == "main"
    candidate = classify(
        Operation(
            kind="git_push", repository=world.repo_key, branch=world.candidate_ref
        ),
        ResolvedTarget(repository=record),
    )
    assert candidate.action_class == "push_branch"
    protected = classify(
        Operation(kind="git_push", repository=world.repo_key, branch=world.target_ref),
        ResolvedTarget(repository=record),
    )
    assert protected.tier == 3
    assert protected.action_class == "merge_protected_branch"

    before = remote_updates(world)
    assert remote_updates(world, "refs/heads/main") >= 1
    push_ref(world, world.candidate_ref)
    assert remote_updates(world, world.candidate_ref) == 1
    assert remote_updates(world) == before + 1


def test_accept_candidate_modes(service, tmp_path: Path, monkeypatch) -> None:
    world = _world(service, tmp_path / "verify", monkeypatch, verifier_mode="pass")
    code, record = run_verifier_script(world, attempt_id=_ATTEMPT)
    assert code == 0
    assert record == sign_verdict(
        world.verifier_key,
        candidate_sha256=CANDIDATE_SHA256,
        attempt_id=_ATTEMPT,
        verdict="pass",
        verifier_id="verifier",
    )
    assert _accept(world, record) is True
    report = run_verifier_report(world)
    normalized, verdict = normalize_auditor_report(report)
    assert verdict == "PASS"
    assert normalized is not None
    assert "tests pass" in report
    assert "-> exit 0" in report

    set_verifier_mode(world, "fail")
    code, record = run_verifier_script(world, attempt_id=_ATTEMPT)
    assert code == 1
    assert record is not None and record["verdict"] == "fail"
    assert _accept(world, record) is False

    set_verifier_mode(world, "skip")
    code, record = run_verifier_script(world, attempt_id=_ATTEMPT)
    assert code == 0
    assert record is None
    assert not (world.worktree / ".omp" / "verdict.json").exists()
    assert not (world.worktree / ".omp" / "audit-report.txt").exists()
    assert _accept(world, record) is False

    set_verifier_mode(world, "crash")
    code, record = run_verifier_script(world, attempt_id=_ATTEMPT)
    assert code == 1
    assert record is None
    assert _accept(world, record) is False

    set_verifier_mode(world, "other_attempt")
    code, record = run_verifier_script(world, attempt_id=_ATTEMPT)
    assert record is not None
    assert record["attempt_id"] == OTHER_ATTEMPT
    assert _accept(world, record) is False

    forge = _world(service, tmp_path / "forge", monkeypatch, worker_mode="forge")
    assert forge.request.allowed_paths == (
        "feature.txt",
        ".omp/verdict.json",
        ".omp/audit-report.txt",
    )
    assert run_worker_script(forge) == 0
    planted = json.loads(
        (forge.worktree / ".omp" / "verdict.json").read_text(encoding="utf-8")
    )
    assert planted["verdict"] == "pass"
    assert planted["candidate_sha256"] == FORGED_CANDIDATE
    assert planted["attempt_id"] == FORGED_ATTEMPT
    assert _accept(forge, planted) is False
    assert (
        _accept(forge, planted, attempt_id=FORGED_ATTEMPT, candidate=FORGED_CANDIDATE)
        is False
    )


def test_worker_modes_write_their_artifact(
    service, tmp_path: Path, monkeypatch
) -> None:
    world = _world(service, tmp_path / "workers", monkeypatch, worker_mode="ok")
    assert world.request.allowed_paths == ("feature.txt",)
    assert world.request.candidate_ref == world.candidate_ref
    assert world.request.target_ref == "main"
    assert run_worker_script(world) == 0
    assert (world.worktree / "feature.txt").read_text(
        encoding="utf-8"
    ) == "feature implemented\n"

    outside = world.worktree / "outside-run"
    outside.mkdir()
    completed = subprocess.run(
        [*world.worker_argv[:-1], "outside"],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert (outside / "outside.txt").read_text(encoding="utf-8") == "out of envelope\n"

    scope = world.worktree / "scope-run"
    scope.mkdir()
    completed = subprocess.run(
        [*world.worker_argv[:-1], "new_scope"],
        cwd=scope,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    planted = json.loads(
        (scope / ".omp" / "new-scope.json").read_text(encoding="utf-8")
    )
    assert planted["reason"] == "needs a wider scope"


def test_crash_helpers_fire_once(monkeypatch) -> None:
    assert issubclass(SimulatedCrash, BaseException)
    assert not issubclass(SimulatedCrash, Exception)

    commands: list[str] = []

    def fake_command(self, type, payload, *, key=None):
        del self, payload, key
        commands.append(type)
        return {"type": type}

    monkeypatch.setattr(StageContext, "command", fake_command)
    after = crash_after_command(monkeypatch, "create_decision")
    StageContext.command(None, "read_state", {})
    with pytest.raises(SimulatedCrash):
        StageContext.command(None, "create_decision", {})
    StageContext.command(None, "create_decision", {})
    assert after["done"] is True
    assert commands == ["read_state", "create_decision", "create_decision"]

    steps: list[str] = []

    def fake_record(self, step_index, body):
        del self, step_index
        steps.append(body["kind"])
        return {"kind": body["kind"]}

    monkeypatch.setattr(OrchestratorHandler, "record", fake_record)
    before = crash_on_step(monkeypatch, "pause")
    OrchestratorHandler.record(None, 0, {"kind": "advance"})
    with pytest.raises(SimulatedCrash):
        OrchestratorHandler.record(None, 1, {"kind": "pause"})
    OrchestratorHandler.record(None, 2, {"kind": "pause"})
    assert before["done"] is True
    assert steps == ["advance", "pause"]


def test_kill_first_run_sigkills_one_identity(monkeypatch) -> None:
    recorded: list[list[str]] = []

    def fake(argv, **kwargs):
        del kwargs
        recorded.append(list(argv))
        return 0

    monkeypatch.setattr(worker_sandbox, "run_worker", fake)
    holder = kill_first_run(monkeypatch, "worker")
    worker_sandbox.run_worker(["/bin/echo"], identity="verifier")
    worker_sandbox.run_worker(["/bin/echo"], identity="worker")
    worker_sandbox.run_worker(["/bin/echo"], identity="worker")
    assert holder["done"] is True
    assert recorded[0] == ["/bin/echo"]
    assert recorded[1][2] == _SUICIDE
    assert recorded[2] == ["/bin/echo"]

    killed = subprocess.run(["/usr/bin/python3", "-c", _SUICIDE], check=False)
    assert killed.returncode == -signal.SIGKILL


def test_sign_merge_budget_and_tick(service, tmp_path: Path, monkeypatch) -> None:
    world = _world(
        service, tmp_path / "helpers", monkeypatch, test_command=("/bin/true",)
    )
    assert world.request.test_command == ("/bin/true",)
    set_verifier_mode(world, "fail")
    assert world.verifier_argv[-1] == "fail"
    assert world.config.verifier_argv[-1] == "fail"
    assert (
        json.loads(world.config_path.read_text(encoding="utf-8"))["verifier_argv"][-1]
        == "fail"
    )

    decision_id = uuid4()
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=decision_id,
        target_sha256="ab" * 32,
        expires_at=_EXPIRES,
    )
    message = decision_signature_message(
        workspace_id=world.workspace_id,
        decision_id=decision_id,
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256="ab" * 32,
        expires_at=_EXPIRES,
    )
    assert (
        verify_owner_signature(world.allowed_signers_path, message, signature) is True
    )

    status, body = _command(
        service,
        world.workspace_id,
        _batch([{"client_ref": "budget-item", "title": "Budget item"}]),
    )
    assert status == 200, body
    item = body["result"]["items"][0]
    seed_published_budget(
        service,
        world.workspace_id,
        UUID(item["work_id"]),
        UUID(item["revision_id"]),
    )
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        rows = conn.execute(
            "SELECT kind FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (world.workspace_id, item["work_id"]),
        ).fetchall()
    assert rows == [("intake_publication",)]

    ticks = {"n": 0}

    class _Worker:
        def __init__(self) -> None:
            self.ticks = 0

        def tick(self) -> None:
            self.ticks += 1

    worker = _Worker()

    def ready(view) -> bool:
        del view
        ticks["n"] += 1
        return ticks["n"] == 3

    view = tick_until(worker, world, ready, limit=5)
    assert worker.ticks == 2
    assert view["mission_id"] == str(world.mission_id)
