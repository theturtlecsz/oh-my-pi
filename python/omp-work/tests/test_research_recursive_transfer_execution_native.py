"""Native execution arms remain independently consumable and settlement-bound."""

import hashlib
import io
import os
import tarfile
from dataclasses import replace

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.research.custody import artifact_path
from omp_work.research.recursive_method import GeneratedPolicy, validate_frozen_method
from omp_work.research.recursive_transfer_evidence import read_trial
from omp_work.research.recursive_transfer_freshness import prepare_cohorts
from omp_work.research.recursive_transfer_history import reserve_task
from recursive_transfer_execution_support import build_execution, seal_step

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def execution(native_jobs):
    return build_execution(native_jobs)


def test_independent_opened_and_selectable_arms_admit_without_duplicate_output(execution):
    for step in execution.steps:
        seal_step(execution, step)
    all_arms = [arm for pair in execution.arms.values() for arm in pair]
    for field in ("trial_id", "job_id", "receipt_sha256"):
        assert len({getattr(arm, field) for arm in all_arms}) == 6
    evidence = [read_trial(execution.native, arm.trial_id) for arm in all_arms]
    assert all(arm.qualified and not arm.reasons for arm in evidence)
    assert len({arm.output_sha256 for arm in evidence}) == 6
    assert {arm.work_id for arm in evidence} == {str(execution.native.work_id)}
    for arm in evidence:
        output = artifact_path(execution.native.data_dir, execution.native.workspace_id,
                               arm.output_sha256).read_bytes()
        assert hashlib.sha256(output).hexdigest() == arm.output_sha256
    prepared, = prepare_cohorts((execution.cohort(),), execution.native,
                                (execution.baseline.study, execution.candidate.study))
    assert prepared.eligible, prepared.reasons
    reservation = reserve_task(execution.native, prepared)
    assert reservation.replicates == 6
    assert reservation.denominator == 0
    assert reservation.reasons == ()
    replay = reserve_task(execution.native, prepared)
    assert replay.replicates == 0
    assert "duplicate_output" in replay.reasons


def test_live_frozen_methods_select_using_registered_opened_receipts(execution):
    seal_step(execution, "a", baseline_score=0.6, candidate_score=0.6)
    for name, frozen, context, expected in (
        ("baseline", execution.baseline, execution.baseline_context, "b"),
        ("candidate", execution.candidate, execution.candidate_context, "a1"),
    ):
        source = validate_frozen_method(frozen.result, frozen.study)
        assert hashlib.sha256(source.encode()).hexdigest() == frozen.result.source_sha256
        observation = execution.observations(name)["a"]
        assert observation.expected.evaluator_component_sha256 == frozen.study.evaluator_component_sha256
        policy = GeneratedPolicy(frozen.result, frozen.study, context=context,
                                 protocol=execution.protocol, observations=execution.observations(name))
        assert policy(execution.steps, execution.opened) == (expected, 0)
        assert policy.failures == ()
    with execution.native.store.transaction(execution.native.workspace_id, execution.native.actor_id) as cur:
        cur.execute("SELECT request_id FROM omp_jobs.usage_events WHERE workspace_id=%s AND work_id=%s",
                    (execution.native.workspace_id, execution.native.work_id))
        requests = {row["request_id"] for row in cur.fetchall()}
    assert {f"{ctx.request_id}:{index}" for ctx in (execution.baseline_context, execution.candidate_context)
            for index in (1, 2)} <= requests


def test_failed_settlement_stays_unqualified_and_counts_failure(native_jobs):
    execution = build_execution(native_jobs, direction="lower")
    baseline, candidate = seal_step(execution, "b", baseline_score=4.0, candidate_score=2.0,
                                    candidate_outcome="failed",
                                    baseline_resources={"cpu": 2, "memory_mib": 16},
                                    candidate_resources={"cpu": 3, "gpu": 1, "model_calls": 2})
    success = read_trial(execution.native, baseline.trial_id)
    failure = read_trial(execution.native, candidate.trial_id)
    assert success.qualified and not failure.qualified
    assert failure.receipt.verdict == "candidate_failed"
    assert failure.protocol.metrics[0].direction == "lower"
    assert (success.metric, failure.metric) == (4.0, 2.0)
    for arm, evidence in ((baseline, success), (candidate, failure)):
        with execution.native.store.transaction(execution.native.workspace_id, execution.native.actor_id) as cur:
            cur.execute("SELECT resources, settlement FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                        (execution.native.workspace_id, arm.job_id))
            row = cur.fetchone()
        assert row["resources"] == evidence.resources == arm.resources
        assert row["settlement"]["outcome"] == ("succeeded" if evidence.qualified else "failed")
    prepared, = prepare_cohorts((execution.cohort(),), execution.native, ())
    reservation = reserve_task(execution.native, prepared)
    assert reservation.replicates == 2
    assert reservation.denominator == 1
    assert "duplicate_output" not in reservation.reasons
    fresh = build_execution(native_jobs)
    assert not fresh.native.history.consumed
    assert candidate.trial_id in execution.native.history.consumed


def test_source_override_is_sealed_in_custody_without_changing_frozen_method(execution):
    source = b"def propose(steps, opened, *, seed=0, width=1):\n    return 'b', seed\n"
    baseline, candidate = seal_step(execution, "a1", candidate_source=source)
    evidence = read_trial(execution.native, candidate.trial_id)
    raw = artifact_path(execution.native.data_dir, execution.native.workspace_id, evidence.candidate_digest).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        assert archive.extractfile("research/strategies/recursive.py").read() == source
    assert candidate.candidate_digest != baseline.candidate_digest
    with pytest.raises(ValueError, match="changed_source"):
        validate_frozen_method(replace(execution.candidate.result, source_bytes=source), execution.candidate.study)
    validate_frozen_method(execution.candidate.result, execution.candidate.study)
