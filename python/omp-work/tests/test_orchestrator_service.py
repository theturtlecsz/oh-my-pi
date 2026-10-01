"""Orchestrator submit, stage loop, pauses, and qualification (OMP-417-s06)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import JobWorker, WorkerConfig
from omp_work.orchestrator.service import (
    OrchestratorError,
    OrchestratorHandler,
    dry_run,
    load_config,
    load_request,
    qualify,
    request_terminal,
    status,
    submit,
)
from omp_work.v1.canonical import text_sha256
from omp_work.v1.decision_records import find_decision
from omp_work.v1.models import (
    BoundedIntakeDraft,
    CommandEnvelope,
    CreateWorkInput,
    MissionIntakeScope,
    OwnerInstruction,
)
from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message
from omp_work.v1.store import PostgresWorkStore
from test_research_contract import _register_component
from test_workflow_service import OWNER, _command, _grant, _seed_project

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_RELEASE = "r1"
_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_BUDGET = {"usd": "10", "tokens": 1000, "wall_clock_seconds": 3600, "max_subagents": 2}
_SCOPES = ["work.read", "work.mutate", "work.approve", "work.close", "work.execute"]


def _checks(*, kill: bool = True, locks: bool = False) -> dict:
    results: dict[str, dict[str, str]] = {}
    if kill:
        results["jobs-worker-kill-restart"] = {
            "item": "kill",
            "test": "t",
            "result": "pass",
            "release": _RELEASE,
        }
    if locks:
        for number in range(1, 13):
            key = f"lock-{number}"
            results[key] = {"item": key, "test": "t", "result": "pass", "release": _RELEASE}
    return {"release": _RELEASE, "results": results}


def _mission_request(
    mission_id: UUID,
    project_id: UUID,
    *,
    unattended: bool = False,
    controls: dict | None = None,
):
    text = "Ship the orchestrated change"
    return {
        "mission_id": str(mission_id),
        "project_id": str(project_id),
        "unattended": unattended,
        "intake": {
            "source": {"text": text, "sha256": text_sha256(text)},
            "goal": {"id": "goal", "statement": text},
        },
        "scope": {
            "project_id": str(project_id),
            "risk_policy": "risk",
            "approval_policy": "approval",
            "effort_policy": "effort",
            "budget_policy": _BUDGET,
            "kind": "engineering.execute",
        },
        "instruction": {
            "text": "Ship it",
            "provenance": {
                "channel": "test",
                "message_ref": "m1",
                "received_at": "2026-10-01T00:00:00+00:00",
            },
        },
        "work": {
            "client_ref": f"m-{mission_id.hex[:12]}",
            "title": "Orchestrated work",
            "project_id": str(project_id),
        },
        "controls": controls or {},
    }


def _write_config(
    root: Path,
    service,
    workspace_id: UUID,
    qualification: dict,
    *,
    lease_seconds: int = 30,
    wait_seconds: int = 0,
) :
    automation = root / "automation.json"
    owner = root / "owner.json"
    qualification_path = root / "qualification.json"
    automation.write_text(
        json.dumps(
            {
                "actor_id": str(OWNER),
                "actor_kind": "automation",
                "workspaces": [str(workspace_id)],
                "scopes": _SCOPES,
            }
        )
    )
    owner.write_text(
        json.dumps(
            {
                "actor_id": str(OWNER),
                "actor_kind": "owner",
                "workspaces": [str(workspace_id)],
                "scopes": _SCOPES,
            }
        )
    )
    qualification_path.write_text(json.dumps(qualification))
    for name in ("lock-map.json", "verifier.key", "signers"):
        (root / name).write_text("")
    for name in ("repo", "worktrees", "live"):
        (root / name).mkdir()
    path = root / "orchestrator.json"
    path.write_text(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "automation_capability_path": str(automation),
                "qualification_path": str(qualification_path),
                "lock_map_path": str(root / "lock-map.json"),
                "verifier_key_path": str(root / "verifier.key"),
                "allowed_signers": str(root / "signers"),
                "control_repo": str(root / "repo"),
                "worktrees_dir": str(root / "worktrees"),
                "live_checkout": str(root / "live"),
                "repair_rounds": 3,
                "max_workers": 1,
                "lease_seconds": lease_seconds,
                "wait_seconds": wait_seconds,
                "operations": ["orchestrator_test_ops:register"],
            }
        )
    )
    return load_config(path, service.config), path


def _workspace(service) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    _seed_project(service, workspace_id, project_id)
    return workspace_id, project_id


def open_world(
    service,
    tmp_path: Path,
    *,
    controls: dict | None = None,
    unattended: bool = False,
    qualification: dict | None = None,
    lease_seconds: int = 30,
    wait_seconds: int = 0,
):
    """One workspace, its orchestrator config, and a parsed mission request."""
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    workspace_id, project_id = _workspace(service)
    mission_id = uuid4()
    config, path = _write_config(
        tmp_path,
        service,
        workspace_id,
        qualification if qualification is not None else _checks(),
        lease_seconds=lease_seconds,
        wait_seconds=wait_seconds,
    )
    document = _mission_request(
        mission_id, project_id, unattended=unattended, controls=controls
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(document))
    request = load_request(request_path)
    return {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "mission_id": mission_id,
        "config": config,
        "config_path": path,
        "request": request,
        "request_path": request_path,
    }


def _worker(service, config, workspace_id: UUID) -> JobWorker:
    component = _register_component(
        service,
        workspace_id,
        "worker",
        name=f"orch-{uuid4().hex[:8]}",
        capabilities=("omp.orchestrator",),
    )
    worker = JobWorker(
        NativeJobStore(service.config),
        WorkerConfig(
            workspace_id=workspace_id,
            actor_id=OWNER,
            worker_id=f"orch-{uuid4().hex[:8]}",
            component_sha256=component,
            capabilities=["omp.orchestrator"],
            capacity=1024,
        ),
        OrchestratorHandler(config),
    )
    worker.start()
    return worker


def _view(config, mission_id: UUID) -> dict:
    return status(config, mission_id)


def _until(worker: JobWorker, config, mission_id: UUID, predicate, limit: int = 80) -> dict:
    view = _view(config, mission_id)
    for _ in range(limit):
        if predicate(view):
            return view
        worker.tick()
        view = _view(config, mission_id)
    raise AssertionError(json.dumps(view, default=str)[:4000])


def _rows(service, sql: str, params: tuple) -> list:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _decision_count(service, workspace_id: UUID) -> int:
    rows = _rows(
        service,
        """
        SELECT count(*) FROM omp_audit.domain_events
        WHERE workspace_id=%s AND event_type='create_decision' AND outcome='applied'
        """,
        (workspace_id,),
    )
    return int(rows[0][0])


def _orch_jobs(service, workspace_id: UUID) -> list[dict]:
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        return list(
            conn.execute(
                """
                SELECT job_id, status, worker_id, fence
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND job_id LIKE 'orch:%%'
                """,
                (workspace_id,),
            ).fetchall()
        )


def _job(service, workspace_id: UUID, job_id: str) -> dict | None:
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        return conn.execute(
            """
            SELECT status, worker_id, fence
            FROM omp_jobs.jobs
            WHERE workspace_id=%s AND job_id=%s
            """,
            (workspace_id, job_id),
        ).fetchone()


def _steps(config, mission_id: UUID) -> list[dict]:
    handler = OrchestratorHandler(config)
    handler.mission_id = mission_id
    return handler.steps()


def _decision(service, workspace_id: UUID, decision_id: str) -> dict:
    store = NativeJobStore(service.config)
    with store.transaction(workspace_id, OWNER) as cur:
        record = find_decision(cur, workspace_id, decision_id)
    assert record is not None
    return record


def _answer(service, workspace_id: UUID, decision_id: str, answer: str, **extra) -> None:
    status_code, body = _command(
        service,
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {"decision_id": decision_id, "answer": answer, **extra},
        },
    )
    assert status_code == 200, body


def _generate_key(tmp_path: Path) -> Path:
    key = tmp_path / "owner-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _approve(
    service,
    tmp_path: Path,
    workspace_id: UUID,
    decision_id: str,
    *,
    action_class: str,
    target_sha256: str | None,
) -> None:
    key = _generate_key(tmp_path)
    public = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
    signers = service.config.config_dir / "owner_allowed_signers"
    signers.write_text(f"owner {public}\n", encoding="utf-8")
    expires = _FUTURE if target_sha256 is not None else None
    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=UUID(decision_id),
        action_class=action_class,
        answer="approve",
        target_sha256=target_sha256,
        expires_at=expires,
    )
    extra = {"owner_signature": _sign(key, message)}
    if expires is not None:
        extra["expires_at"] = expires.isoformat()
    _answer(service, workspace_id, decision_id, "approve", **extra)


def _pause_id(view: dict) -> str:
    pauses = [step for step in view["steps"] if step.get("decision_id")]
    assert len(pauses) == 1, view
    return str(pauses[0]["decision_id"])


def _resume_after_pause(service, tmp_path: Path, world: dict, worker: JobWorker, *, signed: bool) -> dict:
    view = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] == "paused",
    )
    assert _decision_count(service, world["workspace_id"]) == 1
    decision_id = _pause_id(view)
    if signed:
        record = _decision(service, world["workspace_id"], decision_id)
        _approve(
            service,
            tmp_path,
            world["workspace_id"],
            decision_id,
            action_class=str(record["action_class"]),
            target_sha256=None if record.get("target_sha256") is None else str(record["target_sha256"]),
        )
    else:
        _answer(service, world["workspace_id"], decision_id, "resume")
    completed = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] == "completed",
    )
    assert _decision_count(service, world["workspace_id"]) == 1
    return view | {"completed": completed}


def _stop(service, workspace_id: UUID, command_type: str, actor_kind: str, scope: str) -> None:
    store = PostgresWorkStore(service.config)
    store.execute(
        CommandEnvelope.model_validate(
            {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(workspace_id),
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "command": {"type": command_type, "payload": {"reason": command_type}},
            }
        ),
        actor_id=OWNER,
        actor_kind=actor_kind,
        required_scope=scope,
    )


def test_dry_run_and_qualify_cli(service, tmp_path: Path, capsys) -> None:
    passing = tmp_path / "pass_check.py"
    failing = tmp_path / "fail_check.py"
    passing.write_text("def test_ok():\n    assert True\n")
    failing.write_text("def test_bad():\n    assert False\n")
    lock_map = tmp_path / "locks.json"
    lock_map.write_text(
        json.dumps(
            {
                "release": _RELEASE,
                "locks": {
                    "jobs-worker-kill-restart": {"item": "kill", "test": str(passing)},
                    "lock-1": {"item": "lock-1", "test": str(failing)},
                },
            }
        )
    )
    out = tmp_path / "qualification-out.json"
    recorded = qualify(lock_map, out, release=_RELEASE)
    assert recorded["results"]["jobs-worker-kill-restart"]["result"] == "pass"
    assert recorded["results"]["lock-1"]["result"] == "fail"
    assert json.loads(out.read_text())["results"]["lock-1"]["result"] == "fail"

    from omp_work.__main__ import main as omp_main

    cli_out = tmp_path / "cli-out.json"
    assert (
        omp_main(
            [
                "orchestrator",
                "qualify",
                "--lock-map",
                str(lock_map),
                "--out",
                str(cli_out),
                "--release",
                _RELEASE,
            ]
        )
        == 0
    )
    cli_body = json.loads(capsys.readouterr().out)
    assert cli_body["results"]["jobs-worker-kill-restart"]["result"] == "pass"
    assert cli_body["results"]["lock-1"]["result"] == "fail"

    world = open_world(service, tmp_path / "world")
    assert _decision_count(service, world["workspace_id"]) == 0
    assert _orch_jobs(service, world["workspace_id"]) == []
    preview = dry_run(world["config"], world["request"])
    assert preview["kind"] == "dispatch"
    assert preview["rule_id"] == "stage-dispatch"
    assert preview["decision_id"] is None
    assert _orch_jobs(service, world["workspace_id"]) == []
    assert (
        omp_main(
            [
                "orchestrator",
                "dry-run",
                "--config",
                str(world["config_path"]),
                "--request",
                str(world["request_path"]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["kind"] == "dispatch"
    assert _orch_jobs(service, world["workspace_id"]) == []


def test_stub_ops_reach_close_with_rule_ids_and_no_decision(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    submitted = submit(world["config"], world["request"])
    assert submitted["status"] == "enqueued"
    assert submitted["job_id"] == f"orch:{world['mission_id']}:0"
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] == "completed",
    )
    assert view["steps"]
    assert all(step.get("rule_id") for step in view["steps"])
    assert all(step.get("decision_id") is None for step in view["steps"])
    assert any(step["kind"] == "end" and step["rule_id"] == "mission-complete" for step in view["steps"])
    assert any(step["kind"] == "outcome" and step["stage"] == "close" for step in view["steps"])
    assert _decision_count(service, world["workspace_id"]) == 0


@pytest.mark.parametrize(
    ("controls", "rule_id", "signed"),
    [
        (
            {"push": {"once": True, "action": {"tier": 2, "action_class": "push_branch", "allowed": False, "code": "no_policy"}}},
            "d35-tier2-no-policy",
            False,
        ),
        (
            {"grant": {"once": True, "action": {"tier": 3, "action_class": "unlisted", "allowed": False, "code": "held"}}},
            "d35-tier3",
            True,
        ),
        ({"plan": {"once": True, "new_scope": True}}, "d21-new-scope", False),
        ({"implement": {"once": True, "budget_exhausted": True}}, "budget-exhausted", False),
        ({"evaluate": {"once": True, "blocker": "needs-owner"}}, "unrecoverable-blocker", False),
        ({"plan": {"fails": 1}}, "retry-bound", False),
        ({"evaluate": {"fails": 4}}, "repair-bound", False),
    ],
)
def test_stop_condition_pauses_once_then_resumes(
    service, tmp_path: Path, controls: dict, rule_id: str, signed: bool
) -> None:
    world = open_world(service, tmp_path, controls=controls)
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    paused = _resume_after_pause(service, tmp_path, world, worker, signed=signed)
    assert paused["status"] == "paused"
    pauses = [step for step in paused["steps"] if step["kind"] == "pause"]
    assert [step["rule_id"] for step in pauses] == [rule_id]
    assert paused["completed"]["status"] == "completed"


def test_retry_reroute_and_reschedule_record_rules_without_a_decision(service, tmp_path: Path) -> None:
    retry = open_world(service, tmp_path / "retry", controls={"implement": {"fails": 1}})
    submit(retry["config"], retry["request"])
    retry_view = _until(
        _worker(service, retry["config"], retry["workspace_id"]),
        retry["config"],
        retry["mission_id"],
        lambda item: item["status"] == "completed",
    )
    assert any(step["rule_id"] == "retry-policy" for step in retry_view["steps"])
    assert all(step.get("decision_id") is None for step in retry_view["steps"])
    assert _decision_count(service, retry["workspace_id"]) == 0

    reroute = open_world(service, tmp_path / "reroute", controls={"implement": {"fails": 3}})
    submit(reroute["config"], reroute["request"])
    reroute_view = _until(
        _worker(service, reroute["config"], reroute["workspace_id"]),
        reroute["config"],
        reroute["mission_id"],
        lambda item: item["status"] == "completed",
    )
    rules = {step["rule_id"] for step in reroute_view["steps"]}
    assert {"retry-policy", "reroute-alternate"} <= rules
    assert all(step.get("decision_id") is None for step in reroute_view["steps"])
    assert _decision_count(service, reroute["workspace_id"]) == 0

    reschedule = open_world(service, tmp_path / "reschedule")
    submitted = submit(reschedule["config"], reschedule["request"])
    blocker = f"blocker-{uuid4()}"
    enqueue_job(
        NativeJobStore(service.config),
        operation_id=str(uuid4()),
        workspace_id=reschedule["workspace_id"],
        actor_id=OWNER,
        job_id=blocker,
        work_id=UUID(submitted["work_id"]),
        kind="compute",
        required_capabilities=["omp.orchestrator"],
        resources={"cpu": 1, "memory_mib": 1, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
    )
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status='admitted' WHERE workspace_id=%s AND job_id=%s",
            (reschedule["workspace_id"], blocker),
        )
    worker = _worker(service, reschedule["config"], reschedule["workspace_id"])
    assert worker.tick() == submitted["job_id"]
    waiting = _view(reschedule["config"], reschedule["mission_id"])
    assert any(step["rule_id"] == "reschedule-capacity" for step in waiting["steps"])
    assert all(step.get("decision_id") is None for step in waiting["steps"])
    assert _decision_count(service, reschedule["workspace_id"]) == 0
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status='cancelled' WHERE workspace_id=%s AND job_id=%s",
            (reschedule["workspace_id"], blocker),
        )
    finished = _until(
        worker,
        reschedule["config"],
        reschedule["mission_id"],
        lambda item: item["status"] == "completed",
    )
    assert finished["status"] == "completed"
    assert _decision_count(service, reschedule["workspace_id"]) == 0


def test_terminal_without_basis_or_standing_policy_pauses_and_an_answer_abandons(
    service, tmp_path: Path
) -> None:
    standing = open_world(service, tmp_path / "standing")
    submit(standing["config"], standing["request"])
    request_terminal(standing["config"], standing["mission_id"], "abandon", basis=str(uuid4()))
    worker = _worker(service, standing["config"], standing["workspace_id"])
    paused = _until(
        worker,
        standing["config"],
        standing["mission_id"],
        lambda item: item["status"] == "paused",
    )
    assert [step["rule_id"] for step in paused["steps"] if step["kind"] == "pause"] == ["terminal-needs-basis"]
    assert paused["status"] == "paused"
    assert _decision_count(service, standing["workspace_id"]) == 1

    answered = open_world(service, tmp_path / "answered")
    submit(answered["config"], answered["request"])
    classified = classify(
        parse_submission({"kind": "mission_abandon", "resource_id": str(answered["mission_id"])}),
        ResolvedTarget(),
    )
    decision_id = uuid4()
    created, body = _command(
        service,
        answered["workspace_id"],
        {
            "type": "create_decision",
            "payload": {
                "decision_id": str(decision_id),
                "project_id": str(answered["project_id"]),
                "mission_id": str(answered["mission_id"]),
                "question": "Abandon this mission?",
                "why_it_matters": "The owner asked to end it.",
                "risk_of_delay": "The mission keeps running.",
                "options": ["approve", "decline"],
                "risk_of_each_choice": {
                    "approve": "The mission is abandoned.",
                    "decline": "The mission stays.",
                },
                "default_if_any": "decline",
                "action_class": classified.action_class,
                "target_sha256": classified.target_sha256,
                "resume_state": "intake",
            },
        },
    )
    assert created == 200, body
    _approve(
        service,
        tmp_path / "answered",
        answered["workspace_id"],
        str(decision_id),
        action_class=classified.action_class,
        target_sha256=classified.target_sha256,
    )
    request_terminal(answered["config"], answered["mission_id"], "abandon", basis=str(decision_id))
    worker = _worker(service, answered["config"], answered["workspace_id"])
    abandoned = _until(
        worker,
        answered["config"],
        answered["mission_id"],
        lambda item: item["status"] == "abandoned",
    )
    assert any(step["rule_id"] == "terminal-with-basis" for step in abandoned["steps"])
    assert not any(step["kind"] == "pause" for step in abandoned["steps"])
    assert _decision_count(service, answered["workspace_id"]) == 1


def test_terminal_without_a_basis_pauses_until_the_owner_approves(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    submit(world["config"], world["request"])
    request_terminal(world["config"], world["mission_id"], "abandon")
    worker = _worker(service, world["config"], world["workspace_id"])
    paused = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] == "paused",
    )
    assert paused["status"] == "paused"
    assert [step["rule_id"] for step in paused["steps"] if step["kind"] == "pause"] == ["terminal-needs-basis"]
    assert _decision_count(service, world["workspace_id"]) == 1
    decision_id = _pause_id(paused)
    record = _decision(service, world["workspace_id"], decision_id)
    _approve(
        service,
        tmp_path,
        world["workspace_id"],
        decision_id,
        action_class=str(record["action_class"]),
        target_sha256=str(record["target_sha256"]),
    )
    abandoned = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] == "abandoned",
    )
    assert abandoned["status"] == "abandoned"
    assert _decision_count(service, world["workspace_id"]) == 1


def test_stop_mid_stage_keeps_the_lease_until_release(service, tmp_path: Path) -> None:
    hold = tmp_path / "hold"
    effect = tmp_path / "effect"
    hold.write_text("hold")
    world = open_world(
        service,
        tmp_path / "world",
        controls={"intake": {"hold": str(hold), "effect": str(effect)}},
        lease_seconds=30,
    )
    submitted = submit(world["config"], world["request"])
    started = OrchestratorHandler(world["config"])
    started.mission_id = world["mission_id"]
    started._bind(world["mission_id"])
    baseline = started._mission()
    assert baseline["status"] == "running"
    worker = _worker(service, world["config"], world["workspace_id"])
    box: dict = {}

    def _tick() -> None:
        try:
            box["job"] = worker.tick()
        except Exception as exc:  # noqa: BLE001 - the assertion reports the worker error
            box["error"] = exc

    thread = threading.Thread(target=_tick)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        row = None
        while time.monotonic() < deadline:
            row = _job(service, world["workspace_id"], submitted["job_id"])
            if row is not None and row["status"] in ("admitted", "in_flight"):
                break
            time.sleep(0.05)
        assert row is not None and row["status"] in ("admitted", "in_flight"), row
        _stop(service, world["workspace_id"], "engage_stop", "client", "work.stop")
        hold.unlink()
        thread.join(15)
    finally:
        if hold.exists():
            hold.unlink()
        thread.join(15)
    assert "error" not in box, box.get("error")
    frozen = _job(service, world["workspace_id"], submitted["job_id"])
    assert frozen is not None
    assert frozen["status"] == row["status"]
    assert frozen["worker_id"] == row["worker_id"]
    assert frozen["fence"] == row["fence"]
    assert frozen["status"] != "sealed"
    assert not effect.exists()
    during = OrchestratorHandler(world["config"])
    during.mission_id = world["mission_id"]
    during._bind(world["mission_id"])
    current = during._mission()
    assert current["status"] == baseline["status"] == "running"
    assert current["objective"] == baseline["objective"]
    assert current["approved_scope"] == baseline["approved_scope"]
    assert all(
        not (isinstance(step.get("data"), dict) and step["data"].get("candidate_commit"))
        for step in _steps(world["config"], world["mission_id"])
    )
    _stop(service, world["workspace_id"], "release_stop", "owner", "work.approve")
    assert worker.tick() == submitted["job_id"]
    assert effect.read_text(encoding="utf-8") == "1"
    released = OrchestratorHandler(world["config"])
    released.mission_id = world["mission_id"]
    released._bind(world["mission_id"])
    after = released._mission()
    assert after["objective"] == baseline["objective"]
    assert after["approved_scope"] == baseline["approved_scope"]
    assert effect.read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize("mode", ["expired", "reassigned"])
def test_act_under_a_lost_lease_is_refused(service, tmp_path: Path, mode: str) -> None:
    effect = tmp_path / "effect"
    world = open_world(
        service,
        tmp_path / "world",
        controls={"intake": {"effect": str(effect)}},
    )
    submitted = submit(world["config"], world["request"])
    worker_id = "other-worker" if mode == "reassigned" else "holder"
    lease = "clock_timestamp() - interval '5 seconds'" if mode == "expired" else "clock_timestamp() + interval '1 hour'"
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            f"""
            UPDATE omp_jobs.jobs
            SET status='admitted', worker_id=%s, fence=1, lease_expires_at={lease}
            WHERE workspace_id=%s AND job_id=%s
            """,
            (worker_id, world["workspace_id"], submitted["job_id"]),
        )
    handler = OrchestratorHandler(world["config"])
    handler.run(
        None,
        {
            "job_id": submitted["job_id"],
            "work_id": submitted["work_id"],
            "worker_id": "holder",
            "fence": 1,
        },
    )
    steps = _steps(world["config"], world["mission_id"])
    assert any(step.get("kind") == "action_refused" and step.get("code") == "lease_lost" for step in steps)
    assert not effect.exists()


@pytest.mark.parametrize("unattended", [False, True])
def test_missing_kill_restart_refuses_submit(service, tmp_path: Path, unattended: bool) -> None:
    world = open_world(
        service,
        tmp_path,
        unattended=unattended,
        qualification=_checks(kill=False),
    )
    refused = submit(world["config"], world["request"])
    assert refused["status"] == "refused"
    assert refused["code"] == "release-qualification"
    assert refused["job_id"] is None
    assert refused["decision_id"]
    assert _decision_count(service, world["workspace_id"]) == 1
    assert _orch_jobs(service, world["workspace_id"]) == []


def test_unattended_missing_lock_refuses_with_a_decision(service, tmp_path: Path) -> None:
    world = open_world(
        service,
        tmp_path,
        unattended=True,
        qualification=_checks(kill=True, locks=False),
    )
    refused = submit(world["config"], world["request"])
    assert refused["status"] == "refused"
    assert refused["code"] == "release-qualification"
    assert refused["decision_id"]
    assert refused["job_id"] is None
    assert _decision_count(service, world["workspace_id"]) == 1
    assert _orch_jobs(service, world["workspace_id"]) == []


def test_jobs_not_migrated_refuses_before_any_write(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        row = conn.execute(
            "SELECT ordinal, filename, sha256 FROM omp_jobs.schema_migrations ORDER BY ordinal DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        saved = (row[0], row[1], row[2])
        conn.execute("DELETE FROM omp_jobs.schema_migrations WHERE ordinal=%s", (saved[0],))
    try:
        with pytest.raises(OrchestratorError) as raised:
            submit(world["config"], world["request"])
        assert raised.value.code == "jobs_not_migrated"
        assert _orch_jobs(service, world["workspace_id"]) == []
        assert _decision_count(service, world["workspace_id"]) == 0
    finally:
        with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO omp_jobs.schema_migrations(ordinal, filename, sha256)
                VALUES (%s, %s, %s) ON CONFLICT (ordinal) DO NOTHING
                """,
                saved,
            )


def test_request_models_round_trip() -> None:
    mission_id = uuid4()
    project_id = uuid4()
    document = _mission_request(mission_id, project_id)
    BoundedIntakeDraft.model_validate(document["intake"])
    MissionIntakeScope.model_validate(document["scope"])
    OwnerInstruction.model_validate(document["instruction"])
    CreateWorkInput.model_validate(document["work"])
