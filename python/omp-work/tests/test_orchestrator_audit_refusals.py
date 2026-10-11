"""Postgres tests for orchestrator audit refusals (OMP-417-s07-s06-s03).

Refusal branches:
- verifier_mode "fail" (signed fail, exit 1)
- verifier_mode "skip" (exit 0, no verdict)
- other_attempt: test-local verifier script signs pass with attempt_id "a-other", exit 0
- worker_mode "forge" + verifier_mode "skip" (candidate carries forged .omp/verdict.json,
  so the host refuses the committed transport before any verifier launch)
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.orchestrator import operations, service, worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler, status, submit
from omp_work.v1.models import EvidenceKind
from orchestrator_e2e_support import (
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

_OTHER_ATTEMPT_VERIFIER_SCRIPT = r"""
import hashlib, hmac, json, sys
from pathlib import Path

task_path = Path(".omp/verify-task.json")
task = json.loads(task_path.read_text(encoding="utf-8"))
payload = {
    "candidate_sha256": task["candidate_sha256"],
    "attempt_id": "a-other",
    "verdict": "pass",
    "verifier_id": task["verifier_id"],
}
raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
key = Path(task["key_path"]).read_bytes()
signature = hmac.new(key, raw, hashlib.sha256).hexdigest()

out = Path(".omp")
out.mkdir(parents=True, exist_ok=True)
(out / "verdict.json").write_text(
    json.dumps({**payload, "signature": signature}), encoding="utf-8"
)
(out / "audit-report.txt").write_text(
    "VERDICT: PASS\nFINDINGS\n(none)\nCHECKS RUN\nautomated_test -> exit 0\n",
    encoding="utf-8",
)
sys.exit(0)
"""


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


@pytest.mark.parametrize(
    ("branch", "v_mode", "w_mode"),
    [
        ("fail", "fail", "ok"),
        ("skip", "skip", "ok"),
        ("other_attempt", "other_attempt", "ok"),
        ("forge_skip", "skip", "forge"),
    ],
)
def test_orchestrator_audit_refusals(
    service, tmp_path: Path, monkeypatch, branch: str, v_mode: str, w_mode: str
) -> None:
    """Each refusal branch fails audit, settles nothing, cancels launch, and leaves mission/item unchanged."""
    verifier_runs: list[dict[str, Any]] = []
    real_run_worker = worker_sandbox.run_worker

    def recording_run_worker(argv, **kwargs):
        rc = real_run_worker(argv, **kwargs)
        if kwargs.get("identity") == "verifier":
            verifier_runs.append({
                "argv": list(argv),
                "exit_code": rc,
                "worktree": Path(kwargs["worktree"]),
                "kwargs": kwargs,
            })
        return rc

    monkeypatch.setattr(worker_sandbox, "run_worker", recording_run_worker)

    root = tmp_path / f"refusal-{branch}"
    world = open_e2e_world(
        service,
        root,
        monkeypatch,
        verifier_mode="pass" if branch == "other_attempt" else v_mode,
        worker_mode=w_mode,
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )

    if branch == "other_attempt":
        world.verifier_mode = "other_attempt_pass"
        world.verifier_argv = ("/usr/bin/python3", "-c", _OTHER_ATTEMPT_VERIFIER_SCRIPT)
        world.config = dataclasses.replace(world.config, verifier_argv=world.verifier_argv)
        document = json.loads(world.config_path.read_text(encoding="utf-8"))
        document["verifier_argv"] = list(world.verifier_argv)
        world.config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    # 1. Tick until push succeeds, capture status before audit begins.
    def push_done(_view: dict) -> bool:
        return any(step.get("outcome") == "succeeded" for step in _outcomes(world, "push"))

    tick_until(worker, world, push_done, limit=50)

    mission_status_before = status(world.config, world.mission_id)["status"]
    mission_store_before = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )["status"]

    # 2. Tick until audit step records an outcome.
    def audit_done(_view: dict) -> bool:
        return bool(_outcomes(world, "audit"))

    tick_until(worker, world, audit_done, limit=50)

    # 3. Audit outcome must be failed with verdict fail.
    audit_outcome = _outcomes(world, "audit")[-1]
    assert audit_outcome["outcome"] == "failed"
    assert audit_outcome["verdict"] == "fail"

    # 4. Zero audit receipts.
    audit_receipts = _receipts(world, work_id, EvidenceKind.AUDIT.value)
    assert len(audit_receipts) == 0

    receipt_count = _rows(
        world,
        "SELECT count(*) AS c FROM omp_evidence.receipts WHERE workspace_id = %s AND work_id = %s AND kind = %s",
        (world.workspace_id, work_id, EvidenceKind.AUDIT.value),
    )[0]["c"]
    assert receipt_count == 0

    # 5. No settled launch; >=1 cancelled launch.
    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["accepted_report_count"] == 0
    assert attempt["cancelled_launch_count"] >= 1
    assert attempt["state"] != "audited"

    settled_events = _rows(
        world,
        "SELECT event_id::text AS event_id FROM omp_work.close_attempt_events "
        "WHERE workspace_id = %s AND work_id = %s AND event_type = 'auditor_launch_settled'",
        (world.workspace_id, work_id),
    )
    assert len(settled_events) == 0

    cancelled_events = _rows(
        world,
        "SELECT event_id::text AS event_id, launch_id::text AS launch_id FROM omp_work.close_attempt_events "
        "WHERE workspace_id = %s AND work_id = %s AND event_type = 'auditor_launch_cancelled'",
        (world.workspace_id, work_id),
    )
    assert len(cancelled_events) >= 1

    launches = _launches(world, work_id)
    assert len(launches) >= 1

    # 6. Mission status equals its value before audit.
    mission_status_after = status(world.config, world.mission_id)["status"]
    assert mission_status_after == mission_status_before

    mission_store_after = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )["status"]
    assert mission_store_after == mission_store_before

    # 7. Item stays open.
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    assert item["state"] in ("BACKLOG", "OPEN", "IN_PROGRESS", "ACCEPTED")
    assert item["state"] not in ("DONE", "CANCELED", "CANCELLED")

    item_rows = _rows(
        world,
        "SELECT work_id::text AS work_id, state FROM omp_work.work_items WHERE workspace_id = %s AND work_id = %s",
        (world.workspace_id, work_id),
    )
    assert len(item_rows) == 1
    assert item_rows[0]["state"] in ("BACKLOG", "OPEN", "IN_PROGRESS", "ACCEPTED")
    assert item_rows[0]["state"] not in ("DONE", "CANCELED", "CANCELLED")

    # 8. Branch-specific verifier checks.
    if branch == "forge_skip":
        # The candidate commit carries a forged .omp/verdict.json, so the host
        # refuses the committed transport before any verifier process launches.
        assert len(verifier_runs) == 0
        assert audit_outcome["data"].get("code") == "committed_transport"
        candidate_commit = audit_outcome["data"]["candidate_commit"]
        proc = subprocess.run(
            ["git", "-C", str(world.control_repo), "show", f"{candidate_commit}:.omp/verdict.json"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        forged_doc = json.loads(proc.stdout)
        assert forged_doc["attempt_id"] == "a-forged"
        return

    assert len(verifier_runs) == 1
    v_run = verifier_runs[0]
    v_worktree = v_run["worktree"]
    verdict_path = v_worktree / ".omp" / "verdict.json"

    if branch == "fail":
        assert v_run["exit_code"] == 1
        assert verdict_path.is_file()
        verdict_doc = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert verdict_doc["verdict"] == "fail"
    elif branch == "skip":
        assert v_run["exit_code"] == 0
        assert not verdict_path.is_file()
    elif branch == "other_attempt":
        # Proves refusal is attempt binding: exit 0 and verdict "pass".
        assert v_run["exit_code"] == 0
        assert verdict_path.is_file()
        verdict_doc = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert verdict_doc["verdict"] == "pass"
        assert verdict_doc["attempt_id"] == "a-other"
        assert (v_worktree / ".omp" / "audit-report.txt").is_file()
        assert "VERDICT: PASS" in (v_worktree / ".omp" / "audit-report.txt").read_text(encoding="utf-8")
