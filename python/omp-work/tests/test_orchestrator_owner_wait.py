"""Owner confirm, tier-3 merge hold, and act replay (OMP-417-s07)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.action_classify import classify, parse_submission
from omp_work.orchestrator.service import (
    OrchestratorHandler,
    _REGISTRY,
    load_config,
    load_request,
    register_stage_operation,
    status,
    submit,
)
from omp_work.orchestrator.stages import Facts, decision_for
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import StandingMandate
from omp_work.v1.service import WorkError
from omp_work.v1.store import PostgresWorkStore
from test_orchestrator_service import (
    _approve,
    _checks,
    _decision,
    _mission_request,
    _steps,
    _view,
    _worker,
    _workspace,
)
from test_workflow_service import OWNER, _command
import orchestrator_wait_ops

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_SCOPES = ["work.read", "work.mutate", "work.approve", "work.close", "work.execute"]


class _StageCrash(BaseException):
    """One injected crash. ``Exception`` handlers must not swallow it."""


def _config(
    root: Path,
    service,
    workspace_id: UUID,
    *,
    owner: bool,
    lease_seconds: int = 30,
):
    automation = root / "automation.json"
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
    if owner:
        (root / "owner.json").write_text(
            json.dumps(
                {
                    "actor_id": str(OWNER),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": _SCOPES,
                }
            )
        )
    qualification_path.write_text(json.dumps(_checks()))
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
                "wait_seconds": 0,
                "operations": ["orchestrator_wait_ops:register"],
            }
        )
    )
    return load_config(path, service.config), path


def _world(
    service,
    tmp_path: Path,
    *,
    owner: bool = True,
    controls: dict | None = None,
    target_ref: str | None = None,
    lease_seconds: int = 30,
):
    root = Path(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    workspace_id, project_id = _workspace(service)
    mission_id = uuid4()
    config, path = _config(
        root, service, workspace_id, owner=owner, lease_seconds=lease_seconds
    )
    document = _mission_request(mission_id, project_id, controls=controls)
    if target_ref:
        document["target_ref"] = target_ref
    request_path = root / "request.json"
    request_path.write_text(json.dumps(document))
    return {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "mission_id": mission_id,
        "config": config,
        "config_path": path,
        "request": load_request(request_path),
    }


def _event_count(service, workspace_id: UUID, event_type: str) -> int:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM omp_audit.domain_events
            WHERE workspace_id=%s AND event_type=%s AND outcome='applied'
            """,
            (workspace_id, event_type),
        ).fetchone()
    return int(row[0])


def _actions(service, workspace_id: UUID) -> list[dict]:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn:
        return list(
            conn.execute(
                """
                SELECT outcome, code, decision_id
                FROM omp_work.project_action_records
                WHERE workspace_id=%s
                ORDER BY recorded_at, record_id
                """,
                (workspace_id,),
            ).fetchall()
        )


def _wait_until(worker, config, mission_id: UUID, predicate, limit: int = 40) -> dict:
    view = _view(config, mission_id)
    for _ in range(limit):
        if predicate(view):
            return view
        worker.tick()
        view = _view(config, mission_id)
    raise AssertionError(json.dumps({"view": view, "steps": _steps(config, mission_id)}, default=str)[:8000])


def _pause(view: dict, rule_id: str) -> dict:
    found = [step for step in view["steps"] if step.get("rule_id") == rule_id]
    assert len(found) == 1, view
    return found[0]


def _reached_plan(config, mission_id: UUID) -> bool:
    return any(
        step.get("stage") == "plan" or step.get("next_stage") == "plan"
        for step in _steps(config, mission_id)
    )


def _answer_draft(service, world: dict, decision_id: str, answer: dict) -> None:
    code, body = _command(
        service,
        world["workspace_id"],
        {
            "type": "answer_mission_draft",
            "payload": {
                "decision_id": decision_id,
                "mission_id": str(world["mission_id"]),
                "revision": 1,
                "answer": answer,
                "instruction": world["request"].instruction.model_dump(mode="json"),
            },
        },
    )
    assert code == 200, body


def _allow_merge(service, world: dict) -> None:
    decision_id = uuid4()
    PostgresWorkStore(service.config).set_standing_mandate(
        world["workspace_id"],
        OWNER,
        world["project_id"],
        StandingMandate(
            mandate_id=uuid4(),
            goals=frozenset({"ship"}),
            repositories=frozenset({"repo"}),
            capabilities=frozenset({"git_push"}),
            tier3_classes=frozenset({"merge_protected_branch"}),
            decision_id=decision_id,
        ),
        ChangeAuthority(
            requested_by_kind="owner",
            decision_id=decision_id,
            answered_by_kind="owner",
        ),
    )


def _merge_world(service, tmp_path: Path, effect: Path, *, plant: bool = False, lease_seconds: int = 30):
    world = _world(
        service,
        tmp_path,
        owner=True,
        target_ref="main",
        lease_seconds=lease_seconds,
        controls={"merge": {"effect": str(effect), "plant_intent": plant}},
    )
    submitted = submit(world["config"], world["request"])
    assert submitted["status"] == "enqueued"
    _allow_merge(service, world)
    return world


def _confirm_pause(service, world: dict, worker) -> str:
    view = _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: any(step.get("rule_id") == "d23-owner-confirm" for step in item["steps"]),
    )
    assert view["status"] == "awaiting_confirmation"
    assert _event_count(service, world["workspace_id"], "create_decision") == 0
    assert _event_count(service, world["workspace_id"], "set_mission_status") == 0
    assert _event_count(service, world["workspace_id"], "approve_mission") == 0
    decision_id = str(_pause(view, "d23-owner-confirm")["decision_id"])
    record = _decision(service, world["workspace_id"], decision_id)
    assert record["status"] == "pending"
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        row = conn.execute(
            """
            SELECT job_id, status FROM omp_jobs.jobs
            WHERE workspace_id=%s AND job_id=%s
            """,
            (world["workspace_id"], f"orch:{world['mission_id']}:wait:{decision_id}:0"),
        ).fetchone()
    assert row is not None
    return decision_id


def test_no_owner_approves_nothing_and_owner_json_runs(service, tmp_path: Path) -> None:
    absent = _world(service, tmp_path / "absent", owner=False)
    submitted = submit(absent["config"], absent["request"])
    assert submitted["status"] == "enqueued"
    assert status(absent["config"], absent["mission_id"])["status"] == "draft"
    assert _event_count(service, absent["workspace_id"], "approve_mission") == 0

    present = _world(service, tmp_path / "present", owner=True)
    submitted = submit(present["config"], present["request"])
    assert submitted["status"] == "enqueued"
    assert status(present["config"], present["mission_id"])["status"] == "running"
    assert _event_count(service, present["workspace_id"], "approve_mission") >= 1


@pytest.mark.parametrize(
    "answer",
    [
        {"kind": "option", "option": "confirm"},
        {
            "kind": "edited_draft",
            "draft": {
                "project_id": "filled",
                "objective": "Edited orchestrated objective",
                "acceptance_criteria": ["tests pass"],
                "risk_policy": "risk",
                "approval_policy": "approval",
                "effort_policy": "effort",
                "budget_policy": {
                    "usd": "10.00",
                    "tokens": 1000,
                    "wall_clock_seconds": 3600,
                    "max_subagents": 2,
                },
                "kind": "engineering.execute",
            },
        },
    ],
)
def test_confirm_and_edited_draft_reach_plan(service, tmp_path: Path, answer: dict) -> None:
    world = _world(service, tmp_path, owner=False)
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    decision_id = _confirm_pause(service, world, worker)
    if answer["kind"] == "edited_draft":
        answer["draft"]["project_id"] = str(world["project_id"])
    _answer_draft(service, world, decision_id, answer)
    _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda _item: _reached_plan(world["config"], world["mission_id"]),
    )
    assert _reached_plan(world["config"], world["mission_id"])


def test_reject_abandons_without_a_plan(service, tmp_path: Path) -> None:
    world = _world(service, tmp_path, owner=False)
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    decision_id = _confirm_pause(service, world, worker)
    _answer_draft(service, world, decision_id, {"kind": "option", "option": "reject"})
    for _ in range(8):
        worker.tick()
    assert status(world["config"], world["mission_id"])["status"] == "abandoned"
    assert not _reached_plan(world["config"], world["mission_id"])


def test_merge_hold_is_one_decision_and_approve_runs_once(service, tmp_path: Path) -> None:
    effect = tmp_path / "effect"
    world = _merge_world(service, tmp_path / "world", effect)
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: any(step.get("rule_id") == "d41-merge-approval" for step in item["steps"]),
    )
    pause = _pause(view, "d41-merge-approval")
    decision_id = str(pause["decision_id"])
    expected = str(
        uuid5(
            NAMESPACE_URL,
            f"omp-417:{world['mission_id']}:{pause['step_index']}:d41-merge-approval",
        )
    )
    assert decision_id == expected
    assert _event_count(service, world["workspace_id"], "create_decision") == 1
    record = _decision(service, world["workspace_id"], decision_id)
    classified = classify(parse_submission(orchestrator_wait_ops.push_submission()), orchestrator_wait_ops.push_target())
    assert record["action_class"] == "merge_protected_branch"
    assert record["target_sha256"] == classified.target_sha256
    payload = decision_for(
        Facts(
            mission_id=str(world["mission_id"]),
            project_id=world["project_id"],
            step_index=int(pause["step_index"]),
            stage="merge",
            mission_status="running",
            outcome="failed",
            qualified=True,
            candidate_commit=orchestrator_wait_ops.push_target().commit,
            target_ref="main",
        ),
        "d41-merge-approval",
    )
    assert str(payload.decision_id) == decision_id
    assert not effect.exists()

    _approve(
        service,
        tmp_path,
        world["workspace_id"],
        decision_id,
        action_class="merge_protected_branch",
        target_sha256=classified.target_sha256,
    )
    _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda _item: effect.exists(),
    )
    assert effect.read_text(encoding="utf-8") == "1"
    for _ in range(6):
        worker.tick()
    assert effect.read_text(encoding="utf-8") == "1"
    assert _event_count(service, world["workspace_id"], "create_decision") == 1
    allowed = [row for row in _actions(service, world["workspace_id"]) if row["outcome"] == "allowed"]
    assert len(allowed) == 1
    assert str(allowed[0]["decision_id"]) == decision_id


def test_crashes_at_allowed_then_intent_run_the_effect_once(service, tmp_path: Path, monkeypatch) -> None:
    effect = tmp_path / "effect"
    world = _merge_world(service, tmp_path / "world", effect, lease_seconds=300)
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: any(step.get("rule_id") == "d41-merge-approval" for step in item["steps"]),
    )
    decision_id = str(_pause(view, "d41-merge-approval")["decision_id"])
    classified = classify(parse_submission(orchestrator_wait_ops.push_submission()), orchestrator_wait_ops.push_target())
    _approve(
        service,
        tmp_path,
        world["workspace_id"],
        decision_id,
        action_class="merge_protected_branch",
        target_sha256=classified.target_sha256,
    )
    seen = {"action_allowed": 0, "external_intent": 0}
    original = OrchestratorHandler.record

    def crashing(self, step_index: int, body: Mapping) -> dict:
        kind = body.get("kind")
        if kind in seen and seen[kind] == 0:
            seen[kind] += 1
            raise _StageCrash(str(kind))
        return original(self, step_index, body)

    monkeypatch.setattr(OrchestratorHandler, "record", crashing)
    crashed = None
    for _ in range(12):
        try:
            worker.tick()
        except _StageCrash as exc:
            crashed = exc
            break
    assert crashed is not None and str(crashed) == "action_allowed"
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        rows = list(
            conn.execute(
                """
                SELECT job_id, work_id, worker_id, fence, status
                FROM omp_jobs.jobs
                WHERE workspace_id=%s AND status='admitted' AND job_id LIKE 'orch:%%'
                """,
                (world["workspace_id"],),
            ).fetchall()
        )
    assert len(rows) == 1, rows
    job = rows[0]
    handler = worker.handlers["*"]
    with pytest.raises(_StageCrash, match="external_intent"):
        handler.run(None, dict(job))
    handler.run(None, dict(job))
    assert effect.read_text(encoding="utf-8") == "1"
    handler.run(None, dict(job))
    assert effect.read_text(encoding="utf-8") == "1"
    assert seen == {"action_allowed": 1, "external_intent": 1}


def test_intent_without_action_allowed_still_performs(service, tmp_path: Path) -> None:
    effect = tmp_path / "effect"
    world = _merge_world(service, tmp_path / "world", effect, plant=True)
    worker = _worker(service, world["config"], world["workspace_id"])
    _wait_until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: any(step.get("rule_id") == "d41-merge-approval" for step in item["steps"]),
    )
    assert not effect.exists()
    assert _event_count(service, world["workspace_id"], "create_decision") == 1
    rows = _actions(service, world["workspace_id"])
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["code"] == "blocked_owner_signature"
    steps = _steps(world["config"], world["mission_id"])
    assert any(step.get("kind") == "external_intent" for step in steps)
    assert not any(step.get("kind") == "external_done" for step in steps)
    assert not any(step.get("kind") == "action_allowed" for step in steps)


def test_command_key_conflict_and_dead_lease(service, tmp_path: Path) -> None:
    lease_path = tmp_path / "lease.json"
    saved = _REGISTRY.get("intake")

    def run_intake(ctx) -> object:
        from omp_work.orchestrator.service import Outcome

        live = ctx.lease_expires_at()
        with psycopg.connect(**ctx.config.ops.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                """
                UPDATE omp_jobs.jobs
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE workspace_id=%s AND job_id=%s
                """,
                (ctx.workspace_id, str(ctx.lease.job_id)),
            )
            dead = ctx.lease_expires_at()
            conn.execute(
                """
                UPDATE omp_jobs.jobs
                SET lease_expires_at = clock_timestamp() + interval '1 hour'
                WHERE workspace_id=%s AND job_id=%s
                """,
                (ctx.workspace_id, str(ctx.lease.job_id)),
            )
        lease_path.write_text(
            json.dumps({"live": isinstance(live, datetime), "dead": dead is None}),
            encoding="utf-8",
        )
        payload = _probe_decision(ctx, "Publish the intake?")
        mode = _intake_mode(ctx)
        if mode == "replay":
            first = ctx.command("create_decision", payload, key="probe")
            second = ctx.command("create_decision", payload, key="probe")
            assert first.get("decision_id") == second.get("decision_id") == payload["decision_id"]
            return Outcome(outcome="succeeded", data={"stage": "intake"})
        ctx.command("create_decision", payload, key="probe")
        ctx.command("create_decision", _probe_decision(ctx, "A different question?"), key="probe")
        return Outcome(outcome="succeeded", data={"stage": "intake"})

    register_stage_operation("intake", run_intake)
    try:
        conflict = _world(
            service,
            tmp_path / "conflict",
            owner=False,
            controls={"intake": {"mode": "conflict"}},
        )
        submit(conflict["config"], conflict["request"])
        worker = _worker(service, conflict["config"], conflict["workspace_id"])
        with pytest.raises(WorkError) as raised:
            worker.tick()
        assert raised.value.code == "idempotency_conflict"
        assert json.loads(lease_path.read_text(encoding="utf-8")) == {"live": True, "dead": True}
        assert _event_count(service, conflict["workspace_id"], "create_decision") == 1

        replay = _world(
            service,
            tmp_path / "replay",
            owner=False,
            controls={"intake": {"mode": "replay"}},
        )
        submit(replay["config"], replay["request"])
        worker = _worker(service, replay["config"], replay["workspace_id"])
        assert worker.tick() is not None
        assert _event_count(service, replay["workspace_id"], "create_decision") == 1
        assert json.loads(lease_path.read_text(encoding="utf-8")) == {"live": True, "dead": True}
    finally:
        if saved is None:
            _REGISTRY.pop("intake", None)
        else:
            _REGISTRY["intake"] = saved


def _intake_mode(ctx) -> str:
    raw = ctx.request.get("controls") if isinstance(ctx.request, dict) else None
    stage = raw.get("intake") if isinstance(raw, dict) else None
    if not isinstance(stage, dict):
        return "conflict"
    return str(stage.get("mode") or "conflict")


def _probe_decision(ctx, question: str) -> dict:
    decision_id = str(uuid5(NAMESPACE_URL, f"omp-417:{ctx.mission_id}:probe"))
    return {
        "decision_id": decision_id,
        "project_id": str(ctx.project_id),
        "mission_id": str(ctx.mission_id),
        "question": question,
        "why_it_matters": "The mission cannot continue without a ruling.",
        "risk_of_delay": "The window closes and the mission stalls.",
        "options": ["publish", "hold"],
        "default_if_any": "hold",
        "risk_of_each_choice": {
            "publish": "Irreversible external exposure.",
            "hold": "Missed deadline.",
        },
    }
