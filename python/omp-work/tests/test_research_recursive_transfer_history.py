"""Consumption of confirmation holdouts and arm identities on one history."""

import hashlib
import os
import secrets
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research.custody import replicate_masquerade
from omp_work.research.receipt import EvaluationProtocol
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_freshness import (
    CohortTask,
    PreparedStep,
    PreparedTask,
    StepArms,
    prepare_cohorts,
)
from omp_work.research.recursive_transfer_history import finish_arm, reserve_task
from omp_work.v1.canonical import sha256
from recursive_transfer_support import cache_artifact, register_artifact_bytes, register_task_dataset, seal_managed_trial
from test_research_managed_gate import _store

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture(scope="module")
def bundle(native_jobs):
    protocol = EvaluationProtocol(
        metrics=({"name": "score", "direction": "higher"},),
        primary="score",
        candidate_command=("python", "candidate.py"),
        evaluator_command=("python", "evaluator.py"),
        confirmation_sha256=sha256({"holdout": "history"}),
        timeout_seconds=60,
    )
    qualified = [
        seal_managed_trial(
            native_jobs,
            source_bytes=f"# history arm {index}\n".encode(),
            metric=0.4 + index * 0.2,
            protocol=protocol,
            dataset_id="unused",
        )
        for index in range(2)
    ]
    failed = seal_managed_trial(
        native_jobs,
        source_bytes=b"# history failed arm\n",
        metric=0.1,
        protocol=protocol,
        dataset_id="unused",
        outcome="failed",
        trust="untrusted",
    )
    return SimpleNamespace(protocol=protocol, qualified=qualified, failed=failed)


def _native(native_jobs) -> NativeEvidence:
    return NativeEvidence(
        _store(native_jobs),
        native_jobs.workspace_id,
        native_jobs.actor_id,
        native_jobs.item["work_id"],
        native_jobs.service.config.data_dir,
    )


def _prepare(native_jobs, bundle, native, baseline, candidate, *, payload=None, task_id=None) -> PreparedTask:
    protocol = bundle.protocol
    task = CohortTask(
        phase="confirmation",
        domain="machine_learning",
        metric="score",
        direction="higher",
        protocol_sha256=hashlib.sha256(protocol.protocol_bytes()).hexdigest(),
        seed=0,
        width=1,
        budget=2,
        dataset_id=register_task_dataset(
            native_jobs, task_id or uuid4().hex, payload if payload is not None else {"query": uuid4().hex},
        ),
        steps=(StepArms("root", baseline, candidate),),
    )
    prepared, = prepare_cohorts((task,), native, ())
    return prepared


def _relabel(arm, *, qualified=None, receipt=None, output=None, trial_id=None):
    receipt_sha = receipt if receipt is not None else secrets.token_hex(32)
    output_sha = output if output is not None else secrets.token_hex(32)
    trial = str(trial_id or uuid4())
    is_qualified = arm.qualified if qualified is None else qualified
    return replace(
        arm,
        trial_id=trial,
        job_id=f"managed-trial:{trial}",
        receipt_sha256=receipt_sha,
        receipt_id=receipt_sha,
        output_sha256=output_sha,
        output_id=output_sha,
        qualified=is_qualified,
        reasons=() if is_qualified else ("candidate_failed",),
    )


def _swap(prepared: PreparedTask, baseline, candidate, **changes) -> PreparedTask:
    step = prepared.steps[0]
    return replace(
        prepared,
        steps=(PreparedStep(step.step_id, baseline.trial_id, candidate.trial_id, baseline, candidate),),
        **changes,
    )


def _holdout(prepared: PreparedTask) -> tuple[str, str]:
    assert prepared.content_sha256 is not None
    assert prepared.task_id is not None
    return (prepared.content_sha256, prepared.task_id)


def test_qualified_pair_shares_one_holdout_and_stores_both_trials(native_jobs, bundle):
    native = _native(native_jobs)
    history = native.history
    baseline, candidate = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, baseline.trial_id, candidate.trial_id, task_id="pair-task")
    assert prepared.eligible, prepared.reasons
    step = prepared.steps[0]
    # Sealed fixture arms share output bytes; independent replicates must not.
    prepared = replace(
        prepared,
        steps=(replace(step, candidate=replace(step.candidate, output_sha256="a" * 64, output_id="a" * 64)),),
    )
    step = prepared.steps[0]

    reserved = reserve_task(native, prepared)

    assert native.history is history
    assert reserved.denominator == 0
    assert reserved.replicates == 2
    assert reserved.baseline_trial_id == step.baseline_trial_id == baseline.trial_id
    assert reserved.candidate_trial_id == step.candidate_trial_id == candidate.trial_id
    assert reserved.steps[0].baseline_trial_id == baseline.trial_id
    assert reserved.steps[0].candidate_trial_id == candidate.trial_id
    holdout = _holdout(prepared)
    assert holdout in native.history.confirmation_identities
    assert len(native.history.confirmation_identities) == 1
    snap = native.history.snapshot()
    for arm in (step.baseline, step.candidate):
        assert arm.trial_id in native.history.consumed
        assert arm.job_id in native.history.consumed
        assert arm.receipt_sha256 in native.history.consumed
        assert arm.output_sha256 in native.history.consumed
        assert arm.trial_id in snap["attempts"]
        assert arm.job_id in snap["attempts"]
        assert arm.receipt_sha256 in snap["attempts"]
        assert arm.trial_id not in snap["failures"]
        assert arm.trial_id not in snap["successes"]
    assert step.baseline.output_sha256 in snap["attempts"]
    assert snap["duplicates"] == []


def test_finish_arm_adds_one_once(native_jobs, bundle):
    native = _native(native_jobs)
    history = native.history
    baseline, candidate = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, baseline.trial_id, candidate.trial_id)
    step = prepared.steps[0]
    prepared = replace(
        prepared,
        steps=(replace(step, candidate=replace(step.candidate, output_sha256="a" * 64, output_id="a" * 64)),),
    )
    reserve_task(native, prepared)
    step = prepared.steps[0]

    assert finish_arm(native, step.baseline_trial_id, ok=True).denominator == 1
    assert finish_arm(native, step.baseline_trial_id, ok=True).denominator == 0
    assert finish_arm(native, step.baseline_trial_id, ok=False).denominator == 0
    assert finish_arm(native, step.candidate_trial_id, ok=False).denominator == 1
    assert finish_arm(native, step.candidate_trial_id, ok=True).denominator == 0
    assert native.history is history

    snap = native.history.snapshot()
    assert str(step.baseline_trial_id) in snap["successes"]
    assert str(step.candidate_trial_id) in snap["failures"]
    assert str(step.candidate_trial_id) in native.history.consumed
    assert str(step.baseline_trial_id) in snap["attempts"]


def test_unqualified_attempt_adds_one_and_retry_adds_zero(native_jobs, bundle):
    native = _native(native_jobs)
    history = native.history
    baseline, _candidate = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, baseline.trial_id, bundle.failed.trial_id)
    assert not prepared.eligible
    step = prepared.steps[0]
    # The sealed helper stamps every arm with the same output digest. Split them
    # so each arm's output is consumed on its own.
    qualified = replace(step.baseline, output_sha256="a" * 64, output_id="a" * 64)
    failed = replace(step.candidate, output_sha256="b" * 64, output_id="b" * 64)
    prepared = _swap(prepared, qualified, failed)

    first = reserve_task(native, prepared)
    assert native.history is history
    assert first.denominator == 1
    assert first.replicates == 2
    assert first.baseline_trial_id == qualified.trial_id
    assert first.candidate_trial_id == failed.trial_id
    snap = native.history.snapshot()
    assert qualified.trial_id in snap["attempts"]
    assert qualified.trial_id not in snap["failures"]
    assert failed.trial_id in snap["failures"]
    assert failed.job_id in snap["failures"]
    assert failed.receipt_sha256 in snap["failures"]
    assert failed.output_sha256 in snap["failures"]
    assert failed.trial_id not in snap["attempts"]
    assert failed.trial_id in native.history.consumed

    consumed = set(native.history.consumed)
    retry = reserve_task(native, prepared)
    assert retry.denominator == 0
    assert retry.replicates == 0
    assert set(native.history.consumed) == consumed
    assert _holdout(prepared) in native.history.confirmation_identities
    assert len(native.history.confirmation_identities) == 1
    duplicated = native.history.snapshot()["duplicates"]
    assert ["__tuple__", prepared.content_sha256, prepared.task_id] in duplicated
    assert failed.trial_id in duplicated
    assert qualified.job_id in duplicated
    assert qualified.receipt_sha256 in duplicated
    assert failed.output_sha256 in duplicated

    assert finish_arm(native, failed.trial_id, ok=True).denominator == 0
    assert failed.trial_id in native.history.snapshot()["failures"]
    assert failed.trial_id in native.history.consumed
    assert finish_arm(native, qualified.trial_id, ok=True).denominator == 1


def test_restored_snapshot_rejects_the_same_holdout(native_jobs, bundle):
    native = _native(native_jobs)
    baseline, _candidate = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, baseline.trial_id, bundle.failed.trial_id, task_id="restore-task")
    step = prepared.steps[0]
    prepared = _swap(
        prepared,
        replace(step.baseline, output_sha256="c" * 64, output_id="c" * 64),
        replace(step.candidate, output_sha256="d" * 64, output_id="d" * 64),
    )
    before = native.snapshot()
    assert reserve_task(native, prepared).denominator == 1
    consumed = native.snapshot()

    native.history.note_confirmation(("noise", "noise"))
    native.restore(consumed)
    assert ("noise", "noise") not in native.history.confirmation_identities
    rejected = reserve_task(native, prepared)
    assert rejected.denominator == 0
    assert rejected.replicates == 0
    assert ["__tuple__", prepared.content_sha256, prepared.task_id] in native.history.snapshot()["duplicates"]

    native.restore(before)
    assert reserve_task(native, prepared).denominator == 1
    native.restore(consumed)
    other = _native(native_jobs)
    other.restore(consumed)
    assert other.history is not native.history
    replay = reserve_task(other, prepared)
    assert replay.denominator == 0
    assert replay.replicates == 0
    assert _holdout(prepared) in other.history.confirmation_identities


def test_new_trial_or_dataset_label_still_adds_zero(native_jobs, bundle):
    native = _native(native_jobs)
    baseline, candidate = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, baseline.trial_id, candidate.trial_id, task_id="label-task")
    assert reserve_task(native, prepared).replicates == 1
    holdout = _holdout(prepared)

    relabeled = _swap(
        prepared,
        _relabel(prepared.steps[0].baseline),
        _relabel(prepared.steps[0].candidate),
        dataset_id="e" * 64,
    )
    same_holdout = reserve_task(native, relabeled)
    assert same_holdout.denominator == 0
    assert same_holdout.replicates == 0
    assert "duplicate_holdout" in same_holdout.reasons
    assert relabeled.steps[0].baseline.trial_id not in native.history.consumed
    assert len(native.history.confirmation_identities) == 1
    assert holdout in native.history.confirmation_identities

    fresh = _native(native_jobs)
    original = _prepare(native_jobs, bundle, fresh, baseline.trial_id, candidate.trial_id, task_id="output-task")
    assert reserve_task(fresh, original).replicates == 1
    new_holdout = _prepare(
        native_jobs, bundle, fresh, baseline.trial_id, candidate.trial_id, task_id="output-task-renamed",
    )
    assert _holdout(new_holdout) != _holdout(original)
    reused = reserve_task(fresh, new_holdout)
    assert reused.denominator == 0
    assert reused.replicates == 0
    assert "duplicate_trial" in reused.reasons
    assert "duplicate_job" in reused.reasons
    assert "duplicate_receipt" in reused.reasons
    assert "duplicate_output" in reused.reasons
    assert "duplicate_holdout" not in reused.reasons
    assert _holdout(new_holdout) in fresh.history.confirmation_identities


@pytest.mark.parametrize(
    ("kind", "field", "across_steps"),
    [
        ("trial", "trial_id", False),
        ("job", "job_id", False),
        ("receipt", "receipt_sha256", False),
        ("output", "output_sha256", False),
        ("trial", "trial_id", True),
    ],
)
def test_same_reservation_replay_adds_zero(native_jobs, bundle, kind, field, across_steps):
    native = _native(native_jobs)
    left, right = bundle.qualified
    prepared = _prepare(native_jobs, bundle, native, left.trial_id, right.trial_id)
    source = prepared.steps[0].baseline
    first = _relabel(source, qualified=True)
    replay = replace(_relabel(source, qualified=False), **{field: getattr(first, field)})
    if across_steps:
        prepared = replace(
            prepared,
            steps=(
                PreparedStep("root", first.trial_id, None, first, None),
                PreparedStep("child", replay.trial_id, None, replay, None),
            ),
        )
    else:
        prepared = _swap(prepared, first, replay)

    reserved = reserve_task(native, prepared)

    assert reserved.denominator == 0
    assert reserved.replicates == 1
    assert f"duplicate_{kind}" in reserved.reasons
    assert getattr(first, field) in native.history.snapshot()["duplicates"]
    assert first.trial_id in native.history.snapshot()["attempts"]
    assert replay.trial_id not in native.history.snapshot()["failures"]

    restored = _native(native_jobs)
    restored.restore(native.snapshot())
    retried = reserve_task(restored, prepared)
    assert retried.denominator == 0
    assert retried.replicates == 0
    assert f"duplicate_{kind}" in retried.reasons


def test_cached_artifact_under_a_new_trial_adds_zero(native_jobs, bundle):
    left, right = bundle.qualified
    control_native = _native(native_jobs)
    control_task = _prepare(native_jobs, bundle, control_native, left.trial_id, right.trial_id)
    source = control_task.steps[0].baseline
    control = reserve_task(
        control_native,
        _swap(
            control_task,
            _relabel(source, qualified=True),
            _relabel(source, qualified=False),
            eligible=True,
            reasons=(),
        ),
    )
    assert control.denominator == 1
    assert control.replicates == 2

    for field in ("receipt", "output"):
        native = _native(native_jobs)
        task = _prepare(native_jobs, bundle, native, left.trial_id, right.trial_id, task_id=f"cache-{field}")
        cached_digest = register_artifact_bytes(native_jobs, f"cached-{field}".encode(), f"cached-{field}.json")
        other_digest = register_artifact_bytes(native_jobs, f"other-{field}".encode(), f"other-{field}.json")
        cache_artifact(native_jobs, cached_digest, f"cache-{field}-{uuid4().hex}")
        receipt = cached_digest if field == "receipt" else other_digest
        output = cached_digest if field == "output" else other_digest
        evidence = task.steps[0].baseline
        fresh_arm = _relabel(evidence, qualified=True)
        cached_arm = _relabel(evidence, qualified=False, receipt=receipt, output=output)
        reserved = reserve_task(native, _swap(task, fresh_arm, cached_arm, eligible=True, reasons=()))
        assert reserved.baseline_trial_id == fresh_arm.trial_id
        assert reserved.candidate_trial_id == cached_arm.trial_id
        assert reserved.denominator == 0
        assert reserved.replicates == 1
        assert replicate_masquerade(cached=True) in reserved.reasons
        assert cached_arm.trial_id not in native.history.snapshot()["attempts"]
        assert cached_arm.trial_id in native.history.snapshot()["failures"]
        assert cached_arm.trial_id in native.history.consumed
        assert fresh_arm.trial_id in native.history.snapshot()["attempts"]
        assert finish_arm(native, cached_arm.trial_id, ok=True).denominator == 0
        assert cached_arm.trial_id in native.history.snapshot()["failures"]
        assert cached_arm.trial_id in native.history.consumed
