"""Rollout faults for intake through signed merge (OMP-417-s07-s08).

dry_run writes nothing, and the disposable item reaches DONE. Each listed
fault except other_attempt completes with one remote update per ref. A stale
lease records one recovery step. A settle crash keeps one attempt. An
other_attempt verifier settles nothing. Without a recorded omp16-rollout, a
second mission reschedules even when max_workers is 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator import candidate_git, service as service_module
from omp_work.orchestrator.audit_chain import verify_step_chain
from omp_work.orchestrator.service import OrchestratorHandler, dry_run, load_config, status, submit
from omp_work.v1.models import EvidenceKind
from omp_work.v1.store_shared import WorkStoreError
from orchestrator_e2e_support import (
    E2EWorld,
    SimulatedCrash,
    answer_intake,
    crash_after_command,
    kill_first_run,
    open_e2e_world,
    open_worker,
    remote_updates,
    seed_published_budget,
    sign_merge_answer,
)
from test_jobs_worker_process import _config_dict, _env_for_child
from test_research_contract import _register_component
from test_workflow_service import OWNER, _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_MAIN = "refs/heads/main"
_VERIFIER_INTENT = re.compile(r"^verifier:[^:]+:2$")
_COUNT_TABLES = (
    "omp_jobs.jobs",
    "omp_work.work_items",
    "omp_work.project_missions",
    "omp_work.candidates",
    "omp_evidence.receipts",
    "omp_audit.domain_events",
)


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore service._REGISTRY after each test so later tests keep their ops."""
    saved = dict(service_module._REGISTRY)
    try:
        yield
    finally:
        service_module._REGISTRY.clear()
        service_module._REGISTRY.update(saved)


def _steps(world: E2EWorld) -> list[dict[str, Any]]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _outcomes(world: E2EWorld, stage: str) -> list[dict[str, Any]]:
    return [
        step
        for step in _steps(world)
        if step.get("stage") == stage and step.get("kind") == "outcome"
    ]


def _seed_item_budget(world: E2EWorld, service_fixture: Any, work_id: UUID) -> None:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)


def _retune(
    world: E2EWorld, *, lease_seconds: int | None = None, max_workers: int | None = None
) -> None:
    document = json.loads(world.config_path.read_text(encoding="utf-8"))
    document["wait_seconds"] = 0
    if lease_seconds is not None:
        document["lease_seconds"] = lease_seconds
    if max_workers is not None:
        document["max_workers"] = max_workers
    if not document.get("operations"):
        document["operations"] = ["omp_work.orchestrator.operations:register"]
    world.config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    world.config = load_config(world.config_path, world.service.config)


def _world(
    service_fixture: Any,
    root: Path,
    monkeypatch: Any,
    *,
    verifier_mode: str = "pass",
    worker_mode: str = "ok",
    lease_seconds: int | None = None,
    max_workers: int | None = None,
) -> E2EWorld:
    world = open_e2e_world(
        service_fixture,
        root,
        monkeypatch,
        verifier_mode=verifier_mode,
        worker_mode=worker_mode,
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    _retune(world, lease_seconds=lease_seconds, max_workers=max_workers)
    return world


def _submit(world: E2EWorld, service_fixture: Any) -> UUID:
    submitted = submit(world.config, world.request)
    assert submitted["status"] == "enqueued", submitted
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service_fixture, work_id)
    return work_id


def _answer_if_waiting(world: E2EWorld) -> None:
    try:
        mission = world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))
    except Exception:  # noqa: BLE001 - the mission row appears only after intake
        return
    if str(mission.get("status") or "") != "awaiting_confirmation":
        return
    intake = next(
        (
            step
            for step in _steps(world)
            if step.get("stage") == "intake" and step.get("kind") == "outcome"
        ),
        None,
    )
    if intake is None:
        return
    data = intake.get("data") or {}
    if not data.get("decision_id"):
        return
    world.intake_decision_id = UUID(str(data["decision_id"]))
    world.mission_revision = int(data["mission_revision"])
    answered = answer_intake(world, "edited_draft")
    assert answered["outcome"] == "approved"


def _sign_merge_if_open(world: E2EWorld, service_fixture: Any) -> None:
    steps = _steps(world)
    pause = next(
        (
            step
            for step in reversed(steps)
            if step.get("kind") == "pause" and step.get("rule_id") == "d41-merge-approval"
        ),
        None,
    )
    if pause is None or not pause.get("decision_id"):
        return
    decision_id = str(pause["decision_id"])
    record = OrchestratorHandler(world.config).find_decision(decision_id)
    if record is None or record.get("status") == "answered":
        return
    push_outcome = next(
        step for step in steps if step.get("stage") == "push" and step.get("kind") == "outcome"
    )
    commit = str(push_outcome["data"]["candidate_commit"])
    operation = {
        "kind": "git_push",
        "repository": world.repo_key,
        "branch": "main",
        "commit": commit,
    }
    digest = classify(
        parse_submission(operation),
        ResolvedTarget(repository=world.repository_record(), commit=commit, changed_paths=()),
    ).target_sha256
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=UUID(decision_id),
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=digest,
        expires_at=_FUTURE,
    )
    code, body = _command(
        service_fixture,
        world.workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": decision_id,
                "answer": "approve",
                "owner_signature": signature,
                "expires_at": _FUTURE.isoformat(),
            },
        },
    )
    assert code == 200, body


def _pump(
    worker: Any,
    world: E2EWorld,
    service_fixture: Any,
    *,
    limit: int = 120,
    stop_on_crash: bool = False,
) -> dict[str, Any]:
    """Tick until the mission completes. Answer intake and sign the merge."""
    view = status(world.config, world.mission_id)
    for _ in range(limit):
        if view.get("status") == "completed":
            return view
        _answer_if_waiting(world)
        _sign_merge_if_open(world, service_fixture)
        try:
            worker.tick()
        except SimulatedCrash:
            if stop_on_crash:
                return status(world.config, world.mission_id)
            raise
        view = status(world.config, world.mission_id)
    raise AssertionError(json.dumps({"view": view, "steps": _steps(world)}, default=str)[:4000])


def _ref_counts(world: E2EWorld) -> dict[str, int]:
    if not world._count_path.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in world._count_path.read_text(encoding="utf-8").split():
        counts[line] = counts.get(line, 0) + 1
    return counts


def _assert_one_update(world: E2EWorld, before: dict[str, int]) -> None:
    after = _ref_counts(world)
    deltas = {
        ref: count - before.get(ref, 0)
        for ref, count in after.items()
        if count != before.get(ref, 0)
    }
    assert deltas == {world.candidate_ref: 1, _MAIN: 1}, deltas
    assert remote_updates(world, world.candidate_ref) == before.get(world.candidate_ref, 0) + 1


def _assert_chain(world: E2EWorld) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        assert verify_step_chain(cur, world.workspace_id, str(world.mission_id)) == (True, None)


def _assert_done(world: E2EWorld, work_id: UUID) -> None:
    view = status(world.config, world.mission_id)
    assert view["status"] == "completed"
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    assert item["state"] == "DONE"


def _later(steps: list[dict[str, Any]], **match: str) -> dict[str, Any]:
    """A step of ``stage`` that matches and follows another step of that stage."""
    found = [
        index
        for index, step in enumerate(steps)
        if all(step.get(key) == value for key, value in match.items())
    ]
    assert found, match
    stage = match.get("stage")
    for index in found:
        if any(step.get("stage") == stage for step in steps[:index]):
            return steps[index]
    raise AssertionError(match)


def _rows(world: E2EWorld, sql: str, params: tuple) -> list[dict[str, Any]]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _attempts(world: E2EWorld, work_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        world,
        """
        SELECT attempt_id::text AS attempt_id, state, launch_count,
               cancelled_launch_count, accepted_report_count
        FROM omp_work.close_attempts
        WHERE workspace_id = %s AND work_id = %s
        ORDER BY requested_at, attempt_id
        """,
        (world.workspace_id, work_id),
    )


def _expire_lease(world: E2EWorld) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def _mission_status(world: E2EWorld) -> str | None:
    try:
        mission = world.store.read(world.workspace_id, OWNER, "mission", str(world.mission_id))
    except WorkStoreError:
        return None
    status_text = mission.get("status")
    return None if status_text is None else str(status_text)


def _snapshot(world: E2EWorld, root: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        for table in _COUNT_TABLES:
            row = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE workspace_id = %s",
                (world.workspace_id,),
            ).fetchone()
            counts[table] = int(row[0])
        events = conn.execute(
            """
            SELECT count(*) AS n
            FROM omp_jobs.job_events e
            JOIN omp_jobs.jobs j ON j.job_id = e.job_id
            WHERE j.workspace_id = %s
            """,
            (world.workspace_id,),
        ).fetchone()
        counts["omp_jobs.job_events"] = int(events[0])
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    main = subprocess.run(
        ["git", "-C", str(world.remote_repo), "rev-parse", _MAIN],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    push_count = (
        world._count_path.read_text(encoding="utf-8") if world._count_path.is_file() else ""
    )
    return {"counts": counts, "files": files, "main": main, "push_count": push_count}


def _finish(world: E2EWorld, service_fixture: Any, work_id: UUID, before: dict[str, int]) -> None:
    worker = open_worker(world)
    _pump(worker, world, service_fixture)
    _assert_done(world, work_id)
    _assert_one_update(world, before)
    _assert_chain(world)


def test_dry_run_writes_nothing_and_item_completes(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """dry_run leaves the database, files, and refs unchanged, then the item reaches DONE."""
    root = tmp_path / "dry"
    world = _world(service, root, monkeypatch)
    before_files = _snapshot(world, root)
    before_refs = _ref_counts(world)
    decided = dry_run(world.config, world.request)
    assert decided["kind"]
    assert _snapshot(world, root) == before_files
    work_id = _submit(world, service)
    _finish(world, service, work_id, before_refs)


def test_sigkill_mid_push_restarts_once(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """SIGKILL the jobs worker during the candidate push; the restart pushes that ref once."""
    lease_seconds = 15
    root = tmp_path / "sigkill"
    world = _world(service, root, monkeypatch, lease_seconds=lease_seconds)
    before_refs = _ref_counts(world)
    marker = root / "push-held"
    release = root / "push-release"
    finished = root / "push-finished"
    hook = world.remote_repo / "hooks" / "update"
    hook.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"ref=\"$1\"",
                f"marker='{marker}'",
                f"release='{release}'",
                f"count='{world._count_path}'",
                f"finished='{finished}'",
                f"candidate='{world.candidate_ref}'",
                'if [ "$ref" = "$candidate" ] && [ ! -f "$marker" ]; then',
                '  : > "$marker"',
                "  i=0",
                '  while [ ! -f "$release" ] && [ "$i" -lt 300 ]; do',
                "    sleep 0.1",
                "    i=$((i+1))",
                "  done",
                "fi",
                'echo "$ref" >> "$count"',
                'if [ "$ref" = "$candidate" ]; then',
                '  : > "$finished"',
                "fi",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    hook.chmod(0o755)
    work_id = _submit(world, service)
    component = _register_component(
        service,
        world.workspace_id,
        "worker",
        name=f"orch-{uuid4().hex[:8]}",
        capabilities=("omp.orchestrator",),
    )
    ops = _config_dict(service.config)
    body = {
        "workspace_id": str(world.workspace_id),
        "actor_id": str(OWNER),
        "worker_id": f"orch-{uuid4().hex[:8]}",
        "component_sha256": component,
        "capacity": 1024,
        "capabilities": ["omp.orchestrator"],
        "idle_sleep": 0.05,
        "operations": ops,
        "handlers": {
            "omp.orchestrator": {
                "factory": "omp_work.orchestrator.service:factory",
                "options": {"config_path": str(world.config_path), "operations": ops},
            }
        },
    }
    config_path = tmp_path / "worker.json"
    config_path.write_text(json.dumps(body), encoding="utf-8")
    env = _env_for_child()
    env["OMP_WORKTREES_DIR"] = str(world.worktrees_dir)
    first_log = tmp_path / "first.log"
    second_log = tmp_path / "second.log"
    first = _start_worker(config_path, env, first_log)
    second: subprocess.Popen | None = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if first.poll() is not None:
                raise AssertionError(
                    f"worker exited {first.returncode}: {first_log.read_text(encoding='utf-8')[-2000:]}"
                )
            _answer_if_waiting(world)
            if marker.is_file():
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"push hook never blocked: {first_log.read_text(encoding='utf-8')[-2000:]}"
            )
        first.kill()
        first.wait(timeout=5)
        release.write_text("go\n", encoding="utf-8")
        wait_hook = time.monotonic() + 15
        while time.monotonic() < wait_hook and not finished.is_file():
            time.sleep(0.1)
        time.sleep(lease_seconds + 3)
        restarted = time.monotonic() + 15
        while True:
            second = _start_worker(config_path, env, second_log)
            time.sleep(0.2)
            if second.poll() != 3:
                break
            second.wait(timeout=5)
            second = None
            if time.monotonic() >= restarted:
                raise AssertionError(
                    f"restart kept exiting 3: {second_log.read_text(encoding='utf-8')[-2000:]}"
                )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if second.poll() not in (None, 3):
                raise AssertionError(
                    f"restart exited {second.returncode}: {second_log.read_text(encoding='utf-8')[-2000:]}"
                )
            _sign_merge_if_open(world, service)
            if status(world.config, world.mission_id).get("status") == "completed":
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                f"mission did not complete: {second_log.read_text(encoding='utf-8')[-2000:]}"
            )
    finally:
        release.write_text("go\n", encoding="utf-8")
        _stop_worker(second)
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
    _assert_done(world, work_id)
    _assert_one_update(world, before_refs)
    _assert_chain(world)
    steps = _steps(world)
    recoveries = [step for step in steps if step.get("kind") == "recovery"]
    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery["stage"] == "push"
    assert recovery["resume"] == "recovery"
    assert recovery["rule_id"] == "lease-recovery"
    index = steps.index(recovery)
    assert any(
        step.get("kind") == "external_intent" and step.get("step_index") == recovery.get("step_index")
        for step in steps[:index]
    )


def test_kill_first_run_worker_retries(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A SIGKILL of the worker sandbox records one retry, then the mission completes."""
    world = _world(service, tmp_path / "worker-kill", monkeypatch)
    before_refs = _ref_counts(world)
    kill_first_run(monkeypatch, "worker")
    work_id = _submit(world, service)
    _finish(world, service, work_id, before_refs)
    steps = _steps(world)
    crashed = [
        step
        for step in steps
        if step.get("stage") == "implement" and step.get("kind") == "outcome" and step.get("outcome") == "crashed"
    ]
    assert len(crashed) == 1
    retry = _later(steps, kind="retry", stage="implement", resume="retry", rule_id="retry-policy")
    assert retry["resume"] == "retry"


def test_kill_first_run_verifier_records_second_intent(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A crashed verifier records external_intent verifier:<attempt>:2 and still merges."""
    world = _world(service, tmp_path / "verifier-kill", monkeypatch)
    before_refs = _ref_counts(world)
    kill_first_run(monkeypatch, "verifier")
    work_id = _submit(world, service)
    _finish(world, service, work_id, before_refs)
    intents = [
        step.get("external_ref")
        for step in _steps(world)
        if step.get("kind") == "external_intent"
    ]
    assert any(isinstance(ref, str) and _VERIFIER_INTENT.fullmatch(ref) for ref in intents)


def test_stale_lease_records_one_recovery(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """The first candidate push dies on a stale lease; one recovery step, then one push."""
    world = _world(service, tmp_path / "stale", monkeypatch)
    before_refs = _ref_counts(world)
    original = candidate_git.push
    calls = {"n": 0}

    def push(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _expire_lease(world)
            raise SimulatedCrash("stale-lease")
        return original(*args, **kwargs)

    monkeypatch.setattr(candidate_git, "push", push)
    work_id = _submit(world, service)
    worker = open_worker(world)
    _pump(worker, world, service, stop_on_crash=True)
    _expire_lease(world)
    _pump(worker, world, service)
    _assert_done(world, work_id)
    _assert_one_update(world, before_refs)
    _assert_chain(world)
    steps = _steps(world)
    recoveries = [step for step in steps if step.get("kind") == "recovery"]
    assert len(recoveries) == 1
    assert recoveries[0]["stage"] == "push"
    assert recoveries[0]["resume"] == "recovery"
    assert recoveries[0]["rule_id"] == "lease-recovery"
    assert calls["n"] == 2


def test_settle_crash_keeps_one_attempt(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A crash after settle_auditor_launch re-runs to PASS with one reserve and no cancel."""
    world = _world(service, tmp_path / "settle", monkeypatch)
    before_refs = _ref_counts(world)
    work_id = _submit(world, service)
    worker = open_worker(world)
    for _ in range(80):
        if _outcomes(world, "push"):
            break
        _answer_if_waiting(world)
        worker.tick()
    else:
        raise AssertionError("push did not finish")
    crash_after_command(monkeypatch, "settle_auditor_launch")
    with pytest.raises(SimulatedCrash):
        worker.tick()
    _expire_lease(world)
    _pump(worker, world, service)
    _assert_done(world, work_id)
    _assert_one_update(world, before_refs)
    _assert_chain(world)
    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["state"] in {"audited", "completed"}
    assert attempts[0]["launch_count"] == 1
    assert attempts[0]["accepted_report_count"] == 1
    assert attempts[0]["cancelled_launch_count"] == 0
    audit = _outcomes(world, "audit")[-1]
    assert audit["outcome"] == "succeeded"
    assert audit["verdict"] == "pass"


def test_other_attempt_settles_nothing(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A verifier bound to another attempt settles nothing and leaves the mission status."""
    world = _world(service, tmp_path / "other", monkeypatch, verifier_mode="other_attempt")
    work_id = _submit(world, service)
    worker = open_worker(world)
    before = None
    for _ in range(80):
        if _outcomes(world, "audit"):
            break
        before = _mission_status(world)
        _answer_if_waiting(world)
        worker.tick()
    else:
        raise AssertionError("audit outcome was not recorded")
    assert _mission_status(world) == before
    assert before == "running"
    attempts = _attempts(world, work_id)
    assert len(attempts) == 1
    assert attempts[0]["accepted_report_count"] == 0
    assert attempts[0]["state"] != "audited"
    receipts = _rows(
        world,
        """
        SELECT receipt_id FROM omp_evidence.receipts
        WHERE workspace_id = %s AND work_id = %s AND kind = %s
        """,
        (world.workspace_id, work_id, EvidenceKind.AUDIT.value),
    )
    assert receipts == []
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    assert item["state"] != "DONE"


def test_second_mission_reschedules_without_rollout(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """max_workers 4 without omp16-rollout still reschedules the second mission."""
    world = _world(service, tmp_path / "capacity", monkeypatch, max_workers=4)
    qualification = json.loads(world.config.qualification_path.read_text(encoding="utf-8"))
    assert "omp16-rollout" not in qualification["results"]
    assert world.config.max_workers == 4
    first = submit(world.config, world.request)
    assert first["status"] == "enqueued"
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            """
            UPDATE omp_jobs.jobs
            SET status='admitted',
                lease_expires_at = clock_timestamp() + interval '1 hour'
            WHERE workspace_id=%s AND job_id=%s
            """,
            (world.workspace_id, first["job_id"]),
        )
    new_id = uuid4()
    second_request = replace(
        world.request,
        mission_id=new_id,
        candidate_ref=f"refs/heads/omp/{new_id.hex[:12]}",
        work=world.request.work.model_copy(
            update={"client_ref": f"m-{new_id.hex[:12]}", "title": "Second mission"}
        ),
    )
    second = submit(world.config, second_request)
    assert second["status"] == "enqueued"
    worker = open_worker(world)
    assert worker.tick() == second["job_id"]
    handler = OrchestratorHandler(world.config)
    handler.mission_id = new_id
    rules = [step.get("rule_id") for step in handler.steps()]
    assert "reschedule-capacity" in rules


def _start_worker(config_path: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(config_path)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    handle.close()
    return process


def _stop_worker(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
