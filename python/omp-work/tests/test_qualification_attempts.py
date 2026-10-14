"""Gate 3 counts executions within one freeze, never successes or lifetime jobs."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omp_work.qualification_attempts import DIMENSIONS, attempt_gate_report
from omp_work.qualification_records import (
    DeclaredExperiment, ExecutionAttempt, FrozenProtocol, PreregisteredStratum,
    QualifiedBackend, Receipt, ResourceUsage, ScopeEvent, ScopeIdentity, Snapshot,
    StratumEvidence, decode_evidence,
)

START = datetime(2026, 10, 13, tzinfo=timezone.utc)
SCOPE = ScopeIdentity("release", "config", "recipe", "evaluator", "workload", "criteria")


def candidate(index=0, **changes):
    return replace(ExecutionAttempt(
        record_id=f"delivery-{index}", protocol_sha256="freeze", scope=SCOPE,
        recorded_at=START + timedelta(minutes=index + 1),
        execution_attempt_id=f"execution-{index}", experiment_id="experiment",
        trial_id="trial", task_id="task", job_id=f"job-{index}", backend_id="cpu",
        trial_evidence_id="trial-proof", job_evidence_id=f"job-proof-{index}",
        start_evidence_id=f"start-proof-{index}", started_at=START + timedelta(minutes=index + 1),
        kind="experiment-execution", execution_outcome="completed",
        scientific_outcome="negative science", category="negative science",
        resources=(ResourceUsage("cpu", "measured", "seconds", 20 + index, ("resource-proof",)),
                   ResourceUsage("cash", "cash", "USD", None, ())),
        strata=tuple(StratumEvidence(d, "declared", ("coverage-proof",)) for d in DIMENSIONS),
    ), **changes)


def snapshot(*rows, strata=True):
    protocol = FrozenProtocol(
        ("freeze-proof",), "freeze", SCOPE, "baseline", ("task",), START,
        START + timedelta(hours=72), (("recorded-allocation", 1),), ("backend-proof",),
        (DeclaredExperiment("experiment", "trial", "task", ("declaration-proof",)),),
        (QualifiedBackend("cpu", ("experiment",), ("backend-proof",)),),
        tuple(PreregisteredStratum(d, "declared", 1, ("strata-proof",)) for d in DIMENSIONS)
        if strata else (),
    )
    source_ids = {"freeze-proof", "backend-proof", "declaration-proof", "trial-proof",
                  "strata-proof", "coverage-proof", "resource-proof"}
    source_ids.update(eid for row in rows for eid in (row.job_evidence_id, row.start_evidence_id) if eid)
    # Trusted internal projections; decoder intentionally cannot invent a freeze.
    return Snapshot(tuple(Receipt(eid, "hash", "{}") for eid in sorted(source_ids)),
                    (), (), (), protocol, tuple(rows))


def reasons(report):
    return {item.record_id: item.reason for item in report.exclusions}


def test_repeated_delivery_polling_and_reconciliation_count_once_with_all_source_ids():
    first = candidate()
    rows = (first, replace(first, record_id="poll"), replace(first, record_id="reconciliation"))
    report = attempt_gate_report(snapshot(*rows))
    assert report.count == 1
    assert report.execution_attempt_ids == ("execution-0",)
    assert set(reasons(report)) == {"poll", "reconciliation"}
    assert all("duplicate" in reason for reason in reasons(report).values())
    assert set(report.contributing_record_ids) == {
        "delivery-0", "freeze-proof", "trial-proof", "job-proof-0", "start-proof-0",
        "backend-proof", "declaration-proof", "resource-proof", "strata-proof", "coverage-proof",
    }
    assert report.retained_candidates == rows


def test_failed_execution_and_genuine_retry_count_twice_without_claiming_scientific_success():
    failed = candidate(execution_outcome="failed", scientific_outcome=None,
                       category="infrastructure failure")
    retry = candidate(1, retry_of="execution-0")
    report = attempt_gate_report(snapshot(failed, retry))
    assert report.count == 2
    assert dict(report.execution_outcomes) == {"failed": 1, "completed": 1}
    assert dict(report.scientific_outcomes) == {None: 1, "negative science": 1}
    assert report.retained_candidates[1].retry_of == "execution-0"
    assert [row.resources[0].amount for row in report.retained_candidates] == [20, 21]
    assert all(row.resources[1].amount is None for row in report.retained_candidates)


def test_new_attempt_id_on_same_start_is_not_genuine_retry():
    first = candidate()
    relabeled = candidate(1, start_evidence_id=first.start_evidence_id)
    report = attempt_gate_report(snapshot(first, relabeled))
    assert report.count == 1
    assert "not a genuine retry" in reasons(report)["delivery-1"]


@pytest.mark.parametrize("changes,reason", [
    ({"kind": "synthesis"}, "generic research"),
    ({"kind": "compute"}, "non-execution"),
    ({"backend_id": "unavailable-gpu"}, "unqualified"),
    ({"experiment_id": "undeclared"}, "not declared"),
    ({"task_id": "easier-task"}, "not declared"),
    ({"trial_id": "another-trial"}, "not declared"),
    ({"trial_evidence_id": None}, "trial/job evidence"),
    ({"job_evidence_id": None}, "trial/job evidence"),
    ({"execution_attempt_id": None}, "stable execution-attempt id"),
    ({"start_evidence_id": None}, "actual start evidence"),
    ({"started_at": None, "execution_outcome": "PASS"}, "actual start evidence"),
    ({"started_at": START - timedelta(seconds=1)}, "outside frozen"),
    ({"started_at": START + timedelta(hours=72)}, "outside frozen"),
    ({"protocol_sha256": "other-run"}, "different frozen run"),
    ({"replacement_for": "failed-attempt"}, "swapped in after run start"),
])
def test_nonqualifying_candidates_contribute_zero_and_keep_reason_and_resources(changes, reason):
    row = candidate(**changes)
    report = attempt_gate_report(snapshot(row))
    assert report.count == 0
    assert report.execution_attempt_ids == ()
    assert reason in reasons(report)[row.record_id]
    assert report.retained_candidates == (row,)


def test_queued_deliveries_have_separate_ids_and_distinct_job_count():
    queued = candidate(started_at=None, start_evidence_id=None, execution_outcome="queued",
                       execution_attempt_id=None)
    duplicate = replace(queued, record_id="queued-redelivery")
    second = candidate(1, started_at=None, start_evidence_id=None, execution_outcome="queued")
    report = attempt_gate_report(snapshot(queued, duplicate, second))
    assert report.count == 0
    assert set(report.queued_record_ids) == {"delivery-0", "queued-redelivery", "delivery-1"}
    assert report.queued_job_ids == ("job-0", "job-1")
    assert report.queued_count == 2
    assert all("queued but never started" in reason for reason in reasons(report).values())


def test_unqualified_queued_job_is_still_reported_as_never_started():
    row = candidate(started_at=None, start_evidence_id=None, execution_outcome="queued",
                    backend_id="unavailable-gpu")
    report = attempt_gate_report(snapshot(row))
    assert report.count == 0
    assert report.queued_count == 1
    assert report.queued_job_ids == (row.job_id,)
    assert "unqualified" in reasons(report)[row.record_id]


def test_backend_proof_must_resolve_and_apply_to_declared_experiment():
    data = snapshot(candidate())
    unrelated = replace(data.protocol.backends[0], experiment_ids=("other-experiment",))
    report = attempt_gate_report(replace(data, protocol=replace(data.protocol, backends=(unrelated,))))
    assert report.count == 0
    assert "not applicable" in reasons(report)["delivery-0"]
    data = replace(data, receipts=tuple(r for r in data.receipts if r.record_id != "backend-proof"))
    report = attempt_gate_report(data)
    assert report.count == 0
    assert "unqualified" in reasons(report)["delivery-0"]


@pytest.mark.parametrize("field", ["release", "configuration", "recipe", "evaluator", "workload", "criteria"])
def test_drift_ends_scope_even_after_reversion_and_does_not_migrate_other_runs(field):
    first = candidate()
    after = candidate(2)
    data = snapshot(first, after)
    drift = ScopeEvent("drift", "freeze", START + timedelta(minutes=2),
                       replace(SCOPE, **{field: "changed"}))
    other_run = replace(drift, record_id="other-drift", protocol_sha256="other-run",
                        observed_at=START)
    report = attempt_gate_report(replace(data, scope_events=(drift, other_run)))
    assert report.count == 1
    assert report.scope_end == drift.observed_at
    assert report.scope_endings[0].record_id == "drift"
    assert "ended at scope drift" in reasons(report)[after.record_id]


def test_attempt_scope_drift_also_ends_run():
    drift = candidate(1, scope=replace(SCOPE, configuration="new"))
    report = attempt_gate_report(snapshot(candidate(), drift, candidate(2)))
    assert report.count == 1
    assert "configuration" in reasons(report)[drift.record_id]
    assert "ended at scope drift" in reasons(report)["delivery-2"]


@pytest.mark.parametrize("missing", ["protocol", "release", "interval", "proof"])
def test_missing_freeze_or_baseline_never_reports_zero_progress(missing):
    data = snapshot(candidate())
    if missing == "protocol":
        data = replace(data, protocol=None)
    elif missing == "release":
        data = replace(data, protocol=replace(data.protocol, release_baseline_sha256=""))
    elif missing == "interval":
        data = replace(data, protocol=replace(data.protocol, run_end=START + timedelta(hours=73)))
    else:
        data = replace(data, receipts=tuple(r for r in data.receipts if r.record_id != "freeze-proof"))
    report = attempt_gate_report(data)
    assert (report.status, report.count) == ("not started", None)
    assert report.strata_status == "not preregistered"
    assert report.allocations == ()
    assert "delivery-0" in reasons(report)
    assert report.retained_candidates == data.attempts


def test_existing_records_cannot_invent_attempts_or_freeze():
    report = attempt_gate_report(decode_evidence())
    assert (report.status, report.count) == ("not started", None)
    assert report.target == 100


@pytest.mark.parametrize("gap", ["unregistered", "missing-evidence", "under-allocation", "missing-dimension"])
def test_100_executions_cannot_hide_missing_preregistration_or_coverage(gap):
    rows = tuple(candidate(i) for i in range(100))
    data = snapshot(*rows, strata=gap != "unregistered")
    if gap == "missing-evidence":
        rows = tuple(replace(row, strata=tuple(s for s in row.strata if s.dimension != "recovery"))
                     for row in rows)
        data = replace(data, attempts=rows)
    elif gap == "under-allocation":
        strata = tuple(replace(s, allocation=101) if s.dimension == "cancellation" else s
                       for s in data.protocol.strata)
        data = replace(data, protocol=replace(data.protocol, strata=strata))
    elif gap == "missing-dimension":
        data = replace(data, protocol=replace(data.protocol,
                       strata=tuple(s for s in data.protocol.strata if s.dimension != "independent-campaign")))
    report = attempt_gate_report(data)
    assert report.count == 100
    assert report.coverage_met is False
    assert report.target_reached is False
    if gap in {"unregistered", "missing-dimension"}:
        assert report.strata_status == "not preregistered"
        absent = [s for s in report.coverage if s.status == "not preregistered"]
        assert all(s.allocation is None and s.proof_record_ids == () for s in absent)
    else:
        assert report.strata_status == "preregistered"
        assert report.allocations == data.protocol.allocations


def test_coverage_shows_recorded_allocations_and_evidence_without_invented_limits():
    report = attempt_gate_report(snapshot(*(candidate(i) for i in range(100))))
    assert report.target_reached is True
    assert {s.dimension for s in report.coverage} == set(DIMENSIONS)
    assert all(s.allocation == 1 and s.proof_record_ids == ("strata-proof",)
               and s.evidence_ids == ("coverage-proof",) and len(s.execution_attempt_ids) == 100
               for s in report.coverage)


def test_report_is_pure_and_reads_no_files_or_writes(monkeypatch, tmp_path):
    data = snapshot(candidate(execution_outcome="failed"))
    before = repr(data)
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure counter attempted file I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    report = attempt_gate_report(data)
    assert report.count == 1
    assert repr(data) == before
    assert list(tmp_path.iterdir()) == []
