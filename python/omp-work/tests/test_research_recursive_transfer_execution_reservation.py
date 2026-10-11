"""Execution reservation admits only new attempts and settles them once."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research import recursive_transfer_execution_reservation as execution_reservation
from omp_work.research.custody import replicate_masquerade
from omp_work.research.recursive_transfer_evidence import read_trial
from omp_work.research.recursive_transfer_execution_reservation import reserve_execution, settle_execution
from omp_work.research.recursive_transfer_freshness import PreparedStep, prepare_cohorts
from omp_work.research.recursive_transfer_history import reserve_task
from recursive_transfer_execution_support import build_execution, seal_step
from recursive_transfer_support import cache_artifact

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def execution(native_jobs):
    return build_execution(native_jobs)


def _prepare(execution):
    prepared, = prepare_cohorts(
        (execution.cohort(),), execution.native, (execution.baseline.study, execution.candidate.study),
    )
    return prepared


def _trial_ids(prepared):
    return [
        str(arm.trial_id)
        for step in prepared.steps
        for arm in (step.baseline, step.candidate)
        if arm is not None
    ]


def test_preconsumed_selected_baseline_blocks_comparison(monkeypatch, execution):
    seal_step(execution, "a")
    seal_step(execution, "b")
    prepared = _prepare(execution)
    assert prepared.eligible, prepared.reasons
    selected = prepared.steps[0]
    assert selected.baseline is not None
    # Consume the selected baseline on another holdout so the later steps stay fresh.
    reserve_task(execution.native, replace(
        prepared,
        task_id="preconsumed-baseline",
        content_sha256="ab" * 32,
        steps=(PreparedStep(selected.step_id, selected.baseline_trial_id, None, selected.baseline, None),),
    ))
    history = execution.native.history
    finishes = []
    real_finish = execution_reservation.finish_arm

    def spy_finish(native, trial_id, *, ok):
        finishes.append((str(trial_id), ok))
        return real_finish(native, trial_id, ok=ok)

    monkeypatch.setattr(execution_reservation, "finish_arm", spy_finish)
    ledger = reserve_execution(execution.native, prepared)
    fresh_step = prepared.steps[1]
    fresh_ids = [str(fresh_step.baseline.trial_id), str(fresh_step.candidate.trial_id)]
    selected_id = str(selected.baseline.trial_id)

    assert execution.native.history is history
    assert ledger.comparison_allowed is False
    assert selected_id not in ledger.newly_admitted_trial_ids
    assert selected_id not in ledger.pending_qualified_ids
    assert set(fresh_ids) <= set(ledger.newly_admitted_trial_ids)
    assert set(fresh_ids) <= set(ledger.pending_qualified_ids)
    assert "duplicate_trial" in ledger.reservation.reasons
    assert selected_id in history.snapshot()["attempts"]
    assert selected_id not in history.snapshot()["successes"]
    assert selected_id not in history.snapshot()["failures"]

    verified = _trial_ids(prepared)
    settlement = settle_execution(execution.native, ledger, verified, True)
    assert settlement.denominator == ledger.reservation.denominator + len(ledger.pending_qualified_ids)
    assert [trial_id for trial_id, _ok in finishes] == list(ledger.pending_qualified_ids)
    assert selected_id not in {trial_id for trial_id, _ok in finishes}
    assert all(ok is False for _trial_id, ok in finishes)
    snap = history.snapshot()
    for trial_id in fresh_ids:
        assert trial_id in snap["failures"]
        assert trial_id not in snap["successes"]
    assert selected_id in snap["attempts"]
    assert selected_id not in snap["successes"]
    assert selected_id not in snap["failures"]
    assert settle_execution(execution.native, ledger, verified, True).denominator == 0
    assert len(finishes) == len(ledger.pending_qualified_ids)
    assert execution.native.history is history


@pytest.mark.parametrize("field", ["receipt", "output"])
def test_cached_receipt_or_output_cannot_win(execution, native_jobs, field):
    baseline, candidate = seal_step(execution, "a", baseline_score=0.2, candidate_score=0.9)
    evidence = read_trial(execution.native, candidate.trial_id)
    digest = evidence.receipt_sha256 if field == "receipt" else evidence.output_sha256
    assert digest
    cache_artifact(native_jobs, digest, f"cache-{field}-{uuid4().hex}")
    prepared = _prepare(execution)
    history = execution.native.history
    ledger = reserve_execution(execution.native, prepared)
    cached_id = str(candidate.trial_id)
    fresh_id = str(baseline.trial_id)

    assert execution.native.history is history
    assert ledger.comparison_allowed is False
    assert replicate_masquerade(cached=True) in ledger.reservation.reasons
    assert cached_id not in ledger.newly_admitted_trial_ids
    assert cached_id not in ledger.pending_qualified_ids
    assert ledger.pending_qualified_ids == (fresh_id,)
    assert cached_id in history.snapshot()["failures"]
    assert ledger.reservation.denominator == 0

    settlement = settle_execution(execution.native, ledger, [cached_id, fresh_id], True)
    snap = history.snapshot()
    assert settlement.denominator == 1
    assert cached_id in snap["failures"]
    assert cached_id not in snap["successes"]
    assert fresh_id in snap["failures"]
    assert fresh_id not in snap["successes"]
    assert settle_execution(execution.native, ledger, [cached_id, fresh_id], True).denominator == 0
    assert cached_id in history.snapshot()["failures"]
    assert cached_id not in history.snapshot()["successes"]
    assert execution.native.history is history


def test_new_attempts_settle_once_including_unused_failures(monkeypatch, execution):
    seal_step(execution, "a")
    seal_step(execution, "b")
    prepared = _prepare(execution)
    assert prepared.eligible, prepared.reasons
    history = execution.native.history
    returned = []
    real_reserve = execution_reservation.reserve_task

    def spy_reserve(native, task):
        result = real_reserve(native, task)
        returned.append(result)
        return result

    finishes = []
    real_finish = execution_reservation.finish_arm

    def spy_finish(native, trial_id, *, ok):
        finishes.append((str(trial_id), ok))
        return real_finish(native, trial_id, ok=ok)

    monkeypatch.setattr(execution_reservation, "reserve_task", spy_reserve)
    monkeypatch.setattr(execution_reservation, "finish_arm", spy_finish)
    ledger = reserve_execution(execution.native, prepared)
    ids = _trial_ids(prepared)
    verified = ids[:3]
    unused = ids[3:]

    assert len(returned) == 1
    assert ledger.reservation is returned[0]
    assert execution.native.history is history
    assert ledger.comparison_allowed is True
    assert ledger.reservation.denominator == 0
    assert ledger.newly_admitted_trial_ids == tuple(ids)
    assert ledger.pending_qualified_ids == tuple(ids)
    settlement = settle_execution(execution.native, ledger, verified, True)
    assert settlement.denominator == len(ids)
    assert finishes == [(trial_id, trial_id in verified) for trial_id in ids]
    snap = history.snapshot()
    for trial_id in verified:
        assert trial_id in snap["successes"]
        assert trial_id not in snap["failures"]
    for trial_id in unused:
        assert trial_id in snap["failures"]
        assert trial_id not in snap["successes"]
    assert settle_execution(execution.native, ledger, ids, True).denominator == 0
    assert finishes == [(trial_id, trial_id in verified) for trial_id in ids]
    assert history.snapshot()["successes"] == snap["successes"]
    assert history.snapshot()["failures"] == snap["failures"]
    assert execution.native.history is history


def test_unqualified_arm_stays_a_counted_failure(monkeypatch, execution):
    baseline, candidate = seal_step(
        execution, "b", baseline_score=0.2, candidate_score=0.95, candidate_outcome="failed",
    )
    prepared = _prepare(execution)
    assert not prepared.eligible
    # Drop the prepare reasons so only the incomplete reservation can block comparison.
    prepared = replace(prepared, eligible=True, reasons=())
    history = execution.native.history
    finishes = []
    real_finish = execution_reservation.finish_arm

    def spy_finish(native, trial_id, *, ok):
        finishes.append(str(trial_id))
        return real_finish(native, trial_id, ok=ok)

    monkeypatch.setattr(execution_reservation, "finish_arm", spy_finish)
    ledger = reserve_execution(execution.native, prepared)
    failed_id = str(candidate.trial_id)
    qualified_id = str(baseline.trial_id)
    assert execution.native.history is history
    assert ledger.reservation.reasons == ()
    assert ledger.reservation.denominator == 1
    assert ledger.comparison_allowed is False
    assert ledger.newly_admitted_trial_ids == (qualified_id,)
    assert ledger.pending_qualified_ids == (qualified_id,)
    assert failed_id in history.snapshot()["failures"]
    assert failed_id not in history.snapshot()["attempts"]

    settlement = settle_execution(execution.native, ledger, [failed_id, qualified_id], True)
    snap = history.snapshot()
    assert settlement.denominator == 2
    assert finishes == [qualified_id]
    assert failed_id in snap["failures"]
    assert failed_id not in snap["successes"]
    assert qualified_id in snap["failures"]
    assert qualified_id not in snap["successes"]
    refused = history.snapshot()
    replay = reserve_execution(execution.native, prepared)
    assert replay.comparison_allowed is False
    assert replay.newly_admitted_trial_ids == ()
    assert replay.pending_qualified_ids == ()
    assert replay.reservation.denominator == 0
    assert history.snapshot()["attempts"] == refused["attempts"]
    assert history.snapshot()["successes"] == refused["successes"]
    assert history.snapshot()["failures"] == refused["failures"]
    assert settle_execution(execution.native, replay, [failed_id, qualified_id], True).denominator == 0
    assert settle_execution(execution.native, ledger, [failed_id, qualified_id], True).denominator == 0
    assert history.snapshot()["attempts"] == refused["attempts"]
    assert history.snapshot()["successes"] == refused["successes"]
    assert history.snapshot()["failures"] == refused["failures"]
    assert failed_id in history.snapshot()["failures"]
    assert failed_id not in history.snapshot()["successes"]
    assert execution.native.history is history


def test_refusal_blocks_comparison_of_otherwise_new_arms(execution):
    seal_step(execution, "a")
    prepared = replace(_prepare(execution), eligible=False, reasons=("confirmation_reused",))
    history = execution.native.history
    ledger = reserve_execution(execution.native, prepared)
    ids = _trial_ids(prepared)
    assert ledger.newly_admitted_trial_ids == tuple(ids)
    assert ledger.pending_qualified_ids == tuple(ids)
    assert ledger.comparison_allowed is False
    assert "confirmation_reused" in ledger.reservation.reasons
    settlement = settle_execution(execution.native, ledger, ids, True)
    assert settlement.denominator == len(ids)
    snap = history.snapshot()
    for trial_id in ids:
        assert trial_id in snap["failures"]
        assert trial_id not in snap["successes"]
    assert settle_execution(execution.native, ledger, ids, True).denominator == 0
    assert execution.native.history is history
