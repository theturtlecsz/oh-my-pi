"""Model-grant failures and the implement envelope check (OMP-417-s07-s07).

Done when:
- registering a lease-bound model task that the proxy refuses with 403 fails
  the orchestrator-worker implement stage as ``grant_refused``;
- a connect error to the model proxy fails it as ``proxy_unavailable``;
- a non-zero worker exit stays ``crashed``;
- an exit-0 orchestrator worker that writes outside ``allowed_paths`` fails as
  ``envelope_violation`` with the violation codes and leaves the edit on disk;
- a legacy worker argv skips the grant and the envelope check;
- evaluate and audit never register a model task, and a second implement visit
  registers a fresh task id.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.orchestrator import (
    candidate_git,
    model_proxy_client,
    operations,
    service as orchestrator_service,
    worker_sandbox,
)
from omp_work.orchestrator.service import OrchestratorHandler, submit
from orchestrator_e2e_support import (
    open_e2e_world,
    open_worker,
    seed_published_budget,
    tick_until,
)
from test_model_proxy import serving
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration required",
)
pytest_plugins = ("test_workflow_service",)

_ROUTES = {
    "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
    "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
}


def _closed_port() -> int:
    """One loopback port that is bound and released, so a connect refuses."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_budget(service, world, work_id: UUID) -> None:
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row["work_id"]) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )


@contextlib.contextmanager
def _world(
    service,
    root: Path,
    monkeypatch,
    *,
    foreign_workspace: bool = False,
    closed: bool = False,
    exit_code: int = 0,
    writes: tuple[str, ...] = (),
):
    """Build a world whose orchestrator-worker argv is a stand-in executable.

    A real model proxy is served on loopback from a mode-0600 capability; the
    worker argv is absolute and named ``orchestrator-worker``. ``run_sandboxed``
    is stubbed so the stand-in file is never executed: it records the launch and
    materializes ``writes`` inside the worktree before returning ``exit_code``.
    Register calls are recorded and re-raised so the stage maps the failure.
    """
    saved = dict(orchestrator_service._REGISTRY)
    world = open_e2e_world(
        service, root, monkeypatch, verifier_mode="pass", worker_mode="ok",
        extra_capability=None, operations=("omp_work.orchestrator.operations:register",),
    )
    executable = root / "orchestrator-worker"
    executable.write_text("stand-in")
    executable.chmod(0o700)
    world.request = dataclasses.replace(
        world.request, worker_argv=(str(executable), "orchestrator-worker")
    )
    capability = world.config.automation_capability_path
    document = json.loads(capability.read_text())
    document["token"] = "control-secret"
    capability.write_text(json.dumps(document))
    capability.chmod(0o600)
    app_capability = root / "app-capability.json"
    app_document = json.loads(capability.read_text())
    if foreign_workspace:
        app_document["workspaces"] = [str(uuid4())]
    app_capability.write_text(json.dumps(app_document))
    app_capability.chmod(0o600)
    app = create_app(ProxyConfig(world.config.ops, app_capability, _ROUTES))
    state = SimpleNamespace(
        world=world, posts=[], statuses=[], ids=[], runs=[],
        outcomes=[], ctx=None, exit_code=exit_code, writes=writes, after=None,
    )
    real_register = model_proxy_client.register

    def record_register(cap, url, payload):
        state.posts.append(payload)
        try:
            result = real_register(cap, url, payload)
        except httpx.HTTPStatusError as error:
            state.statuses.append(error.response.status_code)
            raise
        except httpx.TransportError:
            state.statuses.append(None)
            raise
        state.ids.append(result["id"])
        return result

    def sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs):
        manifest = env.get("OMP_WORKER_MANIFEST")
        state.runs.append((argv, manifest, kwargs))
        if manifest:
            assert Path(manifest).stat().st_mode & 0o777 == 0o600
        for name in state.writes:
            (Path(kwargs["worktree"]) / name).write_text("outside the envelope\n")
        return state.exit_code

    def implement(ctx):
        state.ctx = ctx
        outcome = operations.run_implement(ctx)
        state.outcomes.append(outcome)
        if state.after is not None:
            state.after(ctx, outcome)
        return outcome

    monkeypatch.setattr(model_proxy_client, "register", record_register)
    monkeypatch.setattr(worker_sandbox, "run_sandboxed", sandbox)
    operations.register()
    try:
        with serving(app) as url:
            world.config = dataclasses.replace(
                world.config,
                model_proxy_url=f"http://127.0.0.1:{_closed_port()}" if closed else url,
            )
            state.url = world.config.model_proxy_url
            submitted = submit(world.config, world.request)
            work_id = UUID(str(submitted["work_id"]))
            _seed_budget(service, world, work_id)
            state.worker = open_worker(world)
            # open_worker constructs its handler and re-runs register(), so the
            # wrapper replaces the real implement only after the worker exists.
            orchestrator_service.register_stage_operation("implement", implement)
            yield state
    finally:
        orchestrator_service._REGISTRY.clear()
        orchestrator_service._REGISTRY.update(saved)


def _run_implement(state) -> Any:
    """Tick the worker until the wrapped implement stage records one outcome."""
    for _ in range(60):
        if state.outcomes:
            return state.outcomes[-1]
        state.worker.tick()
    raise AssertionError("implement stage never ran")


def _check_evaluate_and_audit_register_nothing(state, ctx, outcome) -> None:
    """Evaluate and audit run with no model grant: the one POST count stays put."""
    ctx.prior = {**ctx.prior, "implement": {"data": outcome.data}}
    before = len(state.posts)
    assert operations.run_evaluate(ctx).outcome == "succeeded"
    operations.run_audit(ctx)
    assert len(state.posts) == before


def test_register_403_is_grant_refused(service, tmp_path: Path, monkeypatch) -> None:
    """A proxy 403 fails the orchestrator worker as grant_refused."""
    with _world(service, tmp_path / "refused", monkeypatch, foreign_workspace=True) as state:
        state.after = lambda ctx, outcome: _check_evaluate_and_audit_register_nothing(
            state, ctx, outcome
        )
        outcome = _run_implement(state)
        assert outcome.outcome == "failed"
        assert outcome.data["code"] == "grant_refused"
        assert state.statuses == [403]
        assert len(state.posts) == 1


def test_connect_error_is_proxy_unavailable(service, tmp_path: Path, monkeypatch) -> None:
    """A refused connection to the model proxy fails the stage as proxy_unavailable."""
    with _world(service, tmp_path / "unavailable", monkeypatch, closed=True) as state:
        state.after = lambda ctx, outcome: _check_evaluate_and_audit_register_nothing(
            state, ctx, outcome
        )
        outcome = _run_implement(state)
        assert outcome.outcome == "failed"
        assert outcome.data["code"] == "proxy_unavailable"
        assert state.statuses == [None]
        assert len(state.posts) == 1


def test_nonzero_exit_stays_crashed(service, tmp_path: Path, monkeypatch) -> None:
    """A registered worker that exits non-zero stays crashed, not failed."""
    with _world(service, tmp_path / "nonzero", monkeypatch, exit_code=1) as state:
        outcome = _run_implement(state)
        assert outcome.outcome == "crashed"
        assert outcome.data.get("exit_code") == 1
        assert "code" not in outcome.data
        assert len(state.ids) == 1


def test_out_of_scope_exit_zero_violates_envelope(service, tmp_path: Path, monkeypatch) -> None:
    """An exit-0 worker that writes outside the envelope fails and leaves the edit."""
    with _world(
        service, tmp_path / "envelope", monkeypatch, exit_code=0, writes=("outside.txt",)
    ) as state:
        outcome = _run_implement(state)
        assert outcome.outcome == "failed"
        assert outcome.data["code"] == "envelope_violation"
        assert "outside_allowed:outside.txt" in outcome.data["codes"]
        worktree = Path(outcome.data["worktree"])
        assert (worktree / "outside.txt").is_file()
        assert len(state.ids) == 1

        # The retry re-enters implement on a fresh lease and registers again.
        for _ in range(60):
            if len(state.outcomes) >= 2:
                break
            state.worker.tick()
        assert len(state.outcomes) >= 2
        second = state.outcomes[-1]
        assert second.outcome == "failed"
        assert second.data["code"] == "envelope_violation"
        assert len(state.posts) == 2
        assert state.ids[0] != state.ids[1]


def test_legacy_argv_skips_grant_and_envelope(service, tmp_path: Path, monkeypatch) -> None:
    """A legacy worker argv registers no task and is not envelope-checked."""
    saved = dict(orchestrator_service._REGISTRY)
    world = open_e2e_world(
        service, tmp_path / "legacy", monkeypatch, verifier_mode="pass",
        worker_mode="outside", extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    posts: list[Any] = []
    envelopes: list[Any] = []
    real_register = model_proxy_client.register
    real_envelope = candidate_git.check_envelope

    def record_register(cap, url, payload):
        posts.append(payload)
        return real_register(cap, url, payload)

    def record_envelope(*args, **kwargs):
        envelopes.append((args, kwargs))
        return real_envelope(*args, **kwargs)

    monkeypatch.setattr(model_proxy_client, "register", record_register)
    monkeypatch.setattr(candidate_git, "check_envelope", record_envelope)
    operations.register()
    try:
        assert Path(world.request.worker_argv[0]).name != "orchestrator-worker"
        submitted = submit(world.config, world.request)
        work_id = UUID(str(submitted["work_id"]))
        _seed_budget(service, world, work_id)
        handler = OrchestratorHandler(world.config)
        handler.mission_id = world.mission_id

        def implemented(_view: dict) -> bool:
            return any(
                step.get("stage") == "implement" and step.get("kind") == "outcome"
                for step in handler.steps()
            )

        tick_until(open_worker(world), world, implemented, limit=40)
        step = next(
            item
            for item in handler.steps()
            if item.get("stage") == "implement" and item.get("kind") == "outcome"
        )
        assert step["outcome"] == "succeeded"
        assert posts == []
        assert envelopes == []
        worktree = Path(step["data"]["worktree"])
        assert (worktree / "outside.txt").is_file()
    finally:
        orchestrator_service._REGISTRY.clear()
        orchestrator_service._REGISTRY.update(saved)
