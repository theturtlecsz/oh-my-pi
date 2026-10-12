"""Compare recursive methods through native settlement, custody, and holdout gates."""

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
import recursive_transfer_support
from psycopg.types.json import Jsonb

from native_jobs_support import native_jobs as native_jobs_fixture
from omp_work.research import mechanism, recursive_transfer_evidence as evidence
from omp_work.research.custody import artifact_path
from omp_work.research.harness_release import package_candidate
from omp_work.research.receipt import EvaluationProtocol, MetricSpec, TrialReceipt
from omp_work.research.recursive import RecursionState, RecursiveStudy
from omp_work.research.recursive_method import SOURCE_PATH, MethodResult, run_method
from omp_work.research.recursive_transfer import compare_methods
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_freshness import CohortTask, StepArms
from omp_work.v1.canonical import sha256
from recursive_transfer_support import (
    cache_artifact, register_artifact_bytes, register_task_dataset, seal_managed_trial,
)
from test_research_managed_gate import _default_protocol, _evaluator_descriptor, _store
from test_research_recursive_method import inputs, tree
from test_workflow_service import _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def native_jobs(service):
    # Separate workspaces prevent leased jobs and altered artifacts leaking cases.
    return native_jobs_fixture.__wrapped__(service)


@dataclass
class Comparison:
    fix: SimpleNamespace
    native: NativeEvidence
    study: RecursiveStudy
    baseline: MethodResult
    candidate: MethodResult
    protocol: EvaluationProtocol
    verified: list[str]

    def compare(self, *cohorts):
        return compare_methods(self.study, self.baseline, self.candidate, cohorts, self.native)

    def cohort(self, *, phase="confirmation", domain="outside", label=None,
               payload=None, scores=(0.2, 0.9), arm_options=None, metric="score"):
        payload = payload if payload is not None else {
            "steps": tree(), "opened": ["a"], "sample": uuid4().hex,
        }
        # Tree scores deliberately contradict both selection and measured delta.
        payload["steps"]["a"]["metric"] = -100
        payload["steps"]["b"]["metric"] = 100
        payload["steps"]["a1"]["metric"] = -200
        dataset = register_task_dataset(self.fix, label or uuid4().hex, payload)
        assert self.baseline.source_bytes != self.candidate.source_bytes
        pairs = {}
        for step in payload["steps"]:
            pair = []
            for arm_name, result, score in (
                ("baseline", self.baseline, scores[0]),
                ("candidate", self.candidate, scores[1]),
            ):
                options = dict((arm_options or {}).get((step, arm_name), {}))
                source = options.pop("source_bytes", result.source_bytes)
                measured = 0.6 if step == "a" else score
                metrics = {spec.name: measured for spec in self.protocol.metrics}
                sealed = seal_managed_trial(
                    self.fix, source_bytes=source, metric=metrics,
                    protocol=self.protocol, dataset_id=dataset, **options,
                )
                pair.append(sealed)
            pairs[step] = tuple(pair)
        cohort = CohortTask(
            phase=phase, domain=domain, metric=metric, direction="higher",
            protocol_sha256=self.study.protocol_sha256, seed=0, width=1, budget=2,
            dataset_id=dataset,
            steps=tuple(StepArms(step, pair[0].trial_id, pair[1].trial_id)
                        for step, pair in pairs.items()),
        )
        return cohort, pairs


@pytest.fixture
def comparison(native_jobs, monkeypatch):
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=1), now + timedelta(hours=1)
    protocol = _default_protocol().model_copy(update={"metrics": (
        MetricSpec(name="score", direction="higher"), MetricSpec(name="accuracy", direction="higher"),
    )})
    evaluator, evaluator_sha = _evaluator_descriptor()
    # Reuse one registered evaluator across the two methods and all trial arms.
    monkeypatch.setattr(recursive_transfer_support, "_evaluator_descriptor",
                        lambda: (evaluator, evaluator_sha))
    methods = []
    for threshold in (0.8, 0.5):
        study, recipe, spec, dev, context, _ = inputs(threshold)
        grant = context.grant.model_copy(update={"window_start": start, "window_end": end})
        study = study.model_copy(update={
            "window_start": start, "window_end": end,
            "evaluator_component_sha256": evaluator_sha,
            "evaluator_artifact_sha256": evaluator["artifact_sha256"],
            "protocol_sha256": hashlib.sha256(protocol.protocol_bytes()).hexdigest(),
        })
        context = replace(context, state=RecursionState(grant=grant), grant=grant,
                          policy=replace(context.policy, expires_at=end), now=lambda: now,
                          operation_id=str(uuid4()), request_id=str(uuid4()))
        result = run_method(study, recipe, spec, dev, context=context)
        assert result.failures == ()
        methods.append(result)
    native = NativeEvidence(
        _store(native_jobs), native_jobs.workspace_id, native_jobs.actor_id,
        native_jobs.item["work_id"], native_jobs.service.config.data_dir,
        grant=context.grant, policy=context.policy, authority=context.authority,
        action=context.action, repositories=context.repositories,
        operation_id=context.operation_id, request_id=context.request_id,
    )

    def receipt_with_output(**fields):
        # Fixture helper supplies a constant output digest. Register independent
        # output bytes before the real helper settles and records its receipt.
        fields["output_sha256"] = register_artifact_bytes(
            native_jobs, json.dumps({"trial": fields["trial_id"], "metrics": fields["metrics"]}).encode(),
            "output.json",
        )
        return TrialReceipt(**fields)

    monkeypatch.setattr(recursive_transfer_support, "TrialReceipt", receipt_with_output)
    calls = []
    real_verify = evidence.verify_managed_trial

    def verify(*args, **kwargs):
        calls.append(str(args[2]))
        return real_verify(*args, **kwargs)

    # Observe the real gate; never replace its decision or custody reads.
    monkeypatch.setattr(evidence, "verify_managed_trial", verify)
    return Comparison(native_jobs, native, study, *methods, protocol, calls)


def execution(report, index=0):
    return report.domains[index].tasks[0].executions[0]


def assert_refused(report, reason):
    assert report.status == "inconclusive"
    assert report.deltas == report.transfer == ()
    assert report.keep_both
    assert reason in report.failures
    assert all(row.status != "compared" for group in report.domains
               for task in group.tasks for row in task.executions)


def test_sealed_development_and_outside_confirmation_keep_native_provenance(comparison):
    development, _ = comparison.cohort(phase="development", domain="source")
    confirmation, pairs = comparison.cohort()
    report = comparison.compare(development, confirmation)
    assert report.status == "compared", report.failures
    assert report.denominator == 12
    assert report.deltas == pytest.approx((0.7, 0.7))
    assert report.transfer == (report.domains[1],)
    target = report.transfer[0]
    assert (target.phase, target.domain, target.metric, target.denominator) == (
        "confirmation", "outside", "score", 6,
    )
    row = execution(report, 1)
    assert row.selections == {"baseline": "b", "candidate": "a1"}
    for result, sealed in ((row.baseline, pairs["b"][0]), (row.candidate, pairs["a1"][1])):
        assert (result.trial_id, result.job_id, result.campaign_id, result.receipt_id,
                result.tar_sha256, result.output_sha256) == (
            str(sealed.trial_id), sealed.job_id, str(sealed.campaign_id), sealed.receipt_sha256,
            sealed.candidate_digest, sealed.receipt.output_sha256,
        )
        assert result.trial_id in result.verified_trial_ids
        assert result.selected_step.evidence.metrics["score"] == sealed.metric
        frozen = comparison.baseline if result is row.baseline else comparison.candidate
        assert result.source_sha256 == frozen.source_sha256
        assert result.bundle_sha256 == frozen.artifact_sha256
    assert {str(arm.trial_id) for pair in pairs.values() for arm in pair} <= set(comparison.verified)


def test_same_domain_content_disjoint_confirmation_stays_without_transfer(comparison):
    development, _ = comparison.cohort(phase="development", domain="source")
    confirmation, _ = comparison.cohort(domain="source")
    report = comparison.compare(development, confirmation)
    assert report.status == "compared", report.failures
    assert [(row.phase, row.domain, row.deltas) for row in report.domains] == [
        ("development", "source", pytest.approx((0.7,))),
        ("confirmation", "source", pytest.approx((0.7,))),
    ]
    assert report.transfer == ()
    assert execution(report).content_sha256 != execution(report, 1).content_sha256


@pytest.mark.parametrize("case,reason", [
    ("unsealed", "missing_managed_receipt"),
    ("candidate_authored", "candidate_authored"),
    ("negative_control", "negative_control_accepted"),
])
def test_native_refusals_reach_real_managed_verifier(comparison, case, reason):
    options = {"unsealed": {"settle": False}, "candidate_authored": {},
               "negative_control": {"negative_control": "accepted"}}[case]
    cohort, pairs = comparison.cohort(arm_options={("b", "baseline"): options})
    if case == "candidate_authored":
        arm = pairs["b"][0]
        payload = {"receipt_sha256": arm.receipt_sha256, "verdict": arm.receipt.verdict,
                   "metrics": arm.receipt.metrics}
        status, body = _command(comparison.fix.service, comparison.fix.workspace_id, {
            "type": "record_research_observation", "payload": {
                "observation_id": str(uuid4()), "campaign_id": str(arm.campaign_id),
                "trial_id": str(arm.trial_id), "issuer_kind": "candidate_authored",
                "source_ref": f"candidate:{arm.trial_id}", "execution_status": "completed",
                "payload": payload, "payload_sha256": sha256(payload),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        assert status == 200, body
    report = comparison.compare(cohort)
    assert_refused(report, reason)
    assert str(pairs["b"][0].trial_id) in comparison.verified
    if case == "unsealed":
        raw = artifact_path(comparison.native.data_dir, comparison.native.workspace_id,
                            pairs["b"][0].candidate_digest).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == pairs["b"][0].candidate_digest
        assert pairs["b"][0].candidate_digest == pairs["a"][0].candidate_digest


def test_legacy_campaign_cannot_supply_managed_transfer(comparison):
    cohort, _ = comparison.cohort()
    cohort = cohort.model_copy(update={"steps": (
        StepArms("b", comparison.fix.trial_id, cohort.steps[0].candidate_trial_id),
    )})
    assert_refused(comparison.compare(cohort), "missing_managed_trial")


@pytest.mark.parametrize("field,reason", [
    ("evaluator_component_sha256", "evaluator_component_changed"),
    ("evaluator_artifact_sha256", "evaluator_artifact_changed"),
    ("protocol_sha256", "protocol_changed"),
    ("method_sha256", "method_changed"),
    ("lower_researcher_sha256", "lower_researcher_changed"),
])
def test_identity_swap_refuses_pair_before_consumption(comparison, field, reason):
    cohort, _ = comparison.cohort()
    comparison.study = comparison.study.model_copy(update={field: "0" * 64})
    before = comparison.native.snapshot()
    with pytest.raises(ValueError, match=reason):
        comparison.compare(cohort)
    assert comparison.native.snapshot() == before
    assert comparison.verified == []


@pytest.mark.parametrize("case,reason", [
    ("source", "changed_source"), ("artifact", "changed_digest"), ("failed", "generation_crashed"),
])
def test_forged_baseline_and_failed_generation_never_execute(comparison, monkeypatch, case, reason):
    if case == "source":
        source = b"def propose(steps, opened, *, seed=0, width=1):\n    return 'b', seed\n"
        comparison.baseline = replace(
            comparison.baseline, source_bytes=source, source_sha256=hashlib.sha256(source).hexdigest(),
            artifact_sha256=package_candidate({SOURCE_PATH: source})["bundle_sha256"],
        )
    elif case == "artifact":
        comparison.baseline = replace(comparison.baseline, artifact_sha256="0" * 64)
    else:
        _, recipe, spec, dev, context, _ = inputs(0.5)
        context = replace(context, state=RecursionState(grant=comparison.native.grant),
                          grant=comparison.native.grant, policy=comparison.native.policy,
                          now=lambda: datetime.now(timezone.utc))

        def crash(*args):
            raise RuntimeError("generator crashed")

        with monkeypatch.context() as patch:
            patch.setattr(mechanism, "generation_job", crash)
            comparison.candidate = run_method(comparison.study, recipe, spec, dev, context=context)
        assert comparison.candidate.denominator == 1
        assert comparison.candidate.failures == ("generation_crashed",)
    with pytest.raises(ValueError, match=reason):
        comparison.compare()
    assert comparison.native.snapshot()["attempts"] == []


def test_opened_non_selected_tar_swap_refuses_before_selection(comparison):
    cohort, pairs = comparison.cohort(arm_options={
        ("a", "candidate"): {"source_bytes": comparison.baseline.source_bytes},
    })
    report = comparison.compare(cohort)
    assert_refused(report, "changed_source")
    assert report.denominator == 6
    row = execution(report)
    assert row.candidate.selection is None and row.candidate.invocation_count == 0
    assert str(pairs["a"][1].trial_id) in comparison.verified


@pytest.mark.parametrize("axis", ["cpu", "memory_mib", "gpu", "model_calls"])
def test_unequal_native_resource_axis_blocks_transfer(comparison, axis):
    cohort, _ = comparison.cohort(arm_options={
        ("a1", "candidate"): {"resources": {axis: 2}},
    })
    report = comparison.compare(cohort)
    assert_refused(report, "resource_mismatch")
    assert report.denominator == 6
    assert execution(report).selections == {"baseline": "b", "candidate": "a1"}


@pytest.mark.parametrize("overlap", ["payload", "trace"])
def test_confirmation_before_overlapping_development_is_refused(comparison, overlap):
    payload = {"steps": tree(), "opened": ["a"], "traces": {"shared": [{"metric": 0.3}]}}
    confirmation, _ = comparison.cohort(payload=payload)
    dev_payload = json.loads(json.dumps(payload))
    if overlap == "trace":
        dev_payload["different"] = True
    development, _ = comparison.cohort(phase="development", payload=dev_payload)
    report = comparison.compare(confirmation, development)
    assert "confirmation_reused" in report.domains[0].failures
    assert report.domains[0].deltas == ()
    assert report.domains[1].status == "compared"
    assert report.transfer == ()


def test_new_dataset_id_with_same_payload_is_not_fresh(comparison):
    payload = {"steps": tree(), "opened": ["a"]}
    first, _ = comparison.cohort(payload=payload)
    assert comparison.compare(first).status == "compared"
    second, _ = comparison.cohort(payload=payload)
    assert first.dataset_id != second.dataset_id
    assert_refused(comparison.compare(second), "confirmation_reused")


@pytest.mark.parametrize("target", ["dataset", "candidate"])
@pytest.mark.parametrize("mismatch", ["manifest", "bytes"])
def test_manifest_and_bytes_mismatch_cannot_transfer(comparison, target, mismatch):
    cohort, pairs = comparison.cohort()
    digest = cohort.dataset_id if target == "dataset" else pairs["a"][1].candidate_digest
    native = comparison.native
    if mismatch == "manifest":
        with psycopg.connect(
            **comparison.fix.service.config.connection_kwargs("postgres")
        ) as conn, conn.cursor() as cur:
            # Deliberate stored-manifest corruption, confined to this workspace.
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("UPDATE omp_research.artifacts SET manifest=jsonb_set(manifest, "
                        "'{size_bytes}', %s) WHERE workspace_id=%s AND artifact_sha256=%s",
                        (Jsonb(1), native.workspace_id, digest))
        reason = "artifact_unavailable"
    else:
        path = artifact_path(native.data_dir, native.workspace_id, digest)
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        reason = "artifact"
    report = comparison.compare(cohort)
    assert report.status == "inconclusive" and report.transfer == ()
    assert any(reason in failure.lower() for failure in report.failures)
    assert report.denominator == (0 if target == "dataset" else 6)


@pytest.mark.parametrize("restore", [False, True])
def test_second_compare_and_restored_snapshot_cannot_recount(comparison, restore):
    cohort, _ = comparison.cohort()
    first = comparison.compare(cohort)
    assert first.status == "compared" and first.denominator == 6
    snapshot = comparison.native.snapshot()
    if restore:
        comparison.native = replace_native(comparison.native, snapshot)
    second = comparison.compare(cohort)
    assert_refused(second, "confirmation_reused")
    assert second.denominator == 0
    assert comparison.native.snapshot() == snapshot


def replace_native(native, snapshot):
    restored = NativeEvidence(
        native.store, native.workspace_id, native.actor_id, native.work_id, native.data_dir,
        grant=native.grant, policy=native.policy, authority=native.authority, action=native.action,
        repositories=native.repositories, operation_id=native.operation_id, request_id=native.request_id,
    )
    restored.restore(json.loads(json.dumps(snapshot)))
    return restored


def test_cached_artifact_refuses_new_replicate(comparison):
    cohort, pairs = comparison.cohort()
    cache_artifact(comparison.fix, pairs["a1"][1].receipt_sha256, uuid4().hex)
    report = comparison.compare(cohort)
    assert_refused(report, "cached result cannot masquerade as new independent replicate")
    assert str(pairs["a1"][1].trial_id) in comparison.native.snapshot()["failures"]
    assert report.denominator == 5


def test_failed_attempt_stays_in_denominator(comparison):
    cohort, pairs = comparison.cohort(arm_options={
        ("a", "candidate"): {"source_bytes": comparison.baseline.source_bytes},
    })
    report = comparison.compare(cohort)
    assert_refused(report, "changed_source")
    assert report.denominator == 6
    failed = str(pairs["a"][1].trial_id)
    assert failed in comparison.native.snapshot()["failures"]
    assert failed not in comparison.native.snapshot()["successes"]
    assert comparison.compare(cohort).denominator == 0


def test_empty_cohorts_are_ineligible(comparison):
    report = comparison.compare()
    assert report.status == "ineligible"
    assert report.denominator == 0
    assert report.domains == report.transfer == report.deltas == ()
    assert comparison.verified == []


@pytest.mark.parametrize("phase", ["development", "confirmation"])
def test_no_qualified_metric_is_inconclusive_in_both_phases(comparison, phase):
    cohort, pairs = comparison.cohort(phase=phase, arm_options={
        ("a1", "candidate"): {"outcome": "failed", "trust": "untrusted"},
    })
    report = comparison.compare(cohort)
    assert_refused(report, "candidate_failed")
    assert report.domains[0].phase == phase
    assert str(pairs["a1"][1].trial_id) in comparison.verified


def test_repeated_task_label_preserves_distinct_content(comparison):
    first, _ = comparison.cohort(phase="development", label="repeated")
    second, _ = comparison.cohort(phase="development", label="repeated", scores=(0.9, 0.2))
    report = comparison.compare(first, second)
    assert len(report.domains) == 1
    assert [(task.task_id, task.deltas) for task in report.domains[0].tasks] == [
        ("repeated", pytest.approx((0.7,))), ("repeated", pytest.approx((-0.7,))),
    ]
    assert report.denominator == 12
    assert report.keep_both


def test_two_metrics_remain_separate_and_nonprimary_cannot_transfer(comparison):
    primary, _ = comparison.cohort()
    secondary, _ = comparison.cohort(metric="accuracy")
    report = comparison.compare(primary, secondary)
    assert [(group.metric, group.status) for group in report.domains] == [
        ("score", "compared"), ("accuracy", "inconclusive"),
    ]
    assert report.transfer == (report.domains[0],)
    assert report.domains[1].deltas == ()
    assert "metric_mismatch" in report.domains[1].failures


def test_negative_confirmation_delta_survives_positive_development(comparison):
    dev, _ = comparison.cohort(phase="development", domain="source")
    confirmation, _ = comparison.cohort(scores=(0.9, 0.2))
    report = comparison.compare(dev, confirmation)
    assert report.status == "compared", report.failures
    assert report.deltas == pytest.approx((0.7, -0.7))
    assert report.domains[1].keep_both
    assert report.transfer == ()
