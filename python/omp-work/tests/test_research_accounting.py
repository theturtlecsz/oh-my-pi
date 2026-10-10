# ruff: noqa: F811
"""OMP-318-s05: R18 qualification gates and the research usage ledger.

Each test defends one observable R18 contract:
- recording past max_usd still writes the crossing usage event to
  omp_jobs.usage_events, and the admission surface answers quota_exceeded;
- a comparison with unequal cpu is invalid_comparison, a 10x claim needs two
  independent runs, and both receipts must verify under the preregistered
  protocol digest;
- an accepted negative control surfaces as negative_acceptance;
- injected faults publish their kinds and denominators, and a lone 10x claim
  is unsupported_claim;
- an intervention may only narrow a standing policy;
- a canary refuses an untested release identity and reports drift beyond
  tolerance.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.jobs.store import NativeJobStore
from omp_work.research.accounting import (
    ResourceAccounting,
    ResourceLimits,
    audit_fault_tolerance,
    evaluate_comparison,
    inject_fault,
    intervene_policy,
    matched_resources,
    protected_confirmation,
    register_comparison,
    run_canary,
)
from omp_work.research.receipt import EvaluationProtocol, TrialReceipt
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import PolicyRefused, StandingPolicy

pytest_plugins = ("test_workflow_service",)

PROTOCOL_SHA = "dd" * 32
OTHER_PROTOCOL_SHA = "ee" * 32
RESOURCES = {"cpu": 1, "memory_mib": 512, "gpu": 0, "model_calls": 4}


def _receipt(
    *,
    score: float,
    protocol_sha: str = PROTOCOL_SHA,
    trial_id: str,
    verdict: str = "qualified",
    schema_version: int = 1,
) -> TrialReceipt:
    return TrialReceipt.model_validate(
        {
            "schema_version": schema_version,
            "trial_id": trial_id,
            "campaign_id": "camp-1",
            "job_id": f"managed-trial:{trial_id}",
            "candidate_digest": "aa" * 32,
            "evaluator_component_sha256": "bb" * 32,
            "evaluator_artifact_sha256": "cc" * 32,
            "protocol_sha256": protocol_sha,
            "negative_control": "rejected",
            "metrics": {"score": score},
            "verdict": verdict,
        }
    )


def _prereg(independent_n: int, direction: str = "higher"):
    return register_comparison(
        "cmp-1", PROTOCOL_SHA, "score", direction, independent_n
    )


def _protocol() -> EvaluationProtocol:
    return EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [
                {"name": "score", "direction": "higher"},
                {"name": "loss", "direction": "lower"},
            ],
            "primary": "score",
            "candidate_command": ["python", "candidate.py"],
            "evaluator_command": ["python", "evaluator.py"],
            "confirmation_sha256": "ab" * 32,
            "timeout_seconds": 60,
        }
    )


def _bound_receipt(protocol: EvaluationProtocol, **overrides: object) -> TrialReceipt:
    payload: dict[str, object] = {
        "schema_version": 1,
        "trial_id": "trial-1",
        "campaign_id": "camp-1",
        "job_id": "managed-trial:trial-1",
        "candidate_digest": "aa" * 32,
        "evaluator_component_sha256": "bb" * 32,
        "evaluator_artifact_sha256": "cc" * 32,
        "protocol_sha256": hashlib.sha256(protocol.protocol_bytes()).hexdigest(),
        "confirmation_sha256": protocol.confirmation_sha256,
        "candidate_manifest": "11" * 32,
        "evaluator_manifest": "22" * 32,
        "output_sha256": "33" * 32,
        "negative_control": "rejected",
        "metrics": {"score": 0.9, "loss": 0.1},
        "verdict": "qualified",
    }
    payload.update(overrides)
    return TrialReceipt.model_validate(payload)


def _policy(limit: str) -> StandingPolicy:
    return StandingPolicy(
        policy_id=uuid4(),
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal(limit),
    )


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_record_past_max_usd_writes_crossing_event_and_refuses(native_jobs) -> None:
    """Recording past max_usd still writes the usage event; the gate then refuses."""
    store = NativeJobStore(native_jobs.service.config)
    limits = ResourceLimits(
        max_wall_clock_seconds=3600.0,
        max_model_tokens=1_000_000,
        max_usd=Decimal("0.50"),
    )
    accounting = ResourceAccounting(
        limits,
        store=store,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
    )
    result = accounting.record(
        operation_id=f"op-r18-{uuid4()}",
        work_id=UUID(native_jobs.item["work_id"]),
        job_id=None,
        request_id=f"req-r18-{uuid4()}",
        wall_clock_seconds=12.5,
        tokens=1500,
        usd=Decimal("0.75"),
    )
    assert result is not None
    usage_id = result["usage_id"]

    assert accounting.refuse_reason(datetime.now(timezone.utc)) == "quota_exceeded"
    totals = accounting.total_usage()
    assert totals["usd"] == "0.75"
    assert totals["quota_exceeded"] is True

    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        row = conn.execute(
            """
            SELECT input_tokens, price_usd, role_derived, model, price_version
            FROM omp_jobs.usage_events WHERE usage_id=%s
            """,
            (usage_id,),
        ).fetchone()
    assert row["input_tokens"] == 1500
    assert row["price_usd"] == Decimal("0.75")
    assert row["role_derived"] == "evaluator"
    assert row["model"] == "research"
    assert row["price_version"] == "research-m4"


def test_accounting_accumulates_and_crosses_quota() -> None:
    """Totals accumulate per axis; the gate answers only once a limit is exceeded."""
    limits = ResourceLimits(
        max_wall_clock_seconds=10.0, max_model_tokens=100, max_usd=Decimal("0.50")
    )
    accounting = ResourceAccounting(limits)
    assert accounting.refuse_reason(datetime.now(timezone.utc)) is None

    assert (
        accounting.record(
            operation_id="op-1",
            work_id=uuid4(),
            job_id=None,
            request_id="req-1",
            wall_clock_seconds=4.0,
            tokens=40,
            usd=Decimal("0.20"),
        )
        is None
    )
    assert accounting.quota_exceeded is False

    accounting.record(
        operation_id="op-2",
        work_id=uuid4(),
        job_id=None,
        request_id="req-2",
        wall_clock_seconds=3.0,
        tokens=30,
        usd=Decimal("0.40"),
    )
    assert accounting.refuse_reason(None) == "quota_exceeded"
    assert accounting.total_usage() == {
        "compute_seconds": 7.0,
        "tokens": 70,
        "usd": "0.60",
        "quota_exceeded": True,
    }


def test_unequal_cpu_is_invalid_comparison() -> None:
    """A resource mismatch invalidates the comparison before any metric is read."""
    baseline = _receipt(score=0.5, trial_id="trial-base")
    candidate = _receipt(score=1.0, trial_id="trial-cand")
    candidate_resources = dict(RESOURCES, cpu=2)

    assert matched_resources(RESOURCES, dict(RESOURCES)) == "matched"
    assert matched_resources(RESOURCES, candidate_resources) == "invalid_comparison"
    assert evaluate_comparison(
        _prereg(2), baseline, candidate, RESOURCES, candidate_resources
    ) == {
        "status": "invalid_comparison",
        "independent_n": 2,
        "successes": 0,
        "failures": 0,
        "denominator": 0,
    }


def test_unsound_receipt_or_wrong_protocol_is_invalid_comparison() -> None:
    """A stale receipt or a non-preregistered protocol digest invalidates the pair."""
    prereg = _prereg(2)
    baseline = _receipt(score=0.5, trial_id="trial-base")
    candidate = _receipt(score=1.0, trial_id="trial-cand")

    stale = _receipt(score=1.0, trial_id="trial-stale", schema_version=2)
    outcome = evaluate_comparison(prereg, baseline, stale, RESOURCES, RESOURCES)
    assert outcome["status"] == "invalid_comparison"
    assert outcome["denominator"] == 0

    other_protocol = _receipt(
        score=1.0, trial_id="trial-other", protocol_sha=OTHER_PROTOCOL_SHA
    )
    outcome = evaluate_comparison(
        prereg, baseline, other_protocol, RESOURCES, RESOURCES
    )
    assert outcome["status"] == "invalid_comparison"


def test_ten_x_claim_with_one_independent_run_is_unsupported() -> None:
    """A 10x-or-better speedup on a single independent run is unsupported_claim."""
    prereg = _prereg(1)
    baseline = _receipt(score=0.1, trial_id="trial-base")
    candidate = _receipt(score=1.5, trial_id="trial-cand")
    with pytest.raises(ValueError, match="unsupported_claim"):
        evaluate_comparison(prereg, baseline, candidate, RESOURCES, RESOURCES)


def test_ten_x_claim_with_two_independent_runs_compares() -> None:
    """The same 15x speedup compares when two independent runs back it."""
    prereg = _prereg(2)
    baseline = _receipt(score=0.1, trial_id="trial-base")
    candidate = _receipt(score=1.5, trial_id="trial-cand")
    assert evaluate_comparison(prereg, baseline, candidate, RESOURCES, RESOURCES) == {
        "status": "compared",
        "speedup": 15.0,
        "independent_n": 2,
        "successes": 2,
        "failures": 0,
        "denominator": 2,
    }


def test_lower_direction_and_failed_verdict_denominators() -> None:
    """For a lower-is-better metric the speedup inverts; failures keep the denominator."""
    prereg = _prereg(2, direction="lower")
    baseline = _receipt(score=2.0, trial_id="trial-base")
    candidate = _receipt(score=0.5, trial_id="trial-cand")
    outcome = evaluate_comparison(prereg, baseline, candidate, RESOURCES, RESOURCES)
    assert outcome["status"] == "compared"
    assert outcome["speedup"] == 4.0

    failed = _receipt(
        score=0.5, trial_id="trial-cand", verdict="candidate_failed"
    )
    outcome = evaluate_comparison(prereg, baseline, failed, RESOURCES, RESOURCES)
    assert outcome["successes"] == 1
    assert outcome["failures"] == 1
    assert outcome["denominator"] == 2


def test_accepted_negative_control_is_negative_acceptance() -> None:
    """A qualified receipt with an accepted negative control is negative_acceptance."""
    protocol = _protocol()
    confirmed = protected_confirmation(_bound_receipt(protocol), protocol)
    assert confirmed == {"status": "confirmed", "reasons": []}

    negative = protected_confirmation(
        _bound_receipt(protocol, negative_control="accepted"), protocol
    )
    assert negative["status"] == "negative_acceptance"
    assert negative["reasons"] == ["negative_control_accepted"]

    rejected = protected_confirmation(
        _bound_receipt(protocol, confirmation_sha256="cd" * 32), protocol
    )
    assert rejected["status"] == "rejected"
    assert rejected["reasons"] == ["wrong_confirmation"]


def test_fault_tolerance_publishes_kinds_and_denominator() -> None:
    """Fault kinds are published per failure; a lone 10x claim is unsupported."""
    with pytest.raises(ValueError, match="unknown_fault"):
        inject_fault("power_loss")

    results = [inject_fault("crash"), inject_fault("timeout"), {"ok": True}]
    audit = audit_fault_tolerance(results)
    assert audit == {
        "successes": 1,
        "failures": 2,
        "denominator": 3,
        "failure_kinds": ["crash", "timeout"],
    }
    assert "crash" in audit["failure_kinds"]

    with pytest.raises(ValueError, match="unsupported_claim"):
        audit_fault_tolerance([inject_fault("crash")], claimed_speedup=10.0)
    two_runs = audit_fault_tolerance(results[:2], claimed_speedup=10.0)
    assert two_runs["denominator"] == 2


def test_intervention_must_narrow() -> None:
    """A wider money limit is refused; a narrower policy is authorized and returned."""
    old = _policy("100")
    authority = ChangeAuthority(requested_by_kind="owner")

    narrowed = intervene_policy(old, _policy("50"), authority)
    assert narrowed.money_limit_usd == Decimal("50")

    with pytest.raises(PolicyRefused, match="intervention_must_narrow") as err:
        intervene_policy(old, _policy("200"), authority)
    assert err.value.code == "intervention_must_narrow"


def test_canary_identity_and_drift() -> None:
    """The canary refuses an untested release identity and detects drift."""
    assert run_canary(1.0, 1.2, 0.05, "aa" * 32, "aa" * 32) == {
        "status": "drift_detected"
    }
    assert run_canary(1.0, 1.01, 0.05, "aa" * 32, "aa" * 32) == {"status": "ok"}

    with pytest.raises(ValueError, match="release_identity_mismatch"):
        run_canary(1.0, 1.0, 0.05, "aa" * 32, "bb" * 32)
    with pytest.raises(ValueError, match="tolerance"):
        run_canary(1.0, 1.0, 1.5, "aa" * 32, "aa" * 32)
