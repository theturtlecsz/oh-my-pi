"""Implement workers receive scoped model grants, manifests, and task credentials."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.orchestrator import model_proxy_client, operations, service as orchestrator_service, worker_sandbox
from omp_work.orchestrator.service import submit
from orchestrator_e2e_support import open_e2e_world, open_worker, seed_published_budget
from test_model_proxy import serving
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import OWNER

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)


@pytest.fixture
def wiring(service, tmp_path, monkeypatch):
    saved = dict(orchestrator_service._REGISTRY)
    world = open_e2e_world(
        service, tmp_path, monkeypatch, verifier_mode="pass", worker_mode="ok",
        extra_capability=None, operations=("omp_work.orchestrator.operations:register",),
    )
    executable = tmp_path / "omp"
    executable.write_text("stand-in")
    executable.chmod(0o700)
    world.request = dataclasses.replace(
        world.request, worker_argv=(str(executable), "orchestrator-worker")
    )
    capability = world.config.automation_capability_path
    doc = json.loads(capability.read_text())
    doc["token"] = "control-secret"
    capability.write_text(json.dumps(doc))
    capability.chmod(0o600)
    app = create_app(ProxyConfig(world.config.ops, capability, {
        "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
        "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
    }))
    state = SimpleNamespace(world=world, posts=[], revoked=[], runs=[], contexts=[], outcomes=[], exit_code=0)
    register, revoke = model_proxy_client.register, model_proxy_client.revoke_task

    def record_register(capability, url, payload):
        state.posts.append(payload)
        try:
            result = register(capability, url, payload)
        except httpx.HTTPStatusError as error:
            raise AssertionError(error.response.text) from error
        state.grant = result
        return result

    def record_revoke(capability, url, task_id):
        state.revoked.append(task_id)
        result = revoke(capability, url, task_id)
        if getattr(state, "revoke_error", False):
            raise RuntimeError("revoke transport failed")
        return result

    def sandbox(argv, identity, recorder, policy, env, timeout, **kwargs):
        manifest_path = env.get("OMP_WORKER_MANIFEST")
        if manifest_path:
            assert Path(manifest_path).stat().st_mode & 0o777 == 0o600
        manifest = json.loads(Path(manifest_path).read_text()) if manifest_path else None
        state.runs.append((argv, env, kwargs, manifest))
        return state.exit_code

    def implement(ctx):
        state.contexts.append(ctx)
        state.lease = ctx.lease_expires_at().isoformat()
        state.expiry = operations._task_deadline(ctx).isoformat()
        if getattr(state, "request_patch", None):
            ctx.request = {**ctx.request, **state.request_patch}
        if getattr(state, "before_run", None):
            state.before_run(ctx)
        outcome = operations.run_implement(ctx)
        if getattr(state, "after_run", None):
            state.after_run(ctx, outcome)
        state.outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(model_proxy_client, "register", record_register)
    monkeypatch.setattr(model_proxy_client, "revoke_task", record_revoke)
    monkeypatch.setattr(worker_sandbox, "run_sandboxed", sandbox)
    operations.register()
    orchestrator_service.register_stage_operation("implement", implement)
    try:
        with serving(app) as url:
            world.config = dataclasses.replace(world.config, model_proxy_url=url)
            state.url = url
            submitted = submit(world.config, world.request)
            work_id = UUID(submitted["work_id"])
            tree = world.store.read(world.workspace_id, OWNER, "tree", "")
            item = next(row for row in tree["items"] if str(row["work_id"]) == str(work_id))
            seed_published_budget(service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"])))
            state.worker = open_worker(world)
            orchestrator_service.register_stage_operation("implement", implement)
            yield state
    finally:
        orchestrator_service._REGISTRY.clear()
        orchestrator_service._REGISTRY.update(saved)


def run(state):
    for _ in range(30):
        state.worker.tick()
        if state.outcomes:
            return state.outcomes[-1]
    raise AssertionError("implement stage never ran")


@pytest.mark.parametrize("exit_code,revoke_error", [(0, False), (1, True), (0, True)])
def test_worker_grant_manifest_and_cleanup(wiring, exit_code, revoke_error):
    state = wiring
    state.exit_code, state.revoke_error = exit_code, revoke_error
    outcome = run(state)
    assert outcome.outcome == ("succeeded" if exit_code == 0 else "crashed")
    ctx = state.contexts[0]
    assert len(state.posts) == 1
    payload = state.posts[0]
    assert Decimal(payload["ceiling_usd"]) == Decimal("2.00")
    assert payload == {
        "workspace_id": str(ctx.workspace_id), "project_id": str(ctx.project_id),
        "mission_id": str(ctx.mission_id), "work_id": str(ctx.work_id),
        "job_id": str(ctx.lease.job_id), "worker_id": ctx.lease.worker_id,
        "fence": ctx.lease.fence, "stage": "implement",
        "expires_at": state.expiry, "ceiling_usd": payload["ceiling_usd"],
    }
    argv, env, kwargs, manifest = state.runs[0]
    assert Path(argv[0]).is_absolute()
    path = Path(env["OMP_WORKER_MANIFEST"])
    assert not path.is_relative_to(Path(outcome.data["worktree"]))
    assert not path.exists()
    assert str(path) in kwargs["ro_binds"] and argv[0] in kwargs["ro_binds"]
    assert manifest == {
        "version": 1, "instruction": ctx.request["instruction"]["text"],
        "approach": operations._worker_approach(ctx),
        "allowed_paths": list(ctx.request["allowed_paths"]),
        "candidate_id": str(orchestrator_service._ids(ctx.mission_id, "plan-candidate")),
        "lease_expires_at": state.lease,
        "task_deadline": state.expiry,
        "model": {
            "api": state.grant["descriptor"]["api"], "id": state.grant["descriptor"]["id"],
            "limits": {
                "context_window": state.grant["descriptor"]["limits"]["context_tokens"],
                "max_output_tokens": state.grant["descriptor"]["limits"]["max_output_tokens"],
            }, "proxy": {"baseUrl": state.url},
        },
    }
    assert env["OMP_MODEL_TOKEN"] == state.grant["model_token"]
    assert env["OMP_MODEL_PROXY_URL"] == state.url
    # Task capability is scoped independently from the model/control bearer.
    task_path = ctx.config.ops.config_dir / "capabilities" / f"task-{ctx.mission_id}-{ctx.step_index}-implement.json"
    task_doc = json.loads(task_path.read_text())
    assert env["OMP_TASK_TOKEN"] == task_doc["token"]
    assert task_doc["candidate_ids"] == [manifest["candidate_id"]]
    assert task_doc["scopes"] == ["work.candidate.read"]
    assert "control-secret" not in env.values() and "provider-secret" not in env.values()
    assert state.revoked == [state.grant["id"]]


def test_task_capability_binds_captured_task_deadline(wiring):
    state = wiring
    assert run(state).outcome == "succeeded"
    ctx = state.contexts[0]
    assert len(state.runs) == 1
    manifest = state.runs[0][3]
    task_path = ctx.config.ops.config_dir / "capabilities" / f"task-{ctx.mission_id}-{ctx.step_index}-implement.json"
    task_doc = json.loads(task_path.read_text())
    assert task_doc["expires_at"] == state.expiry
    assert task_doc["expires_at"] == manifest["task_deadline"]
    assert task_doc["expires_at"] != manifest["lease_expires_at"]


@pytest.mark.parametrize("case,expected,code", [
    ("missing", "crashed", "worker_argv_missing"),
    ("other", "succeeded", None),
    ("proxy", "failed", "invalid_request"),
    ("room", "failed", "ceiling_refused"),
    ("relative", "failed", "invalid_request"),
    ("relative_omp", "failed", "invalid_request"),
])
def test_nonworker_and_refusals_never_register(wiring, case, expected, code):
    if case == "missing":
        wiring.request_patch = {"worker_argv": []}
    elif case == "other":
        wiring.request_patch = {"worker_argv": ["/usr/bin/true"]}
    elif case == "relative":
        wiring.request_patch = {"worker_argv": ["orchestrator-worker"]}
    elif case == "relative_omp":
        wiring.request_patch = {"worker_argv": ["omp", "orchestrator-worker"]}
    elif case == "proxy":
        wiring.worker.handlers["*"].config = dataclasses.replace(wiring.world.config, model_proxy_url=None)
    else:
        with psycopg.connect(**wiring.world.config.ops.connection_kwargs("postgres")) as conn:
            conn.execute(
                "INSERT INTO omp_work.spend_records(workspace_id,record_id,project_id,amount_usd,tier) "
                "VALUES(%s,%s,%s,100,'tier1')",
                (wiring.world.workspace_id, uuid4(), wiring.world.project_id),
            )
    outcome = run(wiring)
    assert outcome.outcome == expected
    assert outcome.data.get("code") == code
    assert wiring.posts == []
    if case == "other":
        assert not any(name in wiring.runs[0][1] for name in ("OMP_MODEL_TOKEN", "OMP_MODEL_PROXY_URL", "OMP_WORKER_MANIFEST"))


@pytest.mark.parametrize("cap,expected", [("item", "2.00"), ("project", "0.70"), ("mission", "0.70")])
def test_ceiling_includes_spend_and_caps_unused_room(wiring, cap, expected):
    def seed(ctx):
        record_usage(
            NativeJobStore(ctx.config.ops), operation_id=str(uuid4()),
            workspace_id=ctx.workspace_id, actor_id=OWNER, work_id=ctx.work_id,
            request_id=str(uuid4()), role="coder", model="test-model",
            input_tokens=1, output_tokens=1, cache_tokens=0,
            measurement="measured", price_usd="0.40",
        )
        if cap == "item":
            return
        with psycopg.connect(**ctx.config.ops.connection_kwargs("postgres")) as conn:
            if cap == "project":
                conn.execute(
                    "UPDATE omp_work.spend_budgets SET ceiling_usd=0.50,threshold_usd=0.50 "
                    "WHERE workspace_id=%s AND mission_id IS NULL", (ctx.workspace_id,),
                )
            else:
                conn.execute(
                    "UPDATE omp_work.spend_budgets SET ceiling_usd=0.50,threshold_usd=0.50 "
                    "WHERE workspace_id=%s AND mission_id=%s",
                    (ctx.workspace_id, ctx.mission_id),
                )
        ctx.projects.record_spend(
            ctx.workspace_id, OWNER, ctx.project_id,
            ctx.mission_id if cap == "mission" else None, Decimal("0.20"), datetime.now(UTC),
        )
    wiring.before_run = seed
    assert run(wiring).outcome == "succeeded"
    assert Decimal(wiring.posts[0]["ceiling_usd"]) == Decimal(expected)


@pytest.mark.parametrize("stamp", [True, False])
def test_manifest_uses_plan_or_intake_approach(wiring, monkeypatch, stamp):
    def set_read(ctx):
        read = ctx.service.read
        def execution(principal, workspace_id, kind, key):
            if kind == "execution":
                return {"items": [{"work_id": str(ctx.work_id), "plan_stamp": {
                    "approach": ["Follow approved implementation plan"]
                } if stamp else None}]}
            return read(principal, workspace_id, kind, key)
        monkeypatch.setattr(ctx.service, "read", execution)
        ctx.request = {**ctx.request, "intake": {
            "goal": {"statement": "Implement requested feature"},
            "constraints": [{"statement": "Preserve CLI behavior"}],
        }}
    wiring.before_run = set_read
    assert run(wiring).outcome == "succeeded"
    assert wiring.runs[0][3]["approach"] == (
        ["Follow approved implementation plan"] if stamp
        else ["Implement requested feature", "Preserve CLI behavior"]
    )


def test_evaluate_and_verifier_sandbox_omit_model_grant(wiring):
    def after(ctx, outcome):
        ctx.prior = {**ctx.prior, "implement": {"data": outcome.data}}
        ctx.request = {**ctx.request, "test_command": ["/usr/bin/true"]}
        assert operations.run_evaluate(ctx).outcome == "succeeded"
        assert operations._sandbox(
            ctx, ["/usr/bin/true"], Path(outcome.data["worktree"]), identity="verifier"
        ) == 0
    wiring.after_run = after
    assert run(wiring).outcome == "succeeded"
    assert len(wiring.posts) == 1
    assert len(wiring.runs) == 3
    for _, env, kwargs, manifest in wiring.runs[1:]:
        assert manifest is None and kwargs["model_proxy"] is None
        assert not any(name in env for name in (
            "OMP_MODEL_TOKEN", "OMP_MODEL_PROXY_URL", "OMP_WORKER_MANIFEST"
        ))
