"""Verifier audit integration tests (OMP-417)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.orchestrator import candidate_git, operations, service as orch_service
from omp_work.orchestrator.service import (
    OrchestratorHandler,
    Outcome,
    StageContext,
    submit,
)
from omp_work.orchestrator.verifier import accept_candidate, sign_verdict
from omp_work.v1.models import EvidenceKind
from orchestrator_e2e_support import (
    E2EWorld,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore orch_service._REGISTRY after each test."""
    saved = dict(orch_service._REGISTRY)
    try:
        yield
    finally:
        orch_service._REGISTRY.clear()
        orch_service._REGISTRY.update(saved)


def _seed_item_budget(world: E2EWorld, service_fixture: Any, work_id: UUID) -> UUID:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)
    return revision_id


def _rows(world: E2EWorld, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _attempts(world: E2EWorld, work_id: UUID) -> list[dict]:
    return _rows(
        world,
        """
        SELECT attempt_id::text AS attempt_id, state, launch_count,
               cancelled_launch_count, accepted_report_count,
               in_flight_launch_id::text AS in_flight_launch_id
        FROM omp_work.close_attempts
        WHERE workspace_id = %s AND work_id = %s
        ORDER BY requested_at, attempt_id
        """,
        (world.workspace_id, work_id),
    )


def _launches(world: E2EWorld, work_id: UUID) -> list[dict]:
    return _rows(
        world,
        """
        SELECT l.launch_id::text AS launch_id, l.attempt_id::text AS attempt_id,
               l.launch_number, l.task_sha256, l.tool_call_id
        FROM omp_work.auditor_launches l
        JOIN omp_work.close_attempts a ON a.workspace_id = l.workspace_id AND a.attempt_id = l.attempt_id
        WHERE l.workspace_id = %s AND a.work_id = %s
        ORDER BY l.reserved_at, l.launch_id
        """,
        (world.workspace_id, work_id),
    )


def _receipts(world: E2EWorld, work_id: UUID, kind: str) -> list[dict]:
    return _rows(
        world,
        """
        SELECT receipt_id::text AS receipt_id, kind, payload, candidate_commit,
               candidate_sha256, verdict, issuer, issued_at
        FROM omp_evidence.receipts
        WHERE workspace_id = %s AND work_id = %s AND kind = %s
        ORDER BY issued_at, receipt_id
        """,
        (world.workspace_id, work_id, kind),
    )


def _steps(world: E2EWorld) -> list[dict]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _outcomes(world: E2EWorld, stage: str) -> list[dict]:
    return [
        step
        for step in _steps(world)
        if step.get("stage") == stage and step.get("kind") == "outcome"
    ]


def _verify_worktree(world: E2EWorld) -> Path:
    matches = sorted(world.worktrees_dir.glob(f"{world.mission_id}-*-verify"))
    assert matches, f"no verify worktree found for mission {world.mission_id} in {world.worktrees_dir}"
    return matches[-1]


def _open_world(
    service_fixture: Any,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    test_command: Sequence[str] | None = None,
) -> tuple[E2EWorld, UUID]:
    world = open_e2e_world(
        service_fixture,
        root,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
        test_command=tuple(test_command) if test_command is not None else None,
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service_fixture, work_id)

    verifier_argv = ("/usr/bin/python3", "-P", "-m", "omp_work.orchestrator.verify_main")
    world.config = dataclasses.replace(
        world.config,
        verifier_argv=verifier_argv,
        lease_seconds=300,
    )
    cfg_data = json.loads(world.config_path.read_text(encoding="utf-8"))
    cfg_data["verifier_argv"] = list(verifier_argv)
    cfg_data["lease_seconds"] = 300
    world.config_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

    return world, work_id


def _patch_sandbox(monkeypatch: pytest.MonkeyPatch, *, pre_exec=None) -> None:
    saved_sandbox = operations._sandbox

    def patched_sandbox(
        ctx: StageContext,
        argv: Sequence[str],
        worktree: Path,
        *,
        identity: str = "worker",
        ro_binds: Sequence[str | Path] = (),
    ) -> int | None:
        if identity == "verifier":
            if pre_exec is not None:
                pre_exec(ctx, worktree)
            exp = ctx.lease_expires_at()
            if exp is None:
                return None
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            else:
                exp = exp.astimezone(timezone.utc)
            remaining = (exp - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return None
            env = dict(os.environ)
            if "PYTHONPATH" not in env or not env["PYTHONPATH"]:
                env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            try:
                completed = subprocess.run(
                    list(argv),
                    cwd=str(worktree),
                    env=env,
                    timeout=remaining,
                )
                return completed.returncode
            except subprocess.TimeoutExpired:
                return None
        return saved_sandbox(ctx, argv, worktree, identity=identity, ro_binds=ro_binds)

    monkeypatch.setattr(operations, "_sandbox", patched_sandbox)


def test_good_commit_settles_one_audited_pass(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, work_id = _open_world(service, tmp_path / "good", monkeypatch)
    _patch_sandbox(monkeypatch)
    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return any(
            step.get("outcome") == "succeeded" for step in _outcomes(world, "audit")
        )

    tick_until(worker, world, audited, limit=60)

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["state"] == "audited"
    assert attempt["launch_count"] == 1
    assert attempt["accepted_report_count"] == 1
    assert attempt["cancelled_launch_count"] == 0

    launches = _launches(world, work_id)
    assert len(launches) == 1

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "succeeded"
    assert audit_outcome["verdict"] == "pass"

    freeze_digest = str(audit_outcome["data"]["candidate_sha256"])
    attempt_id = str(attempt["attempt_id"])

    audit_receipts = _receipts(world, work_id, EvidenceKind.AUDIT.value)
    assert len(audit_receipts) == 1
    audit_receipt = audit_receipts[0]
    assert audit_receipt["verdict"] == "PASS"
    assert audit_receipt["candidate_sha256"] == freeze_digest

    wt = _verify_worktree(world)
    verdict_file = wt / ".omp" / "verdict.json"
    assert verdict_file.is_file()
    on_disk_verdict = json.loads(verdict_file.read_text(encoding="utf-8"))

    expected_verdict = sign_verdict(
        world.verifier_key,
        candidate_sha256=freeze_digest,
        attempt_id=attempt_id,
        verdict="pass",
        verifier_id="verifier",
    )
    assert on_disk_verdict == expected_verdict

    assert (
        accept_candidate(
            world.verifier_key,
            on_disk_verdict,
            candidate_sha256=freeze_digest,
            attempt_id=attempt_id,
            producer_id="worker",
        )
        is True
    )

    report_file = wt / ".omp" / "audit-report.txt"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "sandbox_unavailable" not in report_text
    assert "VERDICT: PASS" in report_text


def test_failing_test_command_settles_no_pass(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_command = (
        "/usr/bin/python3",
        "-c",
        "import os, sys; sys.exit(0 if os.path.exists('.git') else 1)",
    )
    world, work_id = _open_world(
        service, tmp_path / "failing", monkeypatch, test_command=test_command
    )
    _patch_sandbox(monkeypatch)
    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=60)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"
    assert audit_outcome.get("blocker") == "auditor_launches_exhausted"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["launch_count"] == 3
    assert attempt["cancelled_launch_count"] == 3
    assert attempt["accepted_report_count"] == 0

    assert _receipts(world, work_id, EvidenceKind.AUDIT.value) == []

    wt = _verify_worktree(world)
    report_file = wt / ".omp" / "audit-report.txt"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "tests_exit_1" in report_text

    verdict_file = wt / ".omp" / "verdict.json"
    assert not verdict_file.exists()


def test_outside_path_rejected_at_audit_boundary(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, work_id = _open_world(service, tmp_path / "outside", monkeypatch)
    _patch_sandbox(monkeypatch)

    original_run_audit = operations.run_audit

    def wrapped_run_audit(ctx: StageContext) -> Outcome:
        req = dict(ctx.request)
        req["allowed_paths"] = []
        ctx.request = req
        return original_run_audit(ctx)

    monkeypatch.setattr(operations, "run_audit", wrapped_run_audit)
    orch_service._REGISTRY["audit"] = wrapped_run_audit

    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=60)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"
    assert audit_outcome.get("blocker") == "auditor_launches_exhausted"

    assert _receipts(world, work_id, EvidenceKind.AUDIT.value) == []

    wt = _verify_worktree(world)
    report_file = wt / ".omp" / "audit-report.txt"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "preflight_envelope_violation" in report_text

    verdict_file = wt / ".omp" / "verdict.json"
    assert not verdict_file.exists()


def test_changed_verifier_checkout_rejected(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, work_id = _open_world(service, tmp_path / "dirty", monkeypatch)
    _patch_sandbox(monkeypatch)

    orig_reset_worktree = candidate_git.reset_worktree

    def patched_reset_worktree(
        control_repo: Path | str,
        worktrees_dir: Path | str,
        name: str,
        commit: str,
    ) -> Path:
        dest = orig_reset_worktree(control_repo, worktrees_dir, name, commit)
        if str(name).endswith("-verify"):
            feature = dest / "feature.txt"
            with feature.open("a", encoding="utf-8") as f:
                f.write("dirty modification\n")
        return dest

    monkeypatch.setattr(candidate_git, "reset_worktree", patched_reset_worktree)
    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=60)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"
    assert audit_outcome.get("blocker") == "auditor_launches_exhausted"

    assert _receipts(world, work_id, EvidenceKind.AUDIT.value) == []

    wt = _verify_worktree(world)
    report_file = wt / ".omp" / "audit-report.txt"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "preflight_dirty_worktree" in report_text

    verdict_file = wt / ".omp" / "verdict.json"
    assert not verdict_file.exists()


def test_wrong_attempt_replay_settles_no_pass(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, work_id = _open_world(service, tmp_path / "wrong-attempt", monkeypatch)
    other_attempt_id = str(uuid4())

    def pre_exec(_ctx: StageContext, worktree: Path) -> None:
        task_path = worktree / ".omp" / "verify-task.json"
        task_data = json.loads(task_path.read_text(encoding="utf-8"))
        task_data["attempt_id"] = other_attempt_id
        task_path.write_text(json.dumps(task_data, indent=2), encoding="utf-8")

    _patch_sandbox(monkeypatch, pre_exec=pre_exec)
    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=60)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"
    assert audit_outcome["data"]["code"] == "candidate_rejected"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    stage_attempt_id = str(attempts[0]["attempt_id"])
    assert other_attempt_id != stage_attempt_id

    wt = _verify_worktree(world)
    verdict_file = wt / ".omp" / "verdict.json"
    assert verdict_file.is_file()
    on_disk_verdict = json.loads(verdict_file.read_text(encoding="utf-8"))

    freeze_digest = str(audit_outcome["data"]["candidate_sha256"])

    assert (
        accept_candidate(
            world.verifier_key,
            on_disk_verdict,
            candidate_sha256=freeze_digest,
            attempt_id=stage_attempt_id,
            producer_id="worker",
        )
        is False
    )

    assert (
        accept_candidate(
            world.verifier_key,
            on_disk_verdict,
            candidate_sha256=freeze_digest,
            attempt_id=other_attempt_id,
            producer_id="worker",
        )
        is True
    )

    assert _receipts(world, work_id, EvidenceKind.AUDIT.value) == []


def test_malicious_command_cannot_forge_a_pass(
    service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "malicious"
    key_path = root / "verifier.key"
    malicious_script = (
        "import os, sys\n"
        "if os.path.exists('.git'):\n"
        "    sys.exit(0)\n"
        "try:\n"
        f"    with open({str(key_path)!r}, 'rb') as f:\n"
        "        _ = f.read()\n"
        "    os.makedirs('.omp', exist_ok=True)\n"
        "    with open('.omp/verdict.json', 'w') as f:\n"
        "        f.write('{{\"verdict\": \"pass\"}}')\n"
        "except OSError:\n"
        "    pass\n"
        "sys.exit(42)\n"
    )
    test_command = ("/usr/bin/python3", "-c", malicious_script)
    world, work_id = _open_world(service, root, monkeypatch, test_command=test_command)
    _patch_sandbox(monkeypatch)
    worker = open_worker(world)

    def audited(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=60)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"
    assert audit_outcome.get("blocker") == "auditor_launches_exhausted"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["launch_count"] == 3
    assert attempt["cancelled_launch_count"] == 3
    assert attempt["accepted_report_count"] == 0

    assert _receipts(world, work_id, EvidenceKind.AUDIT.value) == []
    assert world.verifier_key_path.read_bytes() == world.verifier_key

    wt = _verify_worktree(world)
    report_file = wt / ".omp" / "audit-report.txt"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "tests_exit_42" in report_text

    verdict_file = wt / ".omp" / "verdict.json"
    assert not verdict_file.exists()
