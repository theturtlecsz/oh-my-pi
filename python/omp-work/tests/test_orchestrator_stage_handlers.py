"""Missing stage handlers must pause missions instead of reporting success."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

import pytest

from omp_work.orchestrator.service import OrchestratorHandler, Outcome, _REGISTRY, submit
from omp_work.orchestrator.stages import ActionVerdict
from omp_work.orchestrator.verifier import accept_candidate, sign_verdict
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


def test_plain_audit_records_signed_intent_before_pass(service, tmp_path: Path) -> None:
    world = open_world(service, tmp_path)
    key = world["config"].verifier_key_path
    original = b"fedcba9876543210fedcba9876543210"
    key.write_bytes(original)
    key.chmod(0o644)
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _until(
        worker, world["config"], world["mission_id"],
        lambda item: item["status"] in ("paused", "completed"),
    )
    assert view["status"] == "completed"
    handler = OrchestratorHandler(world["config"])
    handler.mission_id = world["mission_id"]
    steps = handler.steps()
    audit = next(step for step in steps if step["kind"] == "outcome" and step["stage"] == "audit")
    intents = [step for step in steps if step["kind"] == "external_intent"]
    assert len(intents) == 1
    intent = intents[0]
    sha = hashlib.sha256(f"stub:{world['mission_id']}".encode()).hexdigest()
    attempt = str(uuid5(world["mission_id"], "stub-audit"))
    assert intent["step_index"] == audit["step_index"]
    assert intent["idempotency_key"] == f"audit-accept:{world['mission_id']}:{audit['step_index']}"
    assert intent["action"] == "sign_verdict"
    record = intent["target"]
    assert record["candidate_sha256"] == sha
    assert record["attempt_id"] == attempt
    assert record["verifier_id"] == "verifier"
    assert accept_candidate(
        key.read_bytes(), record,
        candidate_sha256=sha, attempt_id=attempt, producer_id="worker",
    )
    assert steps.index(intent) < steps.index(audit)
    assert audit["verdict"] == "pass"
    assert key.read_bytes() == original
    assert len(key.read_bytes()) == 32
    assert key.stat().st_mode & 0o777 == 0o644


def test_plain_audit_store_error_does_not_return_pass(tmp_path: Path) -> None:
    key = tmp_path / "verifier.key"
    key.write_bytes(b"0123456789abcdef0123456789abcdef")
    error = RuntimeError("intent store unavailable")

    def record_intent(*args) -> None:
        raise error

    ctx = SimpleNamespace(
        stage="audit", request={}, prior={}, mission_id=uuid4(), step_index=7,
        config=SimpleNamespace(verifier_key_path=key),
        intents=SimpleNamespace(record_intent=record_intent),
    )
    returned = None
    with pytest.raises(RuntimeError) as raised:
        returned = run_stage(ctx)
    assert raised.value is error
    assert returned is None


@pytest.mark.parametrize(
    ("stage", "controls", "prior"),
    [
        ("audit", {"fails": 1}, {}),
        ("audit", {"fails": 1}, {"audit": {"visits": 1}}),
        ("audit", {"once": True, "blocker": "blocked"}, {}),
        ("audit", {"once": True, "action": {"tier": 2, "action_class": "write"}}, {}),
        ("audit", {"once": True, "new_scope": True}, {}),
        ("audit", {"once": True, "budget_exhausted": True}, {}),
        ("implement", {}, {}),
    ],
)
def test_non_plain_stage_does_not_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, controls: dict, prior: dict,
) -> None:
    import omp_work.orchestrator.verifier as verifier

    def unexpected_call(*args, **kwargs) -> None:
        pytest.fail("non-plain stage signed or recorded an audit verdict")

    monkeypatch.setattr(verifier, "sign_verdict", unexpected_call)
    ctx = SimpleNamespace(
        stage=stage, request={"controls": {stage: controls}}, prior=prior,
        mission_id=uuid4(), step_index=7,
        config=SimpleNamespace(verifier_key_path=tmp_path / "verifier.key"),
        intents=SimpleNamespace(record_intent=unexpected_call),
    )
    result = run_stage(ctx)
    assert result.outcome == ("failed" if controls.get("fails") and not prior else "succeeded")


def test_plain_audit_without_step_metadata_still_records_signed_pass(tmp_path: Path) -> None:
    key = tmp_path / "verifier.key"
    records = []

    def record_intent(intent_key: str, action: str, target: dict[str, str]) -> None:
        records.append((intent_key, action, target))

    ctx = SimpleNamespace(
        stage="audit", request={}, prior={},
        config=SimpleNamespace(verifier_key_path=key),
        intents=SimpleNamespace(record_intent=record_intent),
    )

    result = run_stage(ctx)

    assert result.verdict == "pass"
    intent_key, action, record = records.pop()
    assert not records
    mission_id = UUID(int=0)
    assert intent_key == f"audit-accept:{mission_id}:0"
    assert action == "sign_verdict"
    assert accept_candidate(
        key.read_bytes(), record,
        candidate_sha256=hashlib.sha256(f"stub:{mission_id}".encode()).hexdigest(),
        attempt_id=str(uuid5(mission_id, "stub-audit")), producer_id="worker",
    )


def _audit_acceptance_context(tmp_path: Path) -> SimpleNamespace:
    key = b"0123456789abcdef0123456789abcdef"
    key_path = tmp_path / "acceptance.key"
    key_path.write_bytes(key)
    target = sign_verdict(
        key, candidate_sha256="candidate-sha", attempt_id="attempt-id",
        verdict="pass", verifier_id="verifier",
    )
    return SimpleNamespace(
        prior={}, mission_id=uuid4(), step_index=7,
        principal=object(), workspace_id=uuid4(),
        config=SimpleNamespace(verifier_key_path=key_path),
        service=SimpleNamespace(read=Mock()),
        intents=SimpleNamespace(open=Mock(return_value={"target": target})),
    )


def test_audit_acceptance_workflow_read_failure_never_opens_intent(tmp_path: Path) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    ctx.prior = {"intake": {"data": {"work_key": "work-key"}}}
    ctx.service.read.side_effect = OSError("workflow unavailable")

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is False
    ctx.service.read.assert_called_once_with(ctx.principal, ctx.workspace_id, "workflow", "work-key")
    ctx.intents.open.assert_not_called()


@pytest.mark.parametrize("failure", ["open", "read_bytes"])
def test_audit_acceptance_intent_io_failure_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    if failure == "open":
        ctx.intents.open.side_effect = OSError("intent unavailable")
    else:
        monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=OSError("key unavailable")))

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is False
    ctx.intents.open.assert_called_once_with(f"audit-accept:{ctx.mission_id}:{ctx.step_index}")


@pytest.mark.parametrize(
    ("receipt_patch", "accepted"),
    [
        ({}, True),
        ({"verdict": "pass"}, False),
        ({"independent": 1}, False),
        ({"independent": False}, False),
        ({"candidate_sha256": "other-sha"}, False),
        ({"kind": "evaluate"}, False),
    ],
)
def test_audit_acceptance_workflow_receipt_takes_precedence(
    tmp_path: Path, receipt_patch: dict, accepted: bool,
) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    ctx.prior = {"intake": {"data": {"work_key": "work-key"}}}
    receipt = {
        "kind": "audit", "independent": True, "verdict": "PASS",
        "candidate_sha256": "candidate-sha",
    }
    receipt.update(receipt_patch)
    ctx.service.read.return_value = {
        "item": {"candidate": {"candidate_sha256": "candidate-sha"}},
        "receipts": [None, receipt],
    }

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is accepted
    ctx.intents.open.assert_not_called()


def test_audit_acceptance_workflow_without_receipt_denies_valid_intent(tmp_path: Path) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    ctx.prior = {"intake": {"data": {"work_key": "work-key"}}}
    ctx.service.read.return_value = {
        "item": {"candidate": {"candidate_sha256": "candidate-sha"}},
        "receipts": [],
    }

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is False
    ctx.intents.open.assert_not_called()


@pytest.mark.parametrize(
    "prior",
    [
        {}, {"intake": None}, {"intake": {"data": []}},
        {"intake": {"data": {}}}, {"intake": {"data": {"work_key": ""}}},
        {"intake": {"data": {"work_key": 7}}},
    ],
)
def test_audit_acceptance_without_work_key_verifies_intent_target(
    tmp_path: Path, prior: dict,
) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    ctx.prior = prior

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is True
    ctx.service.read.assert_not_called()
    ctx.intents.open.assert_called_once_with(f"audit-accept:{ctx.mission_id}:{ctx.step_index}")


@pytest.mark.parametrize(
    "view", [{}, {"item": {}}, {"item": {"candidate": {"candidate_sha256": ""}}}],
)
def test_audit_acceptance_without_workflow_candidate_verifies_intent_target(
    tmp_path: Path, view: dict,
) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    ctx.prior = {"intake": {"data": {"work_key": "work-key"}}}
    ctx.service.read.return_value = view

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is True
    ctx.intents.open.assert_called_once_with(f"audit-accept:{ctx.mission_id}:{ctx.step_index}")


@pytest.mark.parametrize("invalid", ["empty_key", "worker", "signature", "target", "sha", "attempt"])
def test_audit_acceptance_rejects_untrusted_intent_target(tmp_path: Path, invalid: str) -> None:
    ctx = _audit_acceptance_context(tmp_path)
    target = ctx.intents.open.return_value["target"]
    if invalid == "empty_key":
        ctx.config.verifier_key_path.write_bytes(b"")
    elif invalid == "worker":
        ctx.intents.open.return_value["target"] = sign_verdict(
            ctx.config.verifier_key_path.read_bytes(),
            candidate_sha256=target["candidate_sha256"], attempt_id=target["attempt_id"],
            verdict="pass", verifier_id="worker",
        )
    elif invalid == "signature":
        target["signature"] = "invalid"
    elif invalid == "target":
        ctx.intents.open.return_value["target"] = None
    else:
        target["candidate_sha256" if invalid == "sha" else "attempt_id"] = None

    assert OrchestratorHandler.__new__(OrchestratorHandler)._audit_accepted(ctx) is False


def _audit_without_intent_pauses(service, tmp_path: Path, produced: Outcome, rule_id: str) -> list[dict]:
    world = open_world(service, tmp_path)
    OrchestratorHandler(world["config"])
    world["config"] = replace(world["config"], operations=())
    _REGISTRY["audit"] = lambda ctx: produced
    submit(world["config"], world["request"])
    worker = _worker(service, world["config"], world["workspace_id"])
    view = _until(
        worker, world["config"], world["mission_id"],
        lambda item: item["status"] in ("paused", "completed"),
    )
    assert view["status"] == "paused"
    assert [step["rule_id"] for step in view["steps"] if step["kind"] == "pause"] == [rule_id]
    assert not any(step["stage"] == "merge" for step in view["steps"])
    assert not any(step["rule_id"] == "mission-complete" for step in view["steps"])
    handler = OrchestratorHandler(world["config"])
    handler.mission_id = world["mission_id"]
    steps = handler.steps()
    assert not any(step["kind"] == "external_intent" for step in steps)
    audits = [step for step in steps if step["kind"] == "outcome" and step["stage"] == "audit"]
    assert audits
    return audits


def test_audit_succeeded_none_reaches_repair_bound(service, tmp_path: Path) -> None:
    audits = _audit_without_intent_pauses(
        service, tmp_path, Outcome(outcome="succeeded", verdict="none"), "repair-bound",
    )
    assert all(step["outcome"] == "succeeded" and step["verdict"] == "none" for step in audits)


def test_audit_pass_without_acceptance_reaches_repair_bound(service, tmp_path: Path) -> None:
    audits = _audit_without_intent_pauses(
        service, tmp_path, Outcome(outcome="succeeded", verdict="pass"), "repair-bound",
    )
    for audit in audits:
        assert audit["outcome"] == "failed"
        assert audit["verdict"] == "fail"
        assert audit["data"] == {"stage": "audit", "code": "candidate_rejected"}
        assert audit["blocker"] is None


@pytest.mark.parametrize(
    ("condition", "rule_id"),
    [
        ({"blocker": "needs-owner"}, "unrecoverable-blocker"),
        ({"action": ActionVerdict(3, "unlisted", None, False, "held")}, "d35-tier3"),
        ({"new_scope": True}, "d21-new-scope"),
        ({"budget_exhausted": True}, "budget-exhausted"),
    ],
)
def test_audit_pass_with_extra_condition_is_left_alone(
    service, tmp_path: Path, condition: dict, rule_id: str,
) -> None:
    audits = _audit_without_intent_pauses(
        service, tmp_path, Outcome(outcome="succeeded", verdict="pass", **condition), rule_id,
    )
    assert len(audits) == 1
    audit = audits[0]
    assert audit["outcome"] == "succeeded"
    assert audit["verdict"] == "pass"
    assert audit["data"] == {}
    for field, value in condition.items():
        if field == "action":
            assert audit[field] == {
                "tier": value.tier, "action_class": value.action_class,
                "target_sha256": value.target_sha256, "allowed": value.allowed, "code": value.code,
            }
        else:
            assert audit[field] == value
