"""PostgreSQL integration tests for recursive transfer arm execution."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research import strategy_protocol
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_execution import run_arm
from omp_work.research.recursive_transfer_execution_evidence import BoundStep
from omp_work.research.recursive_transfer_execution_inputs import (
    ExecutionPayload,
    load_payload,
    prepare_execution_cohorts,
)
from omp_work.research.recursive_transfer_execution_reservation import reserve_execution
from recursive_transfer_execution_support import build_execution, seal_step

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def execution(native_jobs):
    return build_execution(native_jobs)


def _prepare_and_reserve(execution):
    cohort = execution.cohort()
    prepared, = prepare_execution_cohorts(
        (cohort,), execution.native, (execution.baseline.study, execution.candidate.study),
    )
    assert prepared.eligible, prepared.reasons
    payload = load_payload(prepared, execution.native)
    ledger = reserve_execution(execution.native, prepared)
    assert ledger.comparison_allowed is True
    return prepared, payload, ledger


def test_receipt_driven_picks_ignore_tree_metric_edits(execution):
    # a is opened with receipt score 0.6.
    # baseline threshold is 0.8: 0.6 < 0.8 -> opens root "b".
    # candidate threshold is 0.5: 0.6 >= 0.5 -> extends "a" to "a1".
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    # Corrupt payload tree metrics to opposite values.
    # If policy inspected tree metrics instead of receipts:
    # baseline would see metric 0.99 (>= 0.8) and pick a1,
    # candidate would see metric 0.01 (< 0.5) and pick b.
    tampered_steps = dict(payload.steps)
    tampered_steps["a"] = dict(tampered_steps["a"])
    tampered_steps["a"]["metric"] = 0.99
    tampered_payload = ExecutionPayload(steps=tampered_steps, opened=payload.opened)

    # 1. Baseline ignores tree metric 0.99 and uses receipt score 0.6 -> picks "b"
    base_exec = run_arm(execution.baseline, prepared, tampered_payload, execution.native, ledger, "baseline")
    assert base_exec.selection == "b"
    assert base_exec.invocation_count == 1
    assert base_exec.failures == ()
    assert isinstance(base_exec.selected_bound_step, BoundStep)
    assert base_exec.selected_bound_step.metric == 0.7
    assert str(execution.arms["a"][0].trial_id) in base_exec.verified_trial_ids
    assert str(execution.arms["b"][0].trial_id) in base_exec.verified_trial_ids

    # 2. Candidate with tree metric corrupted to 0.01 still uses receipt score 0.6 -> picks "a1"
    tampered_steps["a"]["metric"] = 0.01
    cand_exec = run_arm(execution.candidate, prepared, tampered_payload, execution.native, ledger, "candidate")
    assert cand_exec.selection == "a1"
    assert cand_exec.invocation_count == 1
    assert cand_exec.failures == ()
    assert isinstance(cand_exec.selected_bound_step, BoundStep)
    assert cand_exec.selected_bound_step.metric == 0.8
    assert str(execution.arms["a"][1].trial_id) in cand_exec.verified_trial_ids
    assert str(execution.arms["a1"][1].trial_id) in cand_exec.verified_trial_ids


def test_nondefault_seed_and_width_reach_proposal(execution, monkeypatch):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    cohort = execution.cohort().model_copy(update={"seed": 777, "width": 4})
    prepared, = prepare_execution_cohorts(
        (cohort,), execution.native, (execution.baseline.study, execution.candidate.study),
    )
    assert prepared.seed == 777
    assert prepared.width == 4
    payload = load_payload(prepared, execution.native)
    ledger = reserve_execution(execution.native, prepared)

    received_params = {}
    real_run_candidate = strategy_protocol.run_candidate

    def spy_run_candidate(*args, **kwargs):
        received_params["seed"] = kwargs.get("seed")
        received_params["width"] = kwargs.get("width")
        return real_run_candidate(*args, **kwargs)

    monkeypatch.setattr(strategy_protocol, "run_candidate", spy_run_candidate)

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")
    assert cand_exec.selection == "a1"
    assert cand_exec.failures == ()
    assert received_params == {"seed": 777, "width": 4}


def test_controller_revocation_blocks_selection(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    # Revoke controller authority by expiring policy window in the past
    past = datetime.now(timezone.utc) - timedelta(days=2)
    revoked_native = NativeEvidence(
        execution.native.store,
        execution.native.workspace_id,
        execution.native.actor_id,
        execution.native.work_id,
        execution.native.data_dir,
        grant=execution.native.grant,
        policy=replace(execution.native.policy, expires_at=past),
        authority=execution.native.authority,
        action=execution.native.action,
        repositories=execution.native.repositories,
        operation_id=execution.native.operation_id,
        request_id=execution.native.request_id,
        history=execution.native.history,
    )

    cand_exec = run_arm(execution.candidate, prepared, payload, revoked_native, ledger, "candidate")
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 1
    assert len(cand_exec.failures) > 0
    assert any("window" in f or "policy" in f for f in cand_exec.failures)


def test_bad_opened_source_fails_despite_matching_selected_tar(execution):
    # "a" is opened with tampered source bytes in candidate tar.
    # "a1" is the step that candidate would select, and its tar is valid.
    seal_step(
        execution, "a",
        baseline_score=0.6, candidate_score=0.6,
        candidate_source=b"# altered candidate source code\n",
    )
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    cohort = execution.cohort()
    prepared, = prepare_execution_cohorts(
        (cohort,), execution.native, (execution.baseline.study, execution.candidate.study),
    )
    payload = load_payload(prepared, execution.native)
    ledger = reserve_execution(execution.native, prepared)

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")
    # Must fail during opened step binding BEFORE reaching policy
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 0
    assert cand_exec.failures == ("changed_source",)


def test_crash_preserves_measured_state_and_produces_no_scored_selection(execution, monkeypatch):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    def crashing_candidate(*args, **kwargs):
        raise RuntimeError("candidate_simulation_crash")

    monkeypatch.setattr(strategy_protocol, "run_candidate", crashing_candidate)

    usage_before = len(execution.candidate.result.state.usage)
    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")

    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 1
    assert cand_exec.failures == ("crashed",)
    # Usage accounting was measured and preserved on state
    assert len(cand_exec.state.usage) > usage_before


def test_refused_reservation_never_reaches_generated_policy(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    # Tamper ledger with refused reservation reason
    refused_ledger = replace(
        ledger,
        reservation=replace(ledger.reservation, reasons=("duplicate_trial",)),
    )

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, refused_ledger, "candidate")
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 0
    assert cand_exec.failures == ("duplicate_trial",)


def test_unadmitted_opened_trial_fails_arm(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    # Strip admitted trial IDs
    tampered_ledger = replace(ledger, newly_admitted_trial_ids=())

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, tampered_ledger, "candidate")
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 0
    assert cand_exec.failures == ("unadmitted_trial",)


def test_unadmitted_selected_trial_fails_arm(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    # Keep only opened trial "a" admitted, excluding selected trial "a1"
    opened_trial_id = str(execution.arms["a"][1].trial_id)
    tampered_ledger = replace(ledger, newly_admitted_trial_ids=(opened_trial_id,))

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, tampered_ledger, "candidate")
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 1
    assert cand_exec.failures == ("unadmitted_trial",)


def test_independent_request_id_usage_per_arm(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    base_exec = run_arm(execution.baseline, prepared, payload, execution.native, ledger, "baseline")
    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")
    assert base_exec.selection == "b"
    assert cand_exec.selection == "a1"

    with execution.native.store.transaction(execution.native.workspace_id, execution.native.actor_id) as cur:
        cur.execute(
            "SELECT request_id FROM omp_jobs.usage_events WHERE workspace_id=%s AND work_id=%s",
            (execution.native.workspace_id, execution.native.work_id),
        )
        requests = {row["request_id"] for row in cur.fetchall()}

    assert any(f"{execution.native.request_id}:baseline" in req for req in requests)
    assert any(f"{execution.native.request_id}:candidate" in req for req in requests)


def test_selected_step_verification_failure_yields_no_selection(execution):
    # Opened step "a" has valid source, but selected step "a1" has corrupted source in candidate tar
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(
        execution, "a1",
        baseline_score=0.8, candidate_score=0.8,
        candidate_source=b"# tampered selected candidate source\n",
    )
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    cohort = execution.cohort()
    prepared, = prepare_execution_cohorts(
        (cohort,), execution.native, (execution.baseline.study, execution.candidate.study),
    )
    payload = load_payload(prepared, execution.native)
    ledger = reserve_execution(execution.native, prepared)

    cand_exec = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")
    # Policy was invoked (count = 1), but binding selected step failed: no fallback pick or score
    assert cand_exec.selection is None
    assert cand_exec.selected_bound_step is None
    assert cand_exec.invocation_count == 1
    assert cand_exec.failures == ("changed_source",)
    # Opened step "a" was verified
    assert str(execution.arms["a"][1].trial_id) in cand_exec.verified_trial_ids
    # Selected step "a1" was NOT verified
    assert str(execution.arms["a1"][1].trial_id) not in cand_exec.verified_trial_ids


def test_invalid_arm_fails_before_execution(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)

    res = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "invalid_arm")
    assert res.selection is None
    assert res.selected_bound_step is None
    assert res.invocation_count == 0
    assert res.failures == ("invalid_trial_arm",)


def test_ineligible_prepared_task_never_reaches_policy(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)

    prepared, payload, ledger = _prepare_and_reserve(execution)
    ineligible_prepared = replace(prepared, eligible=False, reasons=("ineligible_confirmation",))

    res = run_arm(execution.candidate, ineligible_prepared, payload, execution.native, ledger, "candidate")
    assert res.selection is None
    assert res.selected_bound_step is None
    assert res.invocation_count == 0
    assert res.failures == ("ineligible_confirmation",)


def test_comparison_refusal_never_invokes_policy(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)
    prepared, payload, ledger = _prepare_and_reserve(execution)

    result = run_arm(execution.candidate, prepared, payload, execution.native,
                     replace(ledger, comparison_allowed=False), "candidate")

    assert result.invocation_count == 0
    assert result.selection is None
    assert result.selected_bound_step is None
    assert result.failures == ("comparison_not_allowed",)
    assert result.state == execution.candidate.result.state


@pytest.mark.parametrize("field", ("operation_id", "request_id"))
def test_missing_controller_identity_is_not_fabricated(execution, monkeypatch, field):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    seal_step(execution, "a1", baseline_score=0.8, candidate_score=0.8)
    seal_step(execution, "b", baseline_score=0.7, candidate_score=0.7)
    prepared, payload, ledger = _prepare_and_reserve(execution)
    monkeypatch.setattr(execution.native, field, None)

    result = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")

    assert result.invocation_count == 0
    assert result.selection is None
    assert result.selected_bound_step is None
    assert result.failures == ("missing_controller_identity",)


def test_empty_opened_set_loads_custody_protocol_and_verifies_pick(execution):
    execution.opened.clear()
    for step in ("a", "a1", "b"):
        seal_step(execution, step, baseline_score=0.6, candidate_score=0.6)
    prepared, payload, ledger = _prepare_and_reserve(execution)

    result = run_arm(execution.candidate, prepared, payload, execution.native, ledger, "candidate")

    assert result.invocation_count == 1
    assert result.selection in {"a", "b"}
    assert result.failures == ()
    assert result.selected_bound_step.metric == 0.6
    assert result.verified_trial_ids == (str(execution.arms[result.selection][1].trial_id),)
