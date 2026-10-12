"""Missing stage handlers must pause missions instead of reporting success."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from omp_work.orchestrator.service import OrchestratorHandler, _REGISTRY, submit
from orchestrator_test_ops import run_stage
from test_orchestrator_service import _until, _worker, open_world

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(autouse=True)
def restore_registry() -> Iterator[None]:
    saved = _REGISTRY.copy()
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)


def _assert_missing_handler_pauses(service, world: dict, stage: str) -> None:
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _until(
        worker,
        world["config"],
        world["mission_id"],
        lambda item: item["status"] in ("paused", "completed"),
    )
    assert view["status"] == "paused"
    pauses = [step for step in view["steps"] if step["kind"] == "pause"]
    assert [(step["stage"], step["rule_id"]) for step in pauses] == [
        (stage, "unrecoverable-blocker")
    ]
    assert not any(step["rule_id"] == "mission-complete" for step in view["steps"])

    handler = OrchestratorHandler(world["config"])
    handler.mission_id = world["mission_id"]
    outcomes = [
        step for step in handler.steps()
        if step["kind"] == "outcome" and step["stage"] == stage
    ]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["outcome"] == "failed"
    assert outcome["verdict"] == "none"
    assert outcome["blocker"] == "missing_stage_handler"
    assert outcome["data"] == {"stage": stage, "code": "missing_stage_handler"}


def test_missing_evaluate_pauses_unrecoverable_blocker(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    OrchestratorHandler(world["config"])
    world["config"] = replace(world["config"], operations=())
    _REGISTRY.pop("evaluate")
    _assert_missing_handler_pauses(service, world, "evaluate")


def test_missing_audit_handler_fails_closed(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    OrchestratorHandler(world["config"])
    world["config"] = replace(world["config"], operations=())
    _REGISTRY.pop("audit")
    _assert_missing_handler_pauses(service, world, "audit")


def test_plain_audit_empty_existing_key_is_0600(tmp_path: Path) -> None:
    key = tmp_path / "verifier.key"
    key.write_text("")
    key.chmod(0o644)
    assert key.stat().st_size == 0
    ctx = SimpleNamespace(
        stage="audit", request={}, prior={},
        config=SimpleNamespace(verifier_key_path=key),
        intents=SimpleNamespace(record_intent=lambda *args, **kwargs: None),
    )

    run_stage(ctx)

    assert key.stat().st_mode & 0o777 == 0o600
    assert len(key.read_bytes()) == 32


def test_plain_audit_creates_missing_key_as_0600(tmp_path: Path) -> None:
    key = tmp_path / "verifier.key"
    assert not key.exists()
    ctx = SimpleNamespace(
        stage="audit", request={}, prior={},
        config=SimpleNamespace(verifier_key_path=key),
        intents=SimpleNamespace(record_intent=lambda *args, **kwargs: None),
    )

    run_stage(ctx)

    assert len(key.read_bytes()) == 32
    assert key.stat().st_mode & 0o777 == 0o600


def test_plain_audit_keeps_existing_key_bytes_and_mode(tmp_path: Path) -> None:
    key = tmp_path / "verifier.key"
    original = b"fedcba9876543210fedcba9876543210"
    key.write_bytes(original)
    key.chmod(0o644)
    ctx = SimpleNamespace(
        stage="audit", request={}, prior={},
        config=SimpleNamespace(verifier_key_path=key),
        intents=SimpleNamespace(record_intent=lambda *args, **kwargs: None),
    )

    run_stage(ctx)

    assert key.read_bytes() == original
    assert key.stat().st_mode & 0o777 == 0o644
