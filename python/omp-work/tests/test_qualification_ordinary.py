"""Ordinary-work gates count one frozen scope and never qualify a release."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from omp_work.qualification_ordinary import (
    CONSECUTIVE_GATE,
    SUSTAINED_GATE,
    Attachment,
    OrdinarySnapshot,
    OrdinaryTask,
    ordinary_gate_reports,
)
from omp_work.qualification_records import (
    FrozenProtocol,
    ScopeIdentity,
    Snapshot,
    scope_boundary,
)

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
RESET_FIELDS = ("recipe", "release", "evaluator")
SHARE_FIELDS = ("release", "configuration", "recipe", "evaluator", "workload", "criteria")


def frozen_scope(**overrides) -> ScopeIdentity:
    values = {
        "release": "rel-1",
        "configuration": "cfg-1",
        "recipe": "role-route",
        "evaluator": "eval-1",
        "workload": "ordinary-mix",
        "criteria": "accept-v1",
    }
    values.update(overrides)
    return ScopeIdentity(**values)


def task(task_id, outcome="accepted", **kwargs) -> OrdinaryTask:
    return OrdinaryTask(
        task_id=task_id,
        record_id=kwargs.pop("record_id", f"src-{task_id}"),
        repository=kwargs.pop("repository", "oh-my-pi"),
        family=kwargs.pop("family", "implement"),
        outcome=outcome,
        scope=kwargs.pop("scope", frozen_scope()),
        **kwargs,
    )


def evaluate(tasks, *, elapsed=timedelta(hours=72), order=None, allocations=()):
    tasks = tuple(tasks)
    protocol = FrozenProtocol(
        proof_record_ids=("proof-1",),
        protocol_sha256="a" * 64,
        scope=frozen_scope(),
        release_baseline_sha256="b" * 64,
        task_order=tuple(order) if order is not None else tuple(item.task_id for item in tasks),
        run_start=START,
        run_end=START + elapsed,
        allocations=tuple(allocations),
        qualified_backend_proof_ids=(),
    )
    return ordinary_gate_reports(OrdinarySnapshot(protocol, tasks))


def test_accepted_failure_accepted_cannot_combine_streak():
    reports = evaluate([
        task("a1"),
        task("bad", outcome="failed"),
        task("a2"),
    ])
    gate = reports.consecutive
    assert gate.count == 0
    assert gate.outcomes.accepted == 2
    assert gate.outcomes.failed == 1
    assert gate.outcomes.instances == 3
    assert gate.count != gate.outcomes.accepted
    assert gate.unresolved_failure_ids == ("bad",)
    assert gate.status == "incomplete"
    assert [item.task_id for item in reports.observations] == ["a1", "bad", "a2"]


def test_recorded_regression_restarts_without_combining_earlier_accepts():
    reports = evaluate([
        task("a1"),
        task("a2"),
        task("bad", outcome="failed", regression_id="reg-1"),
        task("a3"),
        task("a4"),
    ])
    gate = reports.consecutive
    assert gate.count == 2
    assert gate.outcomes.accepted == 4
    assert gate.outcomes.failed == 1
    assert gate.unresolved_failure_ids == ()
    assert any(reset.cause == "counted non-accepted" for reset in gate.resets)


@pytest.mark.parametrize("outcome", ["blocked", "cancelled", "incomplete"])
def test_non_accepted_outcomes_are_separate_and_reset_the_streak(outcome):
    reports = evaluate([task("a1"), task("mid", outcome=outcome), task("a2")])
    gate = reports.consecutive
    assert gate.count == 1
    assert gate.outcomes.accepted == 2
    assert getattr(gate.outcomes, outcome) == 1
    assert gate.total_instances == 3
    assert gate.run_failures == ()
    assert gate.status == "incomplete"


def test_retries_review_fixes_and_resumptions_stay_one_task():
    item = task("only", attachments=(
        Attachment("retry-1", "retry"),
        Attachment("fix-1", "review_fix"),
        Attachment("resume-1", "resumption"),
    ))
    reports = evaluate([item])
    gate = reports.consecutive
    assert gate.count == 1
    assert gate.total_instances == 1
    assert gate.contributing_source_ids == ("src-only",)
    assert len(reports.observations) == 1
    assert reports.observations[0].uses == (CONSECUTIVE_GATE, SUSTAINED_GATE)
    assert reports.observations[0].attachments == item.attachments
    reasons = {exclusion.record_id: exclusion.reason for exclusion in gate.exclusions}
    assert reasons["retry-1"] == "attached retry; not a separate task"
    assert reasons["fix-1"] == "attached review fix; not a separate task"
    assert reasons["resume-1"] == "attached resumption; not a separate task"
    assert "retry-1" not in gate.contributing_source_ids
    assert gate.exclusions is reports.sustained.exclusions


def test_duplicate_task_instance_counts_once():
    reports = evaluate(
        [task("same", record_id="src-a"), task("same", record_id="src-b")],
        order=("same",),
    )
    gate = reports.consecutive
    assert gate.total_instances == 1
    assert gate.contributing_source_ids == ("src-a",)
    assert any(
        exclusion.record_id == "src-b" and exclusion.reason == "duplicate task instance"
        for exclusion in gate.exclusions
    )


def test_frozen_order_keeps_the_failure_and_does_not_select_successes():
    failed = task("bad", outcome="failed", regression_id="reg-1")
    reports = evaluate(
        [task("a"), task("b"), failed],
        order=("bad", "a", "b"),
    )
    assert [item.task_id for item in reports.observations] == ["bad", "a", "b"]
    assert reports.consecutive.contributing_source_ids == ("src-bad", "src-a", "src-b")
    assert reports.consecutive.outcomes.failed == 1
    assert reports.consecutive.count == 2


@pytest.mark.parametrize("cause,mutate", [
    ("counted non-accepted", lambda item: replace(item, outcome="failed")),
    ("unplanned workflow repair", lambda item: replace(item, repairs=("unplanned workflow repair",))),
    ("recipe", lambda item: replace(item, scope=replace(item.scope, recipe="other-recipe"))),
    ("release", lambda item: replace(item, scope=replace(item.scope, release="other-release"))),
    ("evaluator", lambda item: replace(item, scope=replace(item.scope, evaluator="other-evaluator"))),
])
def test_each_reset_cause_returns_the_streak_to_zero(cause, mutate):
    tasks = [task("a1"), task("a2"), mutate(task("break")), task("a3")]
    reports = evaluate(tasks)
    gate = reports.consecutive
    assert any(reset.cause == cause and reset.task_id == "break" for reset in gate.resets)
    assert gate.count == 0
    assert gate.outcomes.accepted >= 2
    assert gate.release_qualified is False
    if cause in RESET_FIELDS:
        assert gate.scope_end is not None
        assert cause in gate.scope_end.fields
        assert "src-a3" not in gate.contributing_source_ids
        assert any(
            exclusion.record_id == "src-a3" and "not restarted" in exclusion.reason
            for exclusion in gate.exclusions
        )
        assert reports.sustained.status == "incomplete"
    elif cause == "unplanned workflow repair":
        assert gate.scope_end is None
        assert "src-a3" in gate.contributing_source_ids
        assert gate.status == "failed"
        assert reports.sustained.status == "failed"
        assert "unplanned workflow repair" in gate.run_failures
    else:
        assert gate.scope_end is None
        assert gate.outcomes.failed == 1
        assert gate.status == "incomplete"
        assert reports.sustained.status == "incomplete"


@pytest.mark.parametrize("repair", [
    "reconstructing grants",
    "patching the runtime",
    "manually reconstructing state",
])
def test_reconstructing_grants_or_runtime_is_unplanned_repair(repair):
    reports = evaluate([
        task("a1"),
        task("break", repairs=(repair,)),
        task("a3"),
    ])
    gate = reports.consecutive
    assert gate.count == 0
    assert gate.status == "failed"
    assert reports.sustained.status == "failed"
    assert "unplanned workflow repair" in gate.run_failures
    assert repair in gate.unplanned_repairs
    assert gate.outcomes.accepted == 3


def test_normal_review_remediation_does_not_reset_or_fail_the_run():
    reports = evaluate(
        [task(f"a{index}", repairs=("normal review remediation",)) for index in range(20)],
        elapsed=timedelta(hours=1),
    )
    assert reports.consecutive.count == 20
    assert reports.consecutive.status == "count reached"
    assert reports.consecutive.run_failures == ()
    assert reports.consecutive.resets == ()
    assert reports.consecutive.release_qualified is False
    assert reports.sustained.count == 20
    assert reports.sustained.duration_met is False
    assert reports.sustained.status == "incomplete"
    assert reports.sustained.release_qualified is False


def test_nineteen_consecutive_accepts_do_not_reach_the_streak():
    reports = evaluate([task(f"a{index}") for index in range(19)])
    assert reports.consecutive.count == 19
    assert reports.consecutive.status == "incomplete"
    assert reports.consecutive.release_qualified is False


def test_configuration_drift_ends_scope_without_restarting_or_discarding_failures():
    drifted = replace(frozen_scope(), configuration="cfg-2")
    reports = evaluate([
        task("a1"),
        task("bad", outcome="failed", regression_id="reg-1"),
        task("a2"),
        task("drift", outcome="failed", scope=drifted),
        task("later"),
    ])
    gate = reports.consecutive
    assert gate.scope_end is not None
    assert gate.scope_end.fields == ("configuration",)
    assert gate.scope_end.outcome == "failed"
    assert gate.scope_end.record_id == "src-drift"
    assert all(reset.cause != "configuration" for reset in gate.resets)
    assert gate.outcomes.accepted == 2
    assert gate.outcomes.failed == 1
    assert gate.count == 1
    assert gate.total_instances == 3
    assert "src-later" not in gate.contributing_source_ids
    assert "src-drift" not in gate.contributing_source_ids
    assert any(
        exclusion.record_id == "src-later" and "not restarted" in exclusion.reason
        for exclusion in gate.exclusions
    )
    assert any(
        exclusion.record_id == "src-drift" and "outcome failed retained" in exclusion.reason
        for exclusion in gate.exclusions
    )
    assert [item.task_id for item in reports.observations] == ["a1", "bad", "a2", "drift"]
    assert reports.observations[-1].uses == ()
    assert reports.sustained.status == "incomplete"


def test_71_hours_misses_duration_without_inferring_task_span():
    tasks = [
        task("a", started_at=START + timedelta(hours=80), repository="oh-my-pi", family="implement"),
        task("b", started_at=START, repository="media-discovery", family="review"),
        task("c", started_at=START + timedelta(hours=1), repository="oh-my-pi", family="implement"),
    ]
    reports = evaluate(tasks, elapsed=timedelta(hours=71))
    sustained = reports.sustained
    assert sustained.elapsed == timedelta(hours=71)
    assert sustained.duration_met is False
    assert sustained.count == 3
    assert sustained.status == "incomplete"
    assert sustained.release_qualified is False
    assert [item.task_id for item in reports.observations] == ["a", "b", "c"]
    assert sustained.coverage.missing_repositories == ()
    assert sustained.coverage.multiple_families is True


def test_exactly_72_hours_meets_duration_and_one_second_short_does_not():
    tasks = [task("a", repository="oh-my-pi"), task("b", repository="media-discovery", family="review")]
    met = evaluate(tasks, elapsed=timedelta(hours=72)).sustained
    short = evaluate(tasks, elapsed=timedelta(hours=72) - timedelta(seconds=1)).sustained
    assert met.elapsed == timedelta(hours=72)
    assert met.duration_met is True
    assert short.duration_met is False
    assert met.status == "incomplete"
    assert met.count == 2
    assert met.release_qualified is False


def test_missing_repository_and_family_coverage_is_visible():
    tasks = [task(f"t{index}", repository="oh-my-pi", family="implement") for index in range(30)]
    sustained = evaluate(tasks, elapsed=timedelta(hours=72)).sustained
    assert sustained.count == 30
    assert sustained.duration_met is True
    assert sustained.coverage.repositories == ("oh-my-pi",)
    assert sustained.coverage.missing_repositories == ("media-discovery",)
    assert sustained.coverage.families == ("implement",)
    assert sustained.coverage.multiple_families is False
    assert sustained.status == "incomplete"
    assert sustained.release_qualified is False


def test_29_distinct_tasks_do_not_reach_the_sustained_count():
    tasks = [
        task(
            f"t{index}",
            repository="oh-my-pi" if index % 2 == 0 else "media-discovery",
            family="implement" if index < 10 else "review",
        )
        for index in range(29)
    ]
    sustained = evaluate(tasks, elapsed=timedelta(hours=72)).sustained
    assert sustained.count == 29
    assert sustained.coverage.missing_repositories == ()
    assert sustained.coverage.multiple_families is True
    assert sustained.duration_met is True
    assert sustained.status == "incomplete"


def test_sustained_count_includes_every_outcome_and_does_not_qualify_release():
    cycle = (
        ["accepted"] * 10 + ["failed"] * 10 + ["blocked"] * 5 + ["cancelled"] * 3 + ["incomplete"] * 2
    )
    tasks = [
        task(
            f"t{index}",
            outcome,
            repository="oh-my-pi" if index % 2 == 0 else "media-discovery",
            family="implement" if index < 15 else "review",
            regression_id="reg" if outcome == "failed" else None,
        )
        for index, outcome in enumerate(cycle)
    ]
    reports = evaluate(tasks, elapsed=timedelta(hours=72))
    sustained = reports.sustained
    assert sustained.count == 30
    assert sustained.outcomes.accepted == 10
    assert sustained.outcomes.failed == 10
    assert sustained.outcomes.blocked == 5
    assert sustained.outcomes.cancelled == 3
    assert sustained.outcomes.incomplete == 2
    assert sustained.outcomes.instances == 30
    assert sustained.count != sustained.outcomes.accepted
    assert sustained.duration_met is True
    assert sustained.coverage.missing_repositories == ()
    assert sustained.coverage.multiple_families is True
    assert sustained.status == "count reached"
    assert sustained.release_qualified is False
    assert sustained.run_failures == ()
    assert reports.consecutive.status == "incomplete"
    assert reports.consecutive.release_qualified is False
    assert len(reports.observations) == 30
    assert all(item.uses == (CONSECUTIVE_GATE, SUSTAINED_GATE) for item in reports.observations)


@pytest.mark.parametrize("field", SHARE_FIELDS)
def test_each_sharing_mismatch_prevents_dual_use(field):
    shifted_scope = replace(frozen_scope(), **{field: "shifted"})
    tasks = [
        task("keep"),
        task("shift", scope=shifted_scope),
        task("later"),
    ]
    reports = evaluate(tasks)
    assert [item.task_id for item in reports.observations] == ["keep", "shift"]
    assert reports.observations[0].uses == (CONSECUTIVE_GATE, SUSTAINED_GATE)
    assert reports.observations[1].uses == ()
    assert reports.consecutive.scope_end is not None
    assert reports.consecutive.scope_end.fields == scope_boundary(frozen_scope(), shifted_scope)
    assert reports.consecutive.scope_end.fields == (field,)
    assert reports.consecutive.contributing_source_ids == ("src-keep",)
    assert reports.sustained.contributing_source_ids == ("src-keep",)
    assert "src-later" not in reports.consecutive.contributing_source_ids
    assert reports.consecutive.exclusions is reports.sustained.exclusions
    if field in RESET_FIELDS:
        assert reports.consecutive.count == 0
        assert any(reset.cause == field for reset in reports.consecutive.resets)
    else:
        assert reports.consecutive.count == 1
        assert all(reset.cause != field for reset in reports.consecutive.resets)


@pytest.mark.parametrize("flag", [
    "lost accepted record",
    "duplicate effect",
    "unauthorized admission",
    "false acceptance",
    "unplanned workflow repair",
])
def test_run_failures_are_reported_failed_not_incomplete(flag):
    if flag == "unplanned workflow repair":
        item = task("t", repairs=(flag,))
    else:
        item = task("t", integrity=(flag,))
    reports = evaluate([item], elapsed=timedelta(hours=71))
    for gate in (reports.consecutive, reports.sustained):
        assert gate.status == "failed"
        assert gate.duration_met is False
        assert flag in gate.run_failures
        assert gate.release_qualified is False
    assert reports.sustained.total_instances == 1
    assert reports.sustained.count == 1
    assert reports.consecutive.count == 0
    assert reports.consecutive.outcomes.accepted == 1


def test_negative_science_debugging_infrastructure_and_planned_recovery_are_separate():
    names = (
        "negative science",
        "candidate debugging",
        "infrastructure failure",
        "planned recovery",
        "planned fault injection",
    )
    tasks = [
        task(f"c{index}", outcome="failed", categories=(name,), regression_id="reg")
        for index, name in enumerate(names)
    ]
    tasks.append(task("experiment", outcome="failed", regression_id="reg"))
    reports = evaluate(tasks, elapsed=timedelta(hours=1))
    categories = reports.sustained.categories
    assert categories is not None
    assert categories.negative_science == 1
    assert categories.candidate_debugging == 1
    assert categories.infrastructure_failure == 1
    assert categories.planned_recovery == 1
    assert categories.planned_fault_injection == 1
    assert reports.sustained.outcomes.failed == 6
    assert reports.sustained.outcomes.accepted == 0
    assert reports.sustained.run_failures == ()
    assert reports.sustained.status == "incomplete"
    assert reports.consecutive.status == "incomplete"
    assert reports.consecutive.categories == categories


@pytest.mark.parametrize("blank", ["protocol", "freeze", "baseline"])
def test_missing_freeze_or_baseline_is_not_started(blank):
    item = task("a", attachments=(Attachment("retry-1", "retry"),))
    if blank == "protocol":
        snapshot = OrdinarySnapshot(None, (item,))
    else:
        reports_for_protocol = evaluate([item]).consecutive
        assert reports_for_protocol.status == "incomplete"
        protocol = FrozenProtocol(
            proof_record_ids=("proof-1",),
            protocol_sha256="" if blank == "freeze" else "a" * 64,
            scope=frozen_scope(),
            release_baseline_sha256="" if blank == "baseline" else "b" * 64,
            task_order=("a",),
            run_start=START,
            run_end=START + timedelta(hours=80),
            allocations=(("oh-my-pi", 30),),
            qualified_backend_proof_ids=(),
        )
        snapshot = OrdinarySnapshot(protocol, (item,))
    reports = ordinary_gate_reports(snapshot)
    for gate in (reports.consecutive, reports.sustained):
        assert gate.status == "not started"
        assert gate.count is None
        assert gate.total_instances is None
        assert gate.outcomes is None
        assert gate.elapsed is None
        assert gate.duration_met is None
        assert gate.coverage is None
        assert gate.categories is None
        assert gate.allocations == ()
        assert gate.strata_status == "not preregistered"
        assert gate.contributing_source_ids == ()
        assert gate.release_qualified is False
        assert "acceptance-bound O9 freeze" in gate.missing_proof or blank == "baseline"
        assert "release baseline" in gate.missing_proof or blank == "freeze"
    reasons = {exclusion.record_id: exclusion.reason for exclusion in reports.consecutive.exclusions}
    assert reasons == {
        "src-a": "missing O9 freeze or release baseline",
        "retry-1": "missing O9 freeze or release baseline",
    }
    assert all(item.uses == () for item in reports.observations)


def test_empty_allocations_are_not_invented_and_preregistered_ones_are_copied():
    measured = evaluate([task("a")])
    assert measured.consecutive.allocations == ()
    assert measured.consecutive.strata_status == "not preregistered"
    assert measured.consecutive.missing_proof == ("preregistered strata",)
    assert measured.consecutive.count == 1
    assert ("accepted", 20) not in measured.sustained.allocations
    copied = evaluate([task("a")], allocations=(("media-discovery", 4), ("recovery", 1)))
    assert copied.sustained.allocations == (("media-discovery", 4), ("recovery", 1))
    assert copied.sustained.strata_status == "preregistered"
    assert copied.sustained.missing_proof == ()
    assert copied.sustained.release_qualified is False
    assert copied.consecutive.allocations is copied.sustained.allocations


def test_record_snapshot_without_a_freeze_does_not_start():
    reports = ordinary_gate_reports(Snapshot((), (), (), ()))
    assert reports.consecutive.status == "not started"
    assert reports.sustained.status == "not started"
    assert reports.sustained.count is None
    assert reports.observations == ()


def test_frozen_protocol_without_tasks_is_started_at_zero():
    reports = evaluate(())
    assert reports.consecutive.status == "incomplete"
    assert reports.consecutive.count == 0
    assert reports.sustained.count == 0
    assert reports.sustained.total_instances == 0
    assert reports.sustained.outcomes is not None
    assert reports.sustained.outcomes.instances == 0
    assert reports.sustained.elapsed == timedelta(hours=72)
    assert reports.sustained.coverage is not None
    assert reports.sustained.coverage.missing_repositories == ("oh-my-pi", "media-discovery")
    assert reports.sustained.coverage.multiple_families is False
    assert reports.consecutive.release_qualified is False
    assert reports.sustained.status == "incomplete"


def test_absent_frozen_task_does_not_join_accepts_around_the_hole():
    reports = evaluate([task("a"), task("b")], order=("a", "missing", "b"))
    gate = reports.consecutive
    assert gate.count == 0
    assert gate.outcomes.accepted == 2
    assert gate.unresolved_failure_ids == ()
    assert gate.total_instances == 2
    assert any(
        exclusion.record_id == "missing" and exclusion.reason == "frozen task absent from snapshot"
        for exclusion in gate.exclusions
    )


def test_successes_outside_frozen_order_are_excluded():
    reports = evaluate([task("out"), task("in")], order=("in",))
    assert [item.task_id for item in reports.observations] == ["in"]
    assert reports.consecutive.count == 1
    assert reports.consecutive.contributing_source_ids == ("src-in",)
    assert any(
        exclusion.record_id == "src-out" and exclusion.reason == "not in frozen task order"
        for exclusion in reports.consecutive.exclusions
    )


def test_repeat_evaluation_is_identical():
    item = task("a")
    snapshot = OrdinarySnapshot(
        FrozenProtocol(
            proof_record_ids=("proof-1",),
            protocol_sha256="a" * 64,
            scope=frozen_scope(),
            release_baseline_sha256="b" * 64,
            task_order=("a",),
            run_start=START,
            run_end=START + timedelta(hours=72),
            allocations=(),
            qualified_backend_proof_ids=(),
        ),
        (item,),
    )
    assert ordinary_gate_reports(snapshot) == ordinary_gate_reports(snapshot)
