"""Unpooled reports preserve losses, cohort identities, and caller accounting."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omp_work.research import recursive_transfer as transfer
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_execution import ArmExecution, TaskExecution
from omp_work.research.recursive_transfer_execution_inputs import PreparedExecution
from omp_work.research.recursive_transfer_freshness import CohortTask, PreparedTask
from test_research_recursive_method import inputs


@pytest.fixture
def execution():
    _, _, _, _, context, _ = inputs()
    arm = ArmExecution("pick", 1, (), None, (), context.state)
    return TaskExecution(
        "development", "source", "a" * 64, "score", "higher", "task", "b" * 64,
        "compared", 1.0, {"baseline": "pick", "candidate": "pick"}, 2, (), arm, arm, True,
    )


def test_same_domain_phases_are_retained_but_not_transfer(execution):
    confirmation = replace(execution, phase="confirmation", content_sha256="c" * 64, task_id="fresh")
    report = transfer.assemble_results([execution, confirmation])
    assert [(group.domain, group.phase) for group in report.domains] == [
        ("source", "development"), ("source", "confirmation"),
    ]
    assert report.domains[1].deltas == (1.0,)
    assert report.transfer == ()


def test_distinct_domain_positive_confirmation_transfers(execution):
    target = replace(execution, phase="confirmation", domain="target")
    second = replace(target, task_id="second", content_sha256="c" * 64, delta=2.0)
    report = transfer.assemble_results([execution, target, second])
    assert [(group.domain, group.metric, group.protocol_sha256, group.deltas) for group in report.transfer] == [
        ("target", "score", "a" * 64, (1.0, 2.0)),
    ]


def test_two_contents_with_one_task_id_survive(execution):
    report = transfer.assemble_results([execution, replace(execution, content_sha256="c" * 64, delta=-1.0)])
    assert [(task.content_sha256, task.task_id, task.deltas) for task in report.domains[0].tasks] == [
        ("b" * 64, "task", (1.0,)), ("c" * 64, "task", (-1.0,)),
    ]


@pytest.mark.parametrize("change", [
    {"metric": "accuracy"}, {"direction": "lower"}, {"protocol_sha256": "c" * 64},
])
def test_measurement_boundaries_never_pool(execution, change):
    other = replace(execution, **change, delta=-3.0)
    report = transfer.assemble_results([execution, other])
    assert len(report.domains) == 2
    assert [group.deltas for group in report.domains] == [(1.0,), (-3.0,)]
    assert report.domains[1].tasks[0].executions == (other,)


def test_source_win_cannot_hide_target_loss(execution):
    target = replace(execution, phase="confirmation", domain="target", delta=-1.0)
    report = transfer.assemble_results([replace(execution, delta=100.0), target])
    assert report.domains[1].deltas == (-1.0,)
    assert report.domains[1].keep_both
    assert report.transfer == ()


def test_positive_target_cannot_hide_loss_in_same_group(execution):
    target = replace(execution, phase="confirmation", domain="target", delta=100.0)
    loss = replace(target, content_sha256="c" * 64, delta=-1.0)
    report = transfer.assemble_results([execution, target, loss])
    assert report.domains[1].deltas == (100.0, -1.0)
    assert report.transfer == ()


def test_development_domain_exclusion_crosses_measurement_groups(execution):
    target = replace(execution, phase="confirmation", metric="accuracy", protocol_sha256="c" * 64)
    report = transfer.assemble_results([execution, target])
    assert report.transfer == ()
    assert report.domains[1].status == "compared"


def test_empty_report_is_ineligible():
    report = transfer.assemble_results([])
    assert report.status == "ineligible"
    assert report.denominator == 0
    assert report.keep_both
    assert report.domains == report.transfer == ()


def test_unattempted_task_is_ineligible_at_every_level(execution):
    row = replace(execution, status="ineligible", delta=None, denominator=0, ok=False)
    report = transfer.assemble_results([row])
    for result in (report, report.domains[0], report.domains[0].tasks[0]):
        assert result.status == "ineligible"
        assert result.denominator == 0
        assert result.keep_both


def test_assembly_preserves_input_and_repeated_task_outcomes(execution):
    failed = replace(execution, delta=None, ok=False, failures=("execution_failed",))
    rows = [execution, failed]
    report = transfer.assemble_results(rows)
    assert rows == [execution, failed]
    assert transfer.assemble_results(rows) == report
    task = report.domains[0].tasks[0]
    assert task.executions == (execution, failed)
    assert task.deltas == (1.0,)
    assert task.denominator == 4
    assert task.failures == ("execution_failed",)
    assert task.keep_both


@pytest.mark.parametrize("denominator,failures", [(2, ()), (0, ("verification_failed",))])
def test_missing_delta_is_inconclusive_at_every_level(execution, denominator, failures):
    row = replace(execution, status="ineligible", delta=None, denominator=denominator, failures=failures, ok=False)
    report = transfer.assemble_results([row])
    for result in (report, report.domains[0], report.domains[0].tasks[0]):
        assert result.status == "inconclusive"
        assert result.keep_both
        assert result.denominator == denominator
        assert result.failures == failures
        assert result.deltas == ()
    assert report.transfer == ()


@pytest.mark.parametrize("change", [
    {"ok": False}, {"failures": ("failed",)}, {"delta": float("nan")},
    {"delta": float("inf")}, {"delta": None},
])
def test_unqualified_delta_cannot_be_compared_or_transfer(execution, change):
    row = replace(execution, phase="confirmation", domain="target", **change)
    report = transfer.assemble_results([row])
    assert report.status == report.domains[0].status == report.domains[0].tasks[0].status == "inconclusive"
    assert report.transfer == ()
    assert report.domains[0].tasks[0].executions == (row,)


@pytest.mark.parametrize("delta", [0.0, None])
def test_target_requires_every_delta_positive(execution, delta):
    target = replace(execution, phase="confirmation", domain="target")
    other = replace(target, task_id="other", delta=delta, ok=delta is not None)
    assert transfer.assemble_results([execution, target, other]).transfer == ()


def test_compare_executes_only_eligible_tasks_and_returns_caller_history(monkeypatch, execution, tmp_path):
    study, _, _, _, context, _ = inputs()
    method = SimpleNamespace(state=context.state, source_sha256="d" * 64, artifact_sha256="e" * 64)
    frozen = SimpleNamespace(study=study, result=method)
    history = ConsumptionHistory()
    history.record_attempt("previous")
    native = NativeEvidence(None, uuid4(), uuid4(), uuid4(), tmp_path, history=history)
    cohort = CohortTask(
        phase="development", domain="source", metric="score", direction="higher",
        protocol_sha256=study.protocol_sha256, seed=7, width=3, budget=5,
        dataset_id="a" * 64, steps=(),
    )
    eligible = PreparedTask(True, (), cohort.dataset_id, "task", "b" * 64, ())
    refused = replace(eligible, eligible=False, reasons=("missing_steps",), task_id="refused")
    calls = []

    def freeze(supplied_study, baseline, candidate):
        assert (supplied_study, baseline, candidate) == (study, method, method)
        calls.append("freeze")
        return frozen, frozen

    def prepare(cohorts, supplied_native, studies):
        assert cohorts == (cohort, cohort)
        assert supplied_native is native
        assert studies == (study, study)
        calls.append("prepare")
        return eligible, refused

    def execute(baseline, candidate, prepared, supplied_native):
        assert baseline is candidate is frozen
        assert prepared == PreparedExecution.from_task(eligible, cohort)
        assert supplied_native is native
        calls.append("execute")
        supplied_native.history.record_success("new")
        return replace(execution, protocol_sha256=cohort.protocol_sha256)

    monkeypatch.setattr(transfer, "freeze_pair", freeze)
    monkeypatch.setattr(transfer, "prepare_cohorts", prepare)
    monkeypatch.setattr(transfer, "execute_task", execute)
    report = transfer.compare_methods(study, method, method, (cohort, cohort), native)
    assert calls == ["freeze", "prepare", "execute"]
    assert report.history is native.history is history
    assert history.consumed("previous") and history.consumed("new")
    skipped = report.domains[0].tasks[1].executions[0]
    assert skipped.status == "ineligible" and skipped.denominator == 0
    assert skipped.failures == ("missing_steps",)
    assert skipped.baseline.invocation_count == skipped.candidate.invocation_count == 0
