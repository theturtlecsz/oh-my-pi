"""Postgres tests for the audit stage operation (OMP-417-s07-s06).

Done when:
- pass -> settled PASS, body = real command+exit
- crash_after_command("settle_auditor_launch"), re-run -> PASS, 1 reserve, 1 settle, 0 cancel.
- kill_first_run("verifier") -> 1 attempt, 1 cancelled, 1 settled; intents :1, :2 done.
- crash_after_command("reserve_auditor_launch"), lease expiry, re-run -> 1 cancelled, 1 settled.
- Always crash -> 3 cancelled, unrecoverable-blocker.
- fail/skip/other_attempt, forge+skip -> nothing settled, failed; mission status, item unchanged (open).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import shlex
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.orchestrator import operations, service
from omp_work.orchestrator.service import (
    LeaseClaim,
    OrchestratorHandler,
    StageContext,
    _prior,
    submit,
)
from omp_work.v1.models import EvidenceKind
from orchestrator_e2e_support import (
    SimulatedCrash,
    crash_after_command,
    kill_first_run,
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
        verifier_mode=kwargs.get("verifier_mode", "pass"),
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


def _rows(world, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _attempts(world, work_id: UUID) -> list[dict]:
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


def _launches(world, work_id: UUID) -> list[dict]:
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


def _receipts(world, work_id: UUID, kind: str) -> list[dict]:
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


def _payload(value) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    raise AssertionError(f"receipt payload is {type(value).__name__}")


def test_audit_stage_pass_settles_and_verifies_body(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Pass settles PASS with verification evidence body matching real command and exit."""
    world, worker, work_id = _open(service, tmp_path / "pass", monkeypatch, verifier_mode="pass")

    def audited(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=50)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "succeeded"
    assert audit_outcome["verdict"] == "pass"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "audited"
    assert attempts[0]["accepted_report_count"] == 1
    assert attempts[0]["cancelled_launch_count"] == 0

    v_receipts = _receipts(world, work_id, EvidenceKind.VERIFICATION.value)
    assert len(v_receipts) >= 1
    v_rec = v_receipts[-1]
    body = _payload(v_rec["payload"])
    expected_body = f"{shlex.join(world.request.test_command)} exited 0"
    assert body["body"] == expected_body
    assert body["command"] == list(world.request.test_command)
    assert body["exit_code"] == 0

    a_receipts = _receipts(world, work_id, EvidenceKind.AUDIT.value)
    assert len(a_receipts) == 1
    assert a_receipts[0]["verdict"] == "PASS"


def test_audit_stage_crash_after_settle_rerun(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Crash after settle_auditor_launch re-runs to PASS with 1 reserve, 1 settle, 0 cancel."""
    world, worker, work_id = _open(service, tmp_path / "crash-settle", monkeypatch, verifier_mode="pass")

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)
    crash_after_command(monkeypatch, "settle_auditor_launch")
    with pytest.raises(SimulatedCrash):
        worker.tick()
    _expire_lease(world)

    def audited(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=20)

    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "succeeded"
    assert audit_outcome["verdict"] == "pass"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "audited"
    assert attempts[0]["launch_count"] == 1
    assert attempts[0]["accepted_report_count"] == 1
    assert attempts[0]["cancelled_launch_count"] == 0

    launches = _launches(world, work_id)
    assert len(launches) == 1


def test_audit_stage_kill_first_run_verifier(
    service, tmp_path: Path, monkeypatch
) -> None:
    """kill_first_run('verifier') leaves 1 attempt, 1 cancelled, 1 settled; intents :1, :2 done."""
    world, worker, work_id = _open(service, tmp_path / "kill-verifier", monkeypatch, verifier_mode="pass")
    kill_first_run(monkeypatch, "verifier")

    def audited(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=50)

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["state"] == "audited"
    assert attempt["launch_count"] == 2
    assert attempt["cancelled_launch_count"] == 1
    assert attempt["accepted_report_count"] == 1

    launches = _launches(world, work_id)
    assert len(launches) == 2

    steps = _steps(world)
    attempt_id = attempt["attempt_id"]
    done_1 = [
        s
        for s in steps
        if s.get("kind") == "external_done" and s.get("external_ref") == f"verifier:{attempt_id}:1"
    ]
    done_2 = [
        s
        for s in steps
        if s.get("kind") == "external_done" and s.get("external_ref") == f"verifier:{attempt_id}:2"
    ]
    assert len(done_1) >= 1
    assert len(done_2) >= 1


def test_audit_stage_crash_after_reserve_lease_expiry_rerun(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Crash after reserve_auditor_launch, lease expiry, re-run -> 1 cancelled, 1 settled."""
    world, worker, work_id = _open(service, tmp_path / "crash-reserve", monkeypatch, verifier_mode="pass")

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)
    crash_after_command(monkeypatch, "reserve_auditor_launch")
    with pytest.raises(SimulatedCrash):
        worker.tick()
    _expire_lease(world)

    def audited(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "audit"))

    tick_until(worker, world, audited, limit=20)

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["state"] == "audited"
    assert attempt["launch_count"] == 2
    assert attempt["cancelled_launch_count"] == 1
    assert attempt["accepted_report_count"] == 1

    launches = _launches(world, work_id)
    assert len(launches) == 2


def test_audit_stage_always_crash_reaches_blocker(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Always crash -> 3 cancelled, unrecoverable-blocker."""
    world, worker, work_id = _open(service, tmp_path / "always-crash", monkeypatch, verifier_mode="crash")

    def paused(view: dict) -> bool:
        return any(
            step.get("kind") == "pause" and step.get("rule_id") == "unrecoverable-blocker"
            for step in view["steps"]
        )

    tick_until(worker, world, paused, limit=50)

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["launch_count"] == 3
    assert attempt["cancelled_launch_count"] == 3
    assert attempt["accepted_report_count"] == 0
    assert attempt["state"] == "audit_ready"


@pytest.mark.parametrize(
    ("v_mode", "w_mode"),
    [
        ("fail", "ok"),
        ("skip", "ok"),
        ("other_attempt", "ok"),
        ("skip", "forge"),
    ],
)
def test_audit_stage_rejected_modes_nothing_settled(
    service, tmp_path: Path, monkeypatch, v_mode: str, w_mode: str
) -> None:
    """fail/skip/other_attempt, forge+skip -> nothing settled, failed; mission status, item unchanged (open)."""
    world, worker, work_id = _open(
        service,
        tmp_path / f"rejected-{v_mode}-{w_mode}",
        monkeypatch,
        verifier_mode=v_mode,
        worker_mode=w_mode,
    )

    def audit_outcome_recorded(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audit_outcome_recorded, limit=50)

    audit = _outcomes(world, "audit")[-1]
    assert audit["outcome"] == "failed"
    assert audit["verdict"] == "fail"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["accepted_report_count"] == 0
    assert attempts[0]["state"] != "audited"

    audit_receipts = _receipts(world, work_id, EvidenceKind.AUDIT.value)
    assert len(audit_receipts) == 0

    mission = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert mission["status"] == "running"

    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    assert item["state"] in ("BACKLOG", "OPEN", "IN_PROGRESS", "ACCEPTED")
    assert item["state"] not in ("DONE", "CANCELED", "CANCELLED")


def test_audit_stage_mismatched_signed_verdict_needs_fix_fails(
    service, tmp_path: Path, monkeypatch
) -> None:
    """When verifier signs PASS but report is NEEDS_FIX, settlement applies but audit fails."""
    world, worker, work_id = _open(
        service, tmp_path / "mismatch", monkeypatch, verifier_mode="pass"
    )

    real_sandbox = operations._sandbox

    def fake_sandbox(ctx, argv, worktree, **kwargs):
        code = real_sandbox(ctx, argv, worktree, **kwargs)
        if kwargs.get("identity") == "verifier":
            report_path = worktree / ".omp" / "audit-report.txt"
            if report_path.is_file():
                report_text = report_path.read_text(encoding="utf-8")
                report_path.write_text(
                    report_text.replace("VERDICT: PASS", "VERDICT: NEEDS_FIX"),
                    encoding="utf-8",
                )
        return code

    monkeypatch.setattr(operations, "_sandbox", fake_sandbox)

    def audit_outcome_recorded(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audit_outcome_recorded, limit=50)

    audit = _outcomes(world, "audit")[-1]
    assert audit["outcome"] == "failed"
    assert audit["verdict"] == "fail"
    assert audit.get("data", {}).get("code") == "verdict_needs_fix"

    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] == "remediation_required"

    audit_receipts = _receipts(world, work_id, EvidenceKind.AUDIT.value)
    assert len(audit_receipts) == 1
    assert audit_receipts[0]["verdict"] == "NEEDS_FIX"


def test_audit_stage_missing_evaluate_command_fails(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Missing evaluate command in prior data fails with evaluate_incomplete without running commands."""
    world, worker, work_id = _open(
        service, tmp_path / "eval-no-cmd", monkeypatch, verifier_mode="pass"
    )

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    steps = handler.steps()

    facts_prior = dict(_prior(steps))
    eval_outcome = facts_prior.get("evaluate")
    assert eval_outcome is not None
    bad_eval_data = dict(eval_outcome.get("data") or {})
    bad_eval_data.pop("command", None)
    facts_prior["evaluate"] = {**eval_outcome, "data": bad_eval_data}

    commands_called: list[str] = []
    ctx = StageContext(
        handler,
        step_index=len(steps),
        stage="audit",
        attempt=0,
        repair_round=0,
        request=handler._request,
        prior=facts_prior,
        lease=LeaseClaim(
            job_id=f"orch:{world.mission_id}:audit",
            worker_id="test-worker",
            fence=1,
        ),
        work_id=work_id,
    )

    def capture_command(name, payload, **kwargs):
        commands_called.append(name)
        raise AssertionError(f"command {name} should not be called when evaluate command missing")

    ctx.command = capture_command

    outcome = operations.run_audit(ctx)
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "evaluate_incomplete"
    assert commands_called == []


def test_audit_stage_missing_grant_issued_at_fails(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Missing grant issued_at fails with grant_issued_at_missing without begin_close_attempt."""
    world, worker, work_id = _open(
        service, tmp_path / "no-grant-issued", monkeypatch, verifier_mode="pass"
    )

    def pushed(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, pushed, limit=40)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    steps = handler.steps()

    facts_prior = dict(_prior(steps))
    facts_prior.pop("grant", None)

    commands_called: list[str] = []
    ctx = StageContext(
        handler,
        step_index=len(steps),
        stage="audit",
        attempt=0,
        repair_round=0,
        request=handler._request,
        prior=facts_prior,
        lease=LeaseClaim(
            job_id=f"orch:{world.mission_id}:audit",
            worker_id="test-worker",
            fence=1,
        ),
        work_id=work_id,
    )

    real_read = ctx.service.read

    def mock_read(principal, workspace_id, kind, value, **kwargs):
        view = real_read(principal, workspace_id, kind, value, **kwargs)
        if kind == "execution" and "grant" in view:
            view = dict(view)
            grant = dict(view["grant"])
            grant.pop("issued_at", None)
            grant.pop("provenance", None)
            view["grant"] = grant
        return view

    monkeypatch.setattr(ctx.service, "read", mock_read)

    def capture_command(name, payload, **kwargs):
        commands_called.append(name)
        raise AssertionError(f"command {name} should not be called when grant issued_at missing")

    ctx.command = capture_command

    outcome = operations.run_audit(ctx)
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "grant_issued_at_missing"
    assert "begin_close_attempt" not in commands_called
