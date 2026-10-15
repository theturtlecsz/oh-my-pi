"""Tests that intake bindings survive service and worker restarts across stages.

After admission on revision N:
- Closing and recreating the WorkService, PostgresWorkStore, OrchestratorHandler,
  and JobWorker allows the grant stage to proceed with the same revision and receipt.
- Direct SQL confirms the admitted binding matches.
- Appending revision N+1 while no worker runs puts the mission on hold. The fresh
  WorkService, read through its client, still serves that mission and shows the
  advanced head with no PASS. The recreated handler reports intake_revision_advanced.
- A fresh worker persists that refusal. The recreated handler reads the stored
  outcome back, and the same client still shows the advanced head with no PASS.
  The worker seam writes no feature.txt in the stage candidate worktree until a
  PASS for N+1 exists.
- After a PASS for N+1, the fresh service shows that receipt and the next stage
  re-binds and proceeds under the same mission id.
- SQL confirms the older binding row stays preserved in held state.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler, load_config
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import open_worker, tick_until
from test_orchestrator_intake_hold import (
    _binding,
    _outcome,
    _outcomes,
    _scope,
)
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
from test_workflow_service import _owner_headers
from typed_intake_support import advance_revision, record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture(autouse=True)
def restore_registry():
    """Restore the stage operation registry each rebuilt config repopulates."""
    saved = dict(orchestrator._REGISTRY)
    try:
        yield
    finally:
        orchestrator._REGISTRY.clear()
        orchestrator._REGISTRY.update(saved)


def _close_worker(worker):
    if worker is not None:
        worker.cancel_requested.set()


def _recreate_service(world):
    """Close and recreate WorkService, PostgresWorkStore, and reload config."""
    if hasattr(world.service, "client") and hasattr(world.service.client, "close"):
        world.service.client.close()
    fresh_store = PostgresWorkStore(world.service.config)
    fresh_app = create_app(
        world.service.config,
        capabilities_dir=world.service.capabilities,
        store=fresh_store,
    )
    fresh_client = TestClient(fresh_app)
    world.service = SimpleNamespace(
        client=fresh_client,
        config=world.service.config,
        capabilities=world.service.capabilities,
    )
    world.store = fresh_store
    world.config = load_config(world.config_path, world.service.config)
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler


def _client_json(world, path, params=None):
    """One read of the world's current WorkService through its HTTP client."""
    response = world.service.client.get(
        path, headers=_owner_headers(world.workspace_id), params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _pass_ids(view):
    return [
        item["id"]
        for item in view.get("assessments") or []
        if isinstance(item.get("assessment"), dict)
        and item["assessment"].get("status") == "READY_FOR_RATIFICATION"
    ]


def _intake(world, run_id, revision=None):
    params = None if revision is None else {"revision": revision}
    return _client_json(world, f"/v1/workspaces/{world.workspace_id}/intake/{run_id}", params)


def _mission_reads(world):
    """Mission record and client mission.status from the current WorkService."""
    mission = _client_json(
        world, f"/v1/workspaces/{world.workspace_id}/missions/{world.mission_id}",
    )
    standing = _client_json(
        world, f"/v1/workspaces/{world.workspace_id}/client/missions/{world.mission_id}",
    )
    return mission, standing


def _record_worker(monkeypatch):
    """Record each real worker-sandbox launch and still run it."""
    calls = []
    real = worker_sandbox.run_worker

    def recording_run_worker(argv, **kwargs):
        calls.append(Path(kwargs["worktree"]))
        return real(argv, **kwargs)

    monkeypatch.setattr(worker_sandbox, "run_worker", recording_run_worker)
    return calls


def _stage_outcome(handler, stage):
    """Persisted orch_step, read back through the recreated service handler."""
    return next(
        step for step in reversed(handler.steps())
        if step.get("stage") == stage and step.get("kind") == "outcome"
    )


def test_orchestrator_intake_survives_restart_and_rebinds_after_advancement(
    service, tmp_path, monkeypatch,
):
    world, worker, _work_id = _open_front(
        service, tmp_path / "restart", monkeypatch, extra_capability=None,
    )

    # 1. Admission on revision N
    tick_until(worker, world, lambda view: bool(_outcomes(view, "plan")), limit=20)
    assert _outcome(world, "plan")["outcome"] == "succeeded"
    admitted = _binding(world)
    assert admitted.state == "admitted"
    rev_n = admitted.revision
    receipt_n = admitted.assessment_id
    run_id = admitted.run_id
    admitted_head = _intake(world, run_id)
    assert receipt_n in _pass_ids(admitted_head)

    # 2. Close and recreate service, store, handler, and worker
    _close_worker(worker)
    _recreate_service(world)
    worker = open_worker(world)

    # 3. A later stage (grant) proceeds with the same revision and receipt
    tick_until(worker, world, lambda view: bool(_outcomes(view, "grant")), limit=20)
    assert _outcome(world, "grant")["outcome"] == "succeeded"
    grant_binding = _binding(world)
    assert grant_binding.binding_id == admitted.binding_id
    assert grant_binding.state == "admitted"
    assert grant_binding.revision == rev_n
    assert grant_binding.assessment_id == receipt_n

    # 4. Query the binding with SQL and show it matches
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        row = conn.execute(
            "SELECT mission_id, state, revision, assessment_id FROM omp_work.mission_intake_bindings "
            "WHERE binding_id = %s",
            (admitted.binding_id,),
        ).fetchone()
        assert row == (world.mission_id, "admitted", rev_n, receipt_n)
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1

    # 5. Append N+1 while no worker runs
    _close_worker(worker)
    advance_revision(world.store, **_scope(world))

    # 6. A fresh service reports the mission held with intake_revision_advanced.
    # The client is the recreated WorkService. The handler built with that
    # service reports the hold already stored by the revision append. This
    # step does not open another handler or read the binding table.
    handler = _recreate_service(world)
    mission, standing = _mission_reads(world)
    assert mission["mission_id"] == standing["result"]["mission_id"] == str(world.mission_id)
    assert standing["operation"] == "mission.status"
    assert standing["state"] == mission["status"] == "running"
    head = _intake(world, run_id)
    assert head["mission_id"] == str(world.mission_id)
    assert head["revision"] == admitted_head["revision"] + 1
    assert _pass_ids(head) == []
    pinned = _intake(world, run_id, revision=admitted_head["revision"])
    assert receipt_n in _pass_ids(pinned)
    report = handler._intake_hold("implement")
    assert report is not None
    assert report.outcome == "failed"
    assert report.data == {"stage": "implement", "code": "intake_revision_advanced"}

    # 7. A fresh worker persists that refusal and performs no implement effect.
    # The outcome is read back through the recreated handler, and the same
    # WorkService client still shows the advanced head with no PASS.
    calls = _record_worker(monkeypatch)
    worker = open_worker(world)
    worker.tick()
    refused = _stage_outcome(handler, "implement")
    assert refused["outcome"] == "failed"
    assert refused["data"]["code"] == "intake_revision_advanced"
    held_head = _intake(world, run_id)
    mission, standing = _mission_reads(world)
    assert mission["mission_id"] == standing["result"]["mission_id"] == str(world.mission_id)
    assert standing["operation"] == "mission.status"
    assert held_head["mission_id"] == str(world.mission_id)
    assert held_head["revision"] == head["revision"]
    assert _pass_ids(held_head) == []
    assert calls == []
    stage_tree = world.worktrees_dir / f"{world.mission_id}-{refused['step_index']}"
    assert not (stage_tree / "feature.txt").exists()
    recorded_tree = refused["data"].get("worktree")
    assert recorded_tree is None
    assert list(world.worktrees_dir.rglob("feature.txt")) == []
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1

    # 8. After that PASS, the next stage re-binds and proceeds under the same mission id
    receipt_n1 = record_pass(world.store, **_scope(world))

    _close_worker(worker)
    handler = _recreate_service(world)
    passed_head = _intake(world, run_id)
    assert receipt_n1 in _pass_ids(passed_head)
    mission, standing = _mission_reads(world)
    assert mission["mission_id"] == standing["result"]["mission_id"] == str(world.mission_id)
    worker = open_worker(world)
    worker.tick()
    implemented = _stage_outcome(handler, "implement")
    assert implemented["outcome"] == "succeeded"
    assert calls
    assert (calls[-1] / "feature.txt").read_text() == "feature implemented\n"
    assert Path(implemented["data"]["worktree"]) == calls[-1]

    rebound = _binding(world)
    assert rebound.binding_id != admitted.binding_id
    assert (rebound.state, rebound.revision, rebound.assessment_id) == (
        "admitted", rev_n + 1, receipt_n1,
    )

    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        # Older row stays in held state
        assert conn.execute(
            "SELECT mission_id, state, revision, assessment_id FROM omp_work.mission_intake_bindings "
            "WHERE binding_id = %s",
            (admitted.binding_id,),
        ).fetchone() == (world.mission_id, "held", rev_n, receipt_n)
        # Rebound row is admitted with revision N+1
        assert conn.execute(
            "SELECT mission_id, state, revision, assessment_id FROM omp_work.mission_intake_bindings "
            "WHERE binding_id = %s",
            (rebound.binding_id,),
        ).fetchone() == (world.mission_id, "admitted", rev_n + 1, receipt_n1)

    assert str(world.mission_id) == str(world.request.mission_id)
