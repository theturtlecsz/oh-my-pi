"""Tests that intake bindings survive service and worker restarts across stages.

After admission on revision N:
- Closing and recreating the WorkService, PostgresWorkStore, OrchestratorHandler,
  and JobWorker allows the grant stage to proceed with the same revision and receipt.
- Direct SQL confirms the admitted binding matches.
- Appending revision N+1 while no worker runs puts the mission on hold: a fresh
  service reports the mission held with code intake_revision_advanced.
- A fresh worker performs no stage effect until a PASS for N+1 exists.
- After a PASS for N+1 is persisted and the pause is answered, the next stage
  re-binds and proceeds under the same mission id.
- SQL confirms the older binding row stays preserved in held state.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import OrchestratorHandler, load_config
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import open_worker, tick_until
from test_orchestrator_intake_hold import (
    _binding,
    _outcome,
    _outcomes,
    _refused,
    _scope,
)
from test_orchestrator_operations_front import _begin_rows, _grant_rows, _open_front
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

    # 2. Close and recreate service, store, handler, and worker
    _close_worker(worker)
    _recreate_service(world)
    worker = open_worker(world)

    # 3. A later stage (grant) proceeds with the same revision and receipt
    view = tick_until(worker, world, lambda view: bool(_outcomes(view, "grant")), limit=20)
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

    # 6. A fresh service reports the mission held with intake_revision_advanced
    _recreate_service(world)
    held = _binding(world)
    assert (held.binding_id, held.run_id) == (admitted.binding_id, admitted.run_id)
    assert (held.state, held.revision, held.assessment_id, held.hold_code) == (
        "held", rev_n, receipt_n, "intake_revision_advanced",
    )
    with psycopg.connect(**world.config.ops.connection_kwargs("postgres")) as conn:
        held_row = conn.execute(
            "SELECT mission_id, state, revision, assessment_id, hold_code "
            "FROM omp_work.mission_intake_bindings WHERE binding_id = %s",
            (admitted.binding_id,),
        ).fetchone()
        assert held_row == (world.mission_id, "held", rev_n, receipt_n, "intake_revision_advanced")

    # 7. A fresh worker performs no stage effect until a PASS for N+1 exists
    worker = open_worker(world)
    worker.tick()
    _refused(world, "implement", "intake_revision_advanced")
    assert not (world.live_checkout / "feature.txt").exists()
    assert len(_begin_rows(world)) == len(_grant_rows(world)) == 1
    assert _binding(world).state == "held"

    # 8. After that PASS, the next stage re-binds and proceeds under the same mission id
    receipt_n1 = record_pass(world.store, **_scope(world))

    _close_worker(worker)
    _recreate_service(world)
    worker = open_worker(world)
    worker.tick()
    assert _outcome(world, "implement")["outcome"] == "succeeded"

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
