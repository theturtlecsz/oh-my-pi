"""Longitudinal benefit of s03 confirmation comparisons (OMP-319-s04)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from omp_work.research.recursive_benefit import (
    ArmLink,
    ConfirmationComparison,
    CostAxes,
    CostBounds,
    ObservationWindow,
    OwnershipCosts,
    QualityBound,
    summarize_benefit,
)
from omp_work.research.recursive_transfer import assemble_results
from omp_work.research.recursive_transfer_execution import ArmExecution, TaskExecution
from omp_work.v1.canonical import sha256

UTC = timezone.utc
PROTOCOL = "p" * 64
WHEN = datetime(2026, 6, 1, 12, tzinfo=UTC)


def _execution(domain, task, delta, *, ok=True, failures=(), content, protocol=PROTOCOL, phase="confirmation"):
    status = "compared" if ok and delta is not None else "inconclusive"
    baseline = ArmExecution("pick", 1, (), None, (), None, "method-base", "release-base")
    candidate = ArmExecution("pick", 1, (), None, (), None, "method-cand", "release-cand")
    return TaskExecution(
        phase, domain, protocol, "score", "higher", task, content, status, delta,
        {"baseline": "pick", "candidate": "pick"}, 2 if ok else 1, tuple(failures),
        baseline, candidate, ok,
    )


def _content(task):
    return f"content-{task}".ljust(64, "x")[:64]


def _ownership(maintenance=None, owner_minutes=Decimal("0")):
    zero = CostAxes(Decimal("0"), Decimal("0"), 0, Decimal("0"))
    if maintenance is None:
        maintenance = CostAxes(Decimal("0"), Decimal("0"), 0, owner_minutes)
    return OwnershipCosts(research=zero, evaluation=zero, maintenance=maintenance, interventions=zero)


def _window(when, *, minimum_delta=0.0, minimum_tasks=1):
    return ObservationWindow(
        when - timedelta(hours=1),
        when + timedelta(days=1),
        QualityBound(minimum_delta, minimum_tasks),
        CostBounds(Decimal("0"), Decimal("0"), 0, Decimal("0")),
    )


def _pair(
    *,
    domain="target",
    task="task-1",
    delta=3.0,
    ok=True,
    failures=(),
    when=WHEN,
    evaluator="eval-1",
    self_scored=False,
    suffix="a",
    base_usd=Decimal("10"),
    cand_usd=Decimal("4"),
    base_compute=Decimal("10"),
    cand_compute=Decimal("4"),
    base_tokens=100,
    cand_tokens=40,
    base_owner=Decimal("10"),
    cand_owner=Decimal("4"),
    content=None,
    protocol=PROTOCOL,
    phase="confirmation",
):
    content = _content(task) if content is None else content
    execution = _execution(
        domain, task, delta if ok else None, ok=ok, failures=failures, content=content,
        protocol=protocol, phase=phase,
    )
    base_id = f"usage-base-{suffix}"
    cand_id = f"usage-cand-{suffix}"
    base_receipt = f"receipt-base-{suffix}"
    cand_receipt = f"receipt-cand-{suffix}"
    comparison = ConfirmationComparison(
        domain=domain,
        task_id=task,
        evaluator_version=evaluator,
        timestamp=when,
        baseline=ArmLink("method-base", "release-base", base_receipt, (base_id,)),
        candidate=ArmLink("method-cand", "release-cand", cand_receipt, (cand_id,)),
        result=execution,
        self_scored=self_scored,
        content_sha256=content,
    )
    shared = {
        "domain": domain,
        "task_id": task,
        "content_sha256": content,
        "evaluator_version": evaluator,
        "recorded_at": when,
    }
    events = [
        {
            "usage_id": base_id,
            "price_usd": base_usd,
            "compute_seconds": base_compute,
            "tokens": base_tokens,
            "owner_minutes": base_owner,
            "arm": "baseline",
            "method_version": "method-base",
            "release_version": "release-base",
            "receipt_digest": base_receipt,
            **shared,
        },
        {
            "usage_id": cand_id,
            "price_usd": cand_usd,
            "compute_seconds": cand_compute,
            "tokens": cand_tokens,
            "owner_minutes": cand_owner,
            "arm": "candidate",
            "method_version": "method-cand",
            "release_version": "release-cand",
            "receipt_digest": cand_receipt,
            **shared,
        },
    ]
    return comparison, events


def _report(comparison, events, ownership=None, window=None):
    return summarize_benefit(
        [comparison],
        events,
        _ownership() if ownership is None else ownership,
        _window(comparison.timestamp) if window is None else window,
    )


def test_higher_total_cost_defeats_positive_quality():
    """A positive quality delta cannot adopt when USD savings miss the bound."""
    cheap, cheap_events = _pair(cand_usd=Decimal("4"), suffix="cheap")
    costly, costly_events = _pair(cand_usd=Decimal("14"), suffix="costly")
    window = _window(WHEN, minimum_delta=1.0)
    adopted = _report(cheap, cheap_events, window=window)
    rejected = _report(costly, costly_events, window=window)

    assert adopted.cumulative.quality_deltas == (3.0,)
    assert adopted.cumulative.eligible_deltas == (3.0,)
    assert adopted.cumulative.axes.usd.net == Decimal("6")
    assert adopted.cumulative.axes.owner_minutes.net == Decimal("6")
    assert adopted.cumulative.axes.tokens.net == 60
    assert adopted.adoption_eligible

    assert rejected.cumulative.quality_deltas == (3.0,)
    assert rejected.cumulative.axes.usd.net == Decimal("-4")
    assert rejected.cumulative.axes.compute_seconds.net == Decimal("6")
    assert rejected.cumulative.complete
    assert not rejected.adoption_eligible
    bound = {
        (obs.arm, obs.method_version, obs.release_version, obs.evaluator_version, obs.timestamp)
        for obs in rejected.cumulative.observations
    }
    assert ("candidate", "method-cand", "release-cand", "eval-1", WHEN) in bound
    assert ("baseline", "method-base", "release-base", "eval-1", WHEN) in bound


def test_self_score_cannot_confer_adoption():
    comparison, events = _pair(self_scored=True, cand_usd=Decimal("4"))
    report = _report(comparison, events, window=_window(WHEN, minimum_delta=1.0))
    assert report.cumulative.quality_deltas == (3.0,)
    assert report.cumulative.self_scored
    assert report.cumulative.axes.usd.net == Decimal("6")
    assert not report.adoption_eligible


def test_maintenance_reverses_savings():
    comparison, events = _pair(base_usd=Decimal("10"), cand_usd=Decimal("4"))
    maintenance = CostAxes(Decimal("8"), Decimal("0"), 0, Decimal("0"))
    report = _report(comparison, events, ownership=_ownership(maintenance=maintenance))
    domain = report.series[0].domains[0].summary.axes.usd
    cumulative = report.cumulative.axes.usd
    assert domain.net == Decimal("6")
    assert not domain.one_time_included
    assert cumulative.baseline_operating == Decimal("10")
    assert cumulative.candidate_operating == Decimal("4")
    assert cumulative.maintenance == Decimal("8")
    assert cumulative.research == Decimal("0")
    assert cumulative.net == Decimal("-2")
    assert cumulative.one_time_included
    assert not report.adoption_eligible


def test_one_time_costs_are_charged_once_per_window_and_domains_stay_separate():
    first, first_events = _pair(
        domain="alpha", task="alpha-task", suffix="alpha",
        base_usd=Decimal("6"), cand_usd=Decimal("2"), delta=2.0,
    )
    second, second_events = _pair(
        domain="beta", task="beta-task", suffix="beta",
        base_usd=Decimal("6"), cand_usd=Decimal("2"), delta=2.0,
    )
    maintenance = CostAxes(Decimal("6"), Decimal("0"), 0, Decimal("0"))
    report = summarize_benefit(
        [first, second],
        first_events + second_events,
        _ownership(maintenance=maintenance),
        _window(WHEN),
    )
    nets = {domain.domain: domain.summary.axes.usd.net for domain in report.series[0].domains}
    assert nets == {"alpha": Decimal("4"), "beta": Decimal("4")}
    assert all(not domain.summary.axes.usd.one_time_included for domain in report.series[0].domains)
    # 12 - 4 - 6 once. Charging the 6 twice would leave -4 and block adoption.
    assert report.cumulative.axes.usd.net == Decimal("2")
    assert report.cumulative.fresh_independent_tasks == 2
    assert report.adoption_eligible


def test_replay_cannot_increase_savings_or_n():
    comparison, events = _pair(cand_usd=Decimal("4"))
    once = _report(comparison, events)
    twice = summarize_benefit(
        [comparison, comparison],
        events + events,
        _ownership(),
        _window(WHEN),
    )
    assert once.cumulative.fresh_independent_tasks == 1
    assert once.cumulative.axes.usd.net == Decimal("6")
    assert twice.cumulative.axes.usd.net == once.cumulative.axes.usd.net
    assert twice.cumulative.axes.compute_seconds.net == once.cumulative.axes.compute_seconds.net
    assert twice.cumulative.axes.tokens.net == once.cumulative.axes.tokens.net
    assert twice.cumulative.fresh_independent_tasks == once.cumulative.fresh_independent_tasks
    assert twice.cumulative.quality_deltas == once.cumulative.quality_deltas
    assert twice.replayed >= 1


def test_failures_cost_money_and_are_retained():
    success, success_events = _pair(task="ok-task", suffix="ok", delta=2.0, base_usd=Decimal("10"), cand_usd=Decimal("4"))
    failure, failure_events = _pair(
        task="bad-task", suffix="bad", ok=False, failures=("execution_failed",),
        base_usd=Decimal("1"), cand_usd=Decimal("5"),
        base_compute=Decimal("1"), cand_compute=Decimal("3"),
        base_tokens=1, cand_tokens=9,
        base_owner=Decimal("1"), cand_owner=Decimal("2"),
    )
    success_only = _report(success, success_events)
    combined = summarize_benefit(
        [success, failure],
        success_events + failure_events,
        _ownership(),
        _window(WHEN),
    )
    assert success_only.cumulative.failures == 0
    assert success_only.cumulative.axes.usd.net == Decimal("6")
    assert combined.cumulative.failures == 1
    assert combined.cumulative.failure_reasons == ("execution_failed",)
    assert combined.cumulative.quality_deltas == (2.0,)
    assert combined.cumulative.axes.usd.baseline_operating == Decimal("11")
    assert combined.cumulative.axes.usd.candidate_operating == Decimal("9")
    assert combined.cumulative.axes.usd.net == success_only.cumulative.axes.usd.net + Decimal("1") - Decimal("5")
    assert combined.cumulative.fresh_independent_tasks == 2
    failed = [obs for obs in combined.cumulative.observations if not obs.successful]
    assert len(failed) == 2
    assert {obs.failures for obs in failed} == {("execution_failed",)}
    assert {obs.task_id for obs in failed} == {"bad-task"}


def test_unknown_owner_time_blocks_complete_claim():
    comparison, events = _pair(cand_owner=None, cand_usd=Decimal("4"))
    report = _report(comparison, events, window=_window(WHEN, minimum_delta=1.0))
    assert report.cumulative.quality_deltas == (3.0,)
    assert report.cumulative.axes.usd.net == Decimal("6")
    assert report.cumulative.axes.owner_minutes.net is None
    assert report.cumulative.axes.owner_minutes.candidate_operating is None
    assert not report.cumulative.complete
    assert not report.adoption_eligible


def test_later_window_shows_decay():
    early_when = datetime(2026, 6, 1, tzinfo=UTC)
    late_when = datetime(2026, 7, 1, tzinfo=UTC)
    early_comparison, early_events = _pair(when=early_when, delta=4.0, cand_usd=Decimal("4"), suffix="early")
    late_comparison, late_events = _pair(when=late_when, delta=1.0, cand_usd=Decimal("9"), suffix="late")
    early = _report(early_comparison, early_events)
    late = _report(late_comparison, late_events)
    assert early.series[0].evaluator_version == late.series[0].evaluator_version == "eval-1"
    assert early.cumulative.quality_deltas == (4.0,)
    assert late.cumulative.quality_deltas == (1.0,)
    assert late.cumulative.quality_deltas[0] < early.cumulative.quality_deltas[0]
    assert late.cumulative.axes.usd.net == Decimal("1")
    assert early.cumulative.axes.usd.net == Decimal("6")
    assert late.cumulative.axes.usd.net < early.cumulative.axes.usd.net
    early_ids = {obs.method_version for obs in early.cumulative.observations}
    late_ids = {obs.method_version for obs in late.cumulative.observations}
    assert early_ids == late_ids == {"method-base", "method-cand"}


def test_old_evaluator_evidence_is_a_separate_series():
    older, older_events = _pair(evaluator="eval-old", task="old-task", suffix="old", delta=5.0)
    newer, newer_events = _pair(evaluator="eval-new", task="new-task", suffix="new", delta=0.5)
    report = summarize_benefit(
        [older, newer],
        older_events + newer_events,
        _ownership(),
        _window(WHEN, minimum_delta=1.0),
    )
    by_version = {series.evaluator_version: series for series in report.series}
    assert set(by_version) == {"eval-old", "eval-new"}
    assert by_version["eval-old"].cumulative.quality_deltas == (5.0,)
    assert by_version["eval-new"].cumulative.quality_deltas == (0.5,)
    assert report.cumulative is None
    assert not report.adoption_eligible
    assert not by_version["eval-old"].cumulative.adoption_eligible
    assert not by_version["eval-new"].cumulative.axes.usd.one_time_included
    assert not by_version["eval-old"].cumulative.axes.usd.one_time_included


def test_comparison_report_uses_transfer_and_ignores_development():
    dev = _execution("source", "dev-task", 9.0, content=_content("dev-task"), phase="development")
    confirmation = _execution("target", "conf-task", 2.0, content=_content("conf-task"))
    report = assemble_results([dev, confirmation])
    assert report.transfer
    when = WHEN
    events = []
    for arm_name, method, release, usd in (
        ("baseline", "method-base", "release-base", Decimal("10")),
        ("candidate", "method-cand", "release-cand", Decimal("4")),
    ):
        events.append({
            "usage_id": f"usage-{arm_name}-report",
            "price_usd": usd,
            "compute_seconds": Decimal("2"),
            "tokens": 8,
            "owner_minutes": Decimal("1"),
            "arm": arm_name,
            "domain": "target",
            "task_id": "conf-task",
            "content_sha256": _content("conf-task"),
            "method_version": method,
            "release_version": release,
            "evaluator_version": "eval-1",
            "receipt_digest": f"receipt-{arm_name}-report",
            "recorded_at": when,
        })
    benefit = summarize_benefit(report, events, _ownership(), _window(when))
    assert benefit.cumulative.quality_deltas == (2.0,)
    assert benefit.cumulative.axes.usd.net == Decimal("6")
    assert {obs.domain for obs in benefit.cumulative.observations} == {"target"}
    assert benefit.adoption_eligible


def test_native_usage_id_must_match_the_jobs_digest():
    comparison, events = _pair(suffix="native")
    native = {
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "work_id": "22222222-2222-2222-2222-222222222222",
        "job_id": "job-base",
        "request_id": "req-base",
    }
    digest = sha256(native)
    events[0] = {**events[0], **native, "usage_id": digest}
    comparison = replace(comparison, baseline=replace(comparison.baseline, usage_ids=(digest,)))
    report = _report(comparison, events)
    assert report.cumulative.axes.usd.net == Decimal("6")

    wrong = replace(comparison, baseline=replace(comparison.baseline, usage_ids=("f" * 64,)))
    events[0] = {**events[0], "usage_id": "f" * 64}
    with pytest.raises(ValueError, match="identity_mismatch"):
        _report(wrong, events)


def test_overlap_and_identity_mismatch_and_window_are_rejected():
    first, first_events = _pair(task="one", suffix="one")
    second, second_events = _pair(task="two", suffix="two")
    shared = first.candidate.usage_ids[0]
    second = replace(second, candidate=replace(second.candidate, usage_ids=(shared,)))
    quiet_events = [{key: value for key, value in event.items() if key in {
        "usage_id", "price_usd", "compute_seconds", "tokens", "owner_minutes",
    }} for event in first_events + second_events if event["usage_id"] != second_events[1]["usage_id"]]
    with pytest.raises(ValueError, match="overlap"):
        summarize_benefit([first, second], quiet_events, _ownership(), _window(WHEN))

    mismatched = [{**event, "receipt_digest": "other-receipt"} if event["arm"] == "candidate" else event for event in first_events]
    with pytest.raises(ValueError, match="identity_mismatch"):
        _report(first, mismatched)

    early = datetime(2026, 5, 1, tzinfo=UTC)
    outside, outside_events = _pair(when=early, suffix="out")
    with pytest.raises(ValueError, match="outside_window"):
        _report(outside, outside_events, window=_window(WHEN))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("price_usd", Decimal("-1"), "negative_cost"),
        ("price_usd", Decimal("NaN"), "nonfinite_cost"),
        ("price_usd", Decimal("Infinity"), "nonfinite_cost"),
        ("price_usd", 1.25, "usd_must_be_decimal"),
        ("compute_seconds", float("inf"), "nonfinite_cost"),
        ("compute_seconds", -0.1, "negative_cost"),
        ("tokens", -3, "negative_cost"),
        ("owner_minutes", Decimal("-2"), "negative_cost"),
    ],
)
def test_negative_and_nonfinite_costs_are_rejected(field, value, reason):
    _comparison, events = _pair()
    events[1] = {**events[1], field: value}
    with pytest.raises(ValueError, match=reason):
        _report(_comparison, events)


def test_negative_ownership_cost_is_rejected():
    with pytest.raises(ValueError, match="negative_cost"):
        CostAxes(Decimal("-0.01"), Decimal("0"), 0, Decimal("0"))
