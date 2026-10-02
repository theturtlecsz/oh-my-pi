"""Tests for orchestrator front stage operations: intake, confirm, plan, and grant (OMP-417-s07-s04).

Defends front pipeline contracts:
- extra kept: intake data has decision_id, work_key; read item by work_key gives submitted work_id; confirm pause names decision; reject -> mission abandoned, no plan step.
- edited_draft -> confirm succeeded, then plan succeeded, project mission row approved, mission links work item, mission status running.
- plain confirm (extra kept) -> confirm succeeded, plan failed with mission_not_admitted, no link, mission status running.
- extra_capability=None -> no decision_id, confirm not paused, plan succeeded, row approved, mission running.
- crash_after_command("draft_mission_intake") -> tick raises SimulatedCrash, expired lease reclaimed, intake succeeded, same decision_id, 1 command row, conflict_count 0.
- edited_draft grant row is active for the mission grant id, stores the s03 judge manifest, and records the intake decision as owner_input_id.
- extra_capability=None grant row records the active mandate id as owner_input_id.
- crash_after_command("begin_execution") leaves one active grant and one begin_execution command row.
- a grant already begun on the item leaves no mission grant row.
- a foreign grant at the mission grant id claiming another item fails the grant step with code grant_foreign, leaves the grant's state and grant_version unchanged, and writes exactly one execution_grants row.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest
import omp_work
from omp_work import control_actions
from omp_work.operations import database, fingerprints
from omp_work.orchestrator import candidate_git, operations, service, verifier, worker_sandbox
from omp_work.orchestrator.operations import (
    judge_manifest,
    run_confirm,
    run_intake,
    run_plan,
)
from omp_work.orchestrator.service import (
    OrchestratorConfig,
    OrchestratorHandler,
    load_request,
    submit,
)
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import CommandEnvelope, ExecutionJudgeManifest
from orchestrator_e2e_support import (
    SimulatedCrash,
    answer_intake,
    crash_after_command,
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
    item = next(i for i in tree["items"] if str(i.get("work_id")) == str(work_id))
    revision_id = UUID(str(item["revision"]["revision_id"]))
    seed_published_budget(service_fixture, world.workspace_id, work_id, revision_id)
    return revision_id


def test_extra_kept_intake_confirm_reject(service, tmp_path: Path, monkeypatch) -> None:
    """Extra capability kept: intake has decision_id, work_key; confirm pauses on decision; reject abandons."""
    world = open_e2e_world(
        service,
        tmp_path / "extra_kept",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    intake_data = intake_outcome["data"]
    assert "decision_id" in intake_data
    decision_id = intake_data["decision_id"]
    assert "work_key" in intake_data
    work_key = intake_data["work_key"]

    item_view = world.store.read(world.workspace_id, OWNER, "item", work_key)
    assert str(item_view["work_id"]) == str(work_id)

    pause_step = next(
        s
        for s in steps
        if s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
    )
    assert str(pause_step.get("decision_id")) == str(decision_id)

    # Test setup before answer_intake (Assumptions 20 / 46)
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])

    answer_intake(world, "reject")
    worker.tick()

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert mission_view["status"] == "abandoned"

    steps = handler.steps()
    assert not any(s.get("stage") == "plan" for s in steps)


def test_edited_draft_confirm_succeeded_then_plan(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Edited draft drops extra capability: confirm succeeds, then advances to a plan step."""
    world = open_e2e_world(
        service,
        tmp_path / "edited",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    intake_data = intake_outcome["data"]

    # Test setup before answer_intake (Assumptions 20 / 46)
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])

    answer_intake(world, "edited_draft")

    def plan_step_run(v: dict) -> bool:
        return any(
            s.get("stage") == "plan" and s.get("kind") == "outcome"
            for s in v["steps"]
        )

    tick_until(worker, world, plan_step_run, limit=20)

    steps = handler.steps()
    confirm_outcomes = [
        s for s in steps if s.get("stage") == "confirm" and s.get("kind") == "outcome"
    ]
    assert len(confirm_outcomes) >= 1
    assert confirm_outcomes[-1]["outcome"] == "succeeded"

    plan_outcomes = [
        s for s in steps if s.get("stage") == "plan" and s.get("kind") == "outcome"
    ]
    assert len(plan_outcomes) >= 1
    assert plan_outcomes[-1]["outcome"] == "succeeded"

    project_doc = world.store.read_project(
        world.workspace_id, OWNER, world.project_id
    )
    mission_row = next(
        m
        for m in project_doc["missions"]
        if str(m.get("mission_id")) == str(world.mission_id)
    )
    assert mission_row["status"] == "approved"

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert any(
        str(link.get("work_id")) == str(work_id)
        for link in mission_view.get("links", ())
    )
    assert mission_view["status"] == "running"


def test_plain_confirm_extra_kept_plan_failed_mission_not_admitted(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Plain confirm with extra capability kept: plan fails with mission_not_admitted, no link, status running."""
    world = open_e2e_world(
        service,
        tmp_path / "plain_confirm",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    intake_data = intake_outcome["data"]
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])

    answer_intake(world, "confirm")

    def confirm_advanced(v: dict) -> bool:
        return any(
            s.get("stage") == "confirm" and s.get("kind") == "advance"
            for s in v["steps"]
        )

    tick_until(worker, world, confirm_advanced, limit=20)

    steps = handler.steps()
    advance_step = next(
        s
        for s in reversed(steps)
        if s.get("stage") == "confirm" and s.get("kind") == "advance"
    )
    step_index = int(advance_step["step_index"]) + 1

    job = {
        "work_id": str(work_id),
        "job_id": f"orch:{world.mission_id}:{step_index}",
        "worker_id": "test-worker",
        "fence": 1,
    }
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    handler._job_id = str(job["job_id"])
    facts = handler._facts(steps, step_index, "plan", None)
    outcome = handler._operate(job, step_index, "plan", facts)

    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "mission_not_admitted"

    project_doc = world.store.read_project(
        world.workspace_id, OWNER, world.project_id
    )
    mission_row = next(
        m
        for m in project_doc["missions"]
        if str(m.get("mission_id")) == str(world.mission_id)
    )
    assert mission_row["status"] != "approved"

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert not any(
        str(link.get("work_id")) == str(work_id)
        for link in mission_view.get("links", ())
    )
    assert mission_view["status"] == "running"

    # The mission was never admitted, so the grant stage never ran.
    assert _grant_rows(world) == []


def test_no_extra_capability_no_decision_approved(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Extra capability None: intake within mandate, no decision_id, mission approved directly, plan succeeds."""
    world = open_e2e_world(
        service,
        tmp_path / "no_extra",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def plan_step_run(view: dict) -> bool:
        return any(
            s.get("stage") == "plan" and s.get("kind") == "outcome"
            for s in view["steps"]
        )

    tick_until(worker, world, plan_step_run, limit=20)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    assert "decision_id" not in intake_outcome["data"]

    # Confirm never paused
    assert not any(
        s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
        for s in steps
    )

    plan_outcomes = [
        s for s in steps if s.get("stage") == "plan" and s.get("kind") == "outcome"
    ]
    assert len(plan_outcomes) >= 1
    assert plan_outcomes[-1]["outcome"] == "succeeded"

    project_doc = world.store.read_project(
        world.workspace_id, OWNER, world.project_id
    )
    mission_row = next(
        m
        for m in project_doc["missions"]
        if str(m.get("mission_id")) == str(world.mission_id)
    )
    assert mission_row["status"] == "approved"

    mission_view = world.store.read(
        world.workspace_id, OWNER, "mission", str(world.mission_id)
    )
    assert mission_view["status"] == "running"
    assert any(
        str(link.get("work_id")) == str(work_id)
        for link in mission_view.get("links", ())
    )


def test_crash_after_draft_mission_intake(service, tmp_path: Path, monkeypatch) -> None:
    """Crash after draft_mission_intake: lease expired, tick reclaims, intake succeeds with same decision_id."""
    world = open_e2e_world(
        service,
        tmp_path / "crash",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    crash_after_command(monkeypatch, "draft_mission_intake")

    with pytest.raises(SimulatedCrash):
        worker.tick()

    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )

    # Next tick reclaims the job and re-runs intake
    worker.tick()

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    steps = handler.steps()
    intake_outcomes = [
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    ]
    assert len(intake_outcomes) == 1
    assert intake_outcomes[0]["outcome"] == "succeeded"
    decision_id = intake_outcomes[0]["data"].get("decision_id")
    assert decision_id is not None

    with (
        psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT operation_id, response, conflict_count FROM omp_control.idempotent_commands "
            "WHERE workspace_id=%s AND command_type='draft_mission_intake'",
            (world.workspace_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][2] == 0
        assert str(rows[0][1]["decision_id"]) == str(decision_id)


def test_intake_clarify_returns_blocker(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Contradictory constraints route draft intake to clarify and return blocker intake_clarify."""
    world = open_e2e_world(
        service,
        tmp_path / "clarify",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    req_path = tmp_path / "clarify" / "request.json"
    doc = json.loads(req_path.read_text(encoding="utf-8"))
    doc["intake"]["constraints"] = [
        {
            "id": "c1",
            "statement": "feature enabled",
            "source_span_ids": [],
            "key": "feature",
            "value": {"kind": "known", "value": True},
            "polarity": "positive",
        },
        {
            "id": "c2",
            "statement": "feature disabled",
            "source_span_ids": [],
            "key": "feature",
            "value": {"kind": "known", "value": True},
            "polarity": "negative",
        },
    ]
    req_path.write_text(json.dumps(doc), encoding="utf-8")
    world.request = load_request(req_path)

    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    job = {
        "work_id": str(work_id),
        "job_id": f"orch:{world.mission_id}:0",
        "worker_id": "test-worker",
        "fence": 1,
    }
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    handler._job_id = str(job["job_id"])
    steps = handler.steps()
    facts = handler._facts(steps, 0, "intake", None)
    outcome = handler._operate(job, 0, "intake", facts)
    assert outcome.outcome == "failed"
    assert outcome.blocker == "intake_clarify"


def test_intake_held_returns_blocker(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Missing budget policy routes draft intake to held and returns blocker intake_held."""
    world = open_e2e_world(
        service,
        tmp_path / "held",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    req_path = tmp_path / "held" / "request.json"
    doc = json.loads(req_path.read_text(encoding="utf-8"))
    doc["scope"]["budget_policy"] = None
    req_path.write_text(json.dumps(doc), encoding="utf-8")
    world.request = load_request(req_path)

    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    job = {
        "work_id": str(work_id),
        "job_id": f"orch:{world.mission_id}:0",
        "worker_id": "test-worker",
        "fence": 1,
    }
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    handler._job_id = str(job["job_id"])
    steps = handler.steps()
    facts = handler._facts(steps, 0, "intake", None)
    outcome = handler._operate(job, 0, "intake", facts)
    assert outcome.outcome == "failed"
    assert outcome.blocker == "intake_held"


def test_confirm_abandoned_status_fails(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Confirm returns failed when mission status is abandoned in store."""
    world = open_e2e_world(
        service,
        tmp_path / "abandoned_confirm",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    worker = open_worker(world)

    def paused_confirm(view: dict) -> bool:
        return any(
            s.get("kind") == "pause" and s.get("rule_id") == "d23-owner-confirm"
            for s in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._request = world.request.document()
    steps = handler.steps()

    intake_outcome = next(
        s for s in steps if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    world.intake_decision_id = UUID(str(intake_outcome["data"]["decision_id"]))
    world.mission_revision = int(intake_outcome["data"]["mission_revision"])

    answer_intake(world, "reject")
    steps = handler.steps()

    job = {
        "work_id": str(work_id),
        "job_id": f"orch:{world.mission_id}:1",
        "worker_id": "test-worker",
        "fence": 1,
    }
    handler._job_id = str(job["job_id"])
    facts = handler._facts(steps, 1, "confirm", None)
    outcome = handler._operate(job, 1, "confirm", facts)
    assert outcome.outcome == "failed"
    assert outcome.data.get("reason") == "abandoned"
    assert outcome.data.get("status") == "abandoned"


def test_missing_request_values_fail_with_named_codes(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Missing request fields fail intake with named codes."""
    world = open_e2e_world(
        service,
        tmp_path / "missing_vals",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    job = {
        "work_id": str(work_id),
        "job_id": f"orch:{world.mission_id}:0",
        "worker_id": "test-worker",
        "fence": 1,
    }
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    handler.project_id = world.request.project_id
    handler._job_id = str(job["job_id"])
    base_doc = world.request.document()
    steps = handler.steps()

    # Missing scope
    handler._request = dict(base_doc)
    handler._request["scope"] = None
    outcome = handler._operate(job, 0, "intake", handler._facts(steps, 0, "intake", None))
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "scope_missing"

    # Missing intake
    handler._request = dict(base_doc)
    handler._request["intake"] = None
    outcome = handler._operate(job, 0, "intake", handler._facts(steps, 0, "intake", None))
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "intake_missing"

    # Missing instruction
    handler._request = dict(base_doc)
    handler._request["instruction"] = None
    outcome = handler._operate(job, 0, "intake", handler._facts(steps, 0, "intake", None))
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "instruction_missing"


def test_plan_missing_approved_scope_fails() -> None:
    """Plan returns failed with mission_not_admitted when approved_scope is missing."""

    class DummyContext:
        def __init__(self):
            self.mission_id = UUID("11111111-1111-1111-1111-111111111111")
            self.workspace_id = UUID("22222222-2222-2222-2222-222222222222")
            self.principal = None
            self.service = type(
                "DummyService",
                (),
                {"read": lambda *args: {"approved_scope": None}},
            )()

    outcome = run_plan(DummyContext())  # type: ignore[arg-type]
    assert outcome.outcome == "failed"
    assert outcome.data.get("code") == "mission_not_admitted"


def test_judge_manifest_tcb_contract(tmp_path: Path) -> None:
    """Judge manifest matches sealed TCB formulas, validates against ExecutionJudgeManifest, and isolates verifier_argv."""
    config = OrchestratorConfig(
        workspace_id=uuid4(),
        automation_capability_path=tmp_path / "automation.json",
        qualification_path=tmp_path / "qualification.json",
        lock_map_path=tmp_path / "lock_map.json",
        verifier_key_path=tmp_path / "verifier.key",
        allowed_signers=tmp_path / "allowed_signers",
        control_repo=tmp_path / "control_repo",
        worktrees_dir=tmp_path / "worktrees",
        live_checkout=tmp_path / "live_checkout",
        verifier_argv=("python", "-m", "omp_work.orchestrator.verifier", "--strict"),
    )

    def file_bytes_sha256(module: object) -> str:
        mod_file = getattr(module, "__file__", None)
        assert mod_file is not None
        p = Path(mod_file)
        if p.suffix in (".pyc", ".pyo"):
            p = p.with_suffix(".py")
        return hashlib.sha256(p.read_bytes()).hexdigest()

    expected_auditor = sha256(
        {
            "verifier_argv": list(config.verifier_argv),
            "verifier_sha256": file_bytes_sha256(verifier),
        }
    )
    expected = {
        "auditor_agent_sha256": expected_auditor,
        "host_sha256": file_bytes_sha256(service),
        "adapter_sha256": file_bytes_sha256(operations),
        "freeze_sha256": file_bytes_sha256(candidate_git),
        "runner_sha256": file_bytes_sha256(worker_sandbox),
        "executor_sha256": file_bytes_sha256(control_actions),
        "contract_sha256": omp_work.contract_sha256(),
        "service_fingerprint": fingerprints.service_runtime_fingerprint(),
        "service_code_fingerprint": fingerprints.code_fingerprint(),
        "service_migration_sha256": database.migration_set_sha256(),
    }

    result = judge_manifest(config)
    assert result is not None
    manifest_sha, manifest = result
    assert result == (sha256(expected), expected)
    validated = ExecutionJudgeManifest(**manifest)
    assert validated.auditor_agent_sha256 == expected["auditor_agent_sha256"]

    # A different verifier_argv changes auditor_agent_sha256 and no other field
    different_config = dataclasses.replace(
        config,
        verifier_argv=("python", "-m", "omp_work.orchestrator.verifier", "--other"),
    )
    different_result = judge_manifest(different_config)
    assert different_result is not None
    diff_sha, diff_manifest = different_result
    assert diff_manifest["auditor_agent_sha256"] != manifest["auditor_agent_sha256"]
    for key in manifest:
        if key == "auditor_agent_sha256":
            assert diff_manifest[key] != manifest[key]
        else:
            assert diff_manifest[key] == manifest[key]

    # verifier_argv () -> None
    empty_config = dataclasses.replace(config, verifier_argv=())
    assert judge_manifest(empty_config) is None


def _file_bytes_sha256(module: object) -> str:
    mod_file = getattr(module, "__file__", None)
    assert mod_file is not None
    path = Path(mod_file)
    if path.suffix in (".pyc", ".pyo"):
        path = path.with_suffix(".py")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _s03_manifest(config: OrchestratorConfig) -> dict:
    """The judge manifest dict pinned by test_judge_manifest_tcb_contract."""
    auditor = sha256(
        {
            "verifier_argv": list(config.verifier_argv),
            "verifier_sha256": _file_bytes_sha256(verifier),
        }
    )
    return {
        "auditor_agent_sha256": auditor,
        "host_sha256": _file_bytes_sha256(service),
        "adapter_sha256": _file_bytes_sha256(operations),
        "freeze_sha256": _file_bytes_sha256(candidate_git),
        "runner_sha256": _file_bytes_sha256(worker_sandbox),
        "executor_sha256": _file_bytes_sha256(control_actions),
        "contract_sha256": omp_work.contract_sha256(),
        "service_fingerprint": fingerprints.service_runtime_fingerprint(),
        "service_code_fingerprint": fingerprints.code_fingerprint(),
        "service_migration_sha256": database.migration_set_sha256(),
    }


def _grant_rows(world) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT grant_id::text AS grant_id, state, grant_version, judge_manifest, provenance
            FROM omp_work.execution_grants
            WHERE workspace_id = %s
            ORDER BY created_at
            """,
            (world.workspace_id,),
        )
        return list(cur.fetchall())


def _begin_rows(world) -> list[dict]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT operation_id::text AS operation_id, command_type, conflict_count
            FROM omp_control.idempotent_commands
            WHERE workspace_id = %s AND command_type = 'begin_execution'
            """,
            (world.workspace_id,),
        )
        return list(cur.fetchall())


def _json_obj(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _intake_data(world) -> dict:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return next(
        step["data"]
        for step in handler.steps()
        if step.get("stage") == "intake" and step.get("kind") == "outcome"
    )


def _open_front(service, root: Path, monkeypatch, *, extra_capability):
    world = open_e2e_world(
        service,
        root,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=extra_capability,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)
    return world, open_worker(world), work_id


def _grant_id(world) -> str:
    return str(service._ids(world.mission_id, "grant"))


def _expire_lease(world) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def test_edited_draft_grant_row_records_intake_decision(
    service, tmp_path: Path, monkeypatch
) -> None:
    """Edited draft: the mission grant row is active and names the intake decision."""
    world, worker, _work_id = _open_front(
        service, tmp_path / "grant_edited", monkeypatch, extra_capability="egress"
    )

    def paused_confirm(view: dict) -> bool:
        return any(
            step.get("kind") == "pause" and step.get("rule_id") == "d23-owner-confirm"
            for step in view["steps"]
        )

    tick_until(worker, world, paused_confirm, limit=20)
    intake_data = _intake_data(world)
    world.intake_decision_id = UUID(str(intake_data["decision_id"]))
    world.mission_revision = int(intake_data["mission_revision"])
    answer_intake(world, "edited_draft")
    gid = _grant_id(world)

    def grant_active(_view: dict) -> bool:
        return any(
            row["grant_id"] == gid and row["state"] == "active" for row in _grant_rows(world)
        )

    tick_until(worker, world, grant_active, limit=30)
    rows = _grant_rows(world)
    assert len(rows) == 1
    assert rows[0]["grant_id"] == gid
    assert rows[0]["state"] == "active"
    assert _json_obj(rows[0]["judge_manifest"]) == _s03_manifest(world.config)
    assert _json_obj(rows[0]["provenance"])["owner_input_id"] == str(
        intake_data["decision_id"]
    )


def test_no_extra_capability_grant_uses_mandate(
    service, tmp_path: Path, monkeypatch
) -> None:
    """No extra capability: the grant's owner_input_id is the active mandate id."""
    world, worker, _work_id = _open_front(
        service, tmp_path / "grant_mandate", monkeypatch, extra_capability=None
    )
    gid = _grant_id(world)

    def grant_active(_view: dict) -> bool:
        return any(
            row["grant_id"] == gid and row["state"] == "active" for row in _grant_rows(world)
        )

    tick_until(worker, world, grant_active, limit=30)
    rows = _grant_rows(world)
    assert len(rows) == 1
    assert rows[0]["state"] == "active"
    assert _json_obj(rows[0]["provenance"])["owner_input_id"] == str(
        world.mandate.mandate_id
    )


def test_crash_after_begin_execution_one_grant(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A crash after begin_execution replays to the same active grant and one command row."""
    world, worker, _work_id = _open_front(
        service, tmp_path / "grant_crash", monkeypatch, extra_capability=None
    )

    def plan_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "plan" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, plan_ran, limit=20)
    crash_after_command(monkeypatch, "begin_execution")
    with pytest.raises(SimulatedCrash):
        worker.tick()
    _expire_lease(world)
    worker.tick()

    gid = _grant_id(world)
    rows = _grant_rows(world)
    assert len(rows) == 1
    assert rows[0]["grant_id"] == gid
    assert rows[0]["state"] == "active"
    begins = _begin_rows(world)
    assert len(begins) == 1
    assert begins[0]["conflict_count"] == 0


def test_prior_grant_on_item_writes_no_mission_grant(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A grant already begun on the item blocks the mission grant id."""
    world, worker, _work_id = _open_front(
        service, tmp_path / "grant_conflict", monkeypatch, extra_capability=None
    )

    def plan_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "plan" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, plan_ran, limit=20)
    intake_data = _intake_data(world)
    item = world.store.read(world.workspace_id, OWNER, "item", intake_data["work_key"])
    focus = world.store.read(world.workspace_id, OWNER, "focus", str(OWNER))
    judged = judge_manifest(world.config)
    assert judged is not None
    judge_sha, manifest = judged
    other_id = uuid4()
    description = str(item["revision"]["description"])
    project_id = item["project_id"]
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(world.workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {
                "type": "begin_execution",
                "payload": {
                    "grant_id": str(other_id),
                    "provenance": {
                        "owner_input_id": str(uuid4()),
                        "owner_session_id": "session-prior",
                        "normalized_command": f"/execute {intake_data['work_key']}",
                        "workspace_id": str(world.workspace_id),
                        "repository": world.repo_key,
                        "nonce": str(uuid4()),
                        "issued_at": datetime.now(UTC).isoformat(),
                    },
                    "remote_ref": world.candidate_ref,
                    "mode": "single",
                    "items": [
                        {
                            "work_id": str(item["work_id"]),
                            "revision_id": str(item["revision"]["revision_id"]),
                            "position": 0,
                            "original_request": description,
                            "original_request_sha256": text_sha256(description),
                            "initial_git_baseline": world.base_commit,
                            "project_id": None if project_id is None else str(project_id),
                        }
                    ],
                    "expected_focus_version": int(focus["version"]),
                    "judge_sha256": judge_sha,
                    "judge_manifest": manifest,
                },
            },
        }
    )
    world.store.execute(
        envelope,
        actor_id=OWNER,
        actor_kind="automation",
        required_scope="work.execute",
    )
    worker.tick()

    gid = _grant_id(world)
    rows = _grant_rows(world)
    assert [row["grant_id"] for row in rows] == [str(other_id)]
    assert gid not in {row["grant_id"] for row in rows}
    assert rows[0]["state"] == "active"


def begin(world, grant_id, work_id, revision_id, project_id):
    """Send one begin_execution claim the way the grant stage builds it."""
    judged = judge_manifest(world.config)
    assert judged is not None
    judge_sha, manifest = judged
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.description AS description, a.key AS key FROM omp_work.work_revisions r "
            "JOIN omp_work.work_aliases a ON a.work_id = r.work_id WHERE r.revision_id = %s",
            (revision_id,),
        )
        claim_row = cur.fetchone()
        description = str(claim_row["description"])
        key = str(claim_row["key"])
        cur.execute(
            "SELECT version FROM omp_work.focus_slots WHERE workspace_id = %s AND owner_id = %s",
            (world.workspace_id, OWNER),
        )
        focus = cur.fetchone()
        focus_version = int(focus["version"]) if focus else 0
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(world.workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {
                "type": "begin_execution",
                "payload": {
                    "grant_id": str(grant_id),
                    "provenance": {
                        "owner_input_id": str(world.mandate.mandate_id),
                        "owner_session_id": f"orchestrator:{world.mission_id}",
                        "normalized_command": f"/execute {key}",
                        "workspace_id": str(world.workspace_id),
                        "repository": world.repo_key,
                        "nonce": str(service._ids(world.mission_id, "nonce")),
                        "issued_at": datetime.now(UTC).isoformat(),
                    },
                    "remote_ref": world.candidate_ref,
                    "mode": "single",
                    "items": [
                        {
                            "work_id": str(work_id),
                            "revision_id": str(revision_id),
                            "position": 0,
                            "original_request": description,
                            "original_request_sha256": text_sha256(description),
                            "initial_git_baseline": world.base_commit,
                            "project_id": None if project_id is None else str(project_id),
                        }
                    ],
                    "expected_focus_version": focus_version,
                    "judge_sha256": judge_sha,
                    "judge_manifest": manifest,
                },
            },
        }
    )
    return world.store.execute(
        envelope,
        actor_id=OWNER,
        actor_kind="automation",
        required_scope="work.execute",
    )


def test_foreign_grant_at_mission_id_fails_grant_foreign(
    service, tmp_path: Path, monkeypatch
) -> None:
    """A grant at the mission grant id claiming another item fails grant_foreign, unchanged."""
    world = open_e2e_world(
        service,
        tmp_path / "grant_foreign",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    _seed_item_budget(world, service, work_id)

    _receipt, created = world.store.execute(
        CommandEnvelope.model_validate(
            {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(world.workspace_id),
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "command": {
                    "type": "create_work_batch",
                    "payload": {
                        "items": [
                            {
                                "client_ref": "second-item",
                                "title": "Second item",
                                "description": "second item description",
                                "project_id": str(world.project_id),
                            }
                        ],
                        "relations": [],
                    },
                },
            }
        ),
        actor_id=OWNER,
        actor_kind="automation",
        required_scope="work.mutate",
    )
    second = created["items"][0]
    gid = _grant_id(world)
    begin(world, gid, second["work_id"], second["revision_id"], str(world.project_id))

    worker = open_worker(world)

    def grant_ran(view: dict) -> bool:
        return any(
            step.get("stage") == "grant" and step.get("kind") == "outcome"
            for step in view["steps"]
        )

    tick_until(worker, world, grant_ran, limit=40)

    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    grant_step = next(
        step
        for step in handler.steps()
        if step.get("stage") == "grant" and step.get("kind") == "outcome"
    )
    assert grant_step["outcome"] == "failed"
    assert grant_step["data"]["code"] == "grant_foreign"

    rows = _grant_rows(world)
    assert len(rows) == 1
    assert rows[0]["grant_id"] == gid
    assert rows[0]["grant_version"] == 1
    assert rows[0]["state"] == "active"
    assert len(_begin_rows(world)) == 1

