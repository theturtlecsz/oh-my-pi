"""Counter report: deterministic JSON for the three gates, sharing observations once."""
import json
from datetime import datetime, timedelta, timezone

from omp_work import qualification_counter as counter
from omp_work.qualification_attempts import AttemptGateReport, StratumCoverage
from omp_work.qualification_ordinary import (
    Attachment, CategoryTotals, Coverage, OrdinaryGateReport, OrdinaryGateReports,
    OutcomeTotals, Reset, ScopeEnd, SharedObservation,
)
from omp_work.qualification_records import (
    Exclusion, ExecutionAttempt, FrozenProtocol, Observation, Receipt, ResourceUsage, ScopeIdentity,
    Snapshot,
)

START = datetime(2026, 10, 13, tzinfo=timezone.utc)
SCOPE = ScopeIdentity("release", "config", "recipe", "evaluator", "workload", "criteria")
BOTH = ("20-consecutive-accepted", "72h/30-distinct-task")


def render(snapshot):
    return json.loads(json.dumps(counter.build_report(snapshot), sort_keys=True))


def test_empty_snapshot_reports_three_not_started_gates_without_targets_allocations():
    report = render(Snapshot((), (), (), ()))
    gates = report["gates"]
    for name in gates:
        assert gates[name]["status"] == "not started"
        assert gates[name]["count"] is None
        assert gates[name]["strata"] == {"status": "not preregistered", "allocations": []}
        assert "preregistered strata" in gates[name]["missing_proof"] or name == "attempts"
    assert gates["attempts"]["missing_proof"]
    assert gates["consecutive"]["target"] == {"accepted_trials": 20}
    assert gates["sustained"]["target"] == {"duration_seconds": 72 * 3600.0, "distinct_tasks": 30}
    assert gates["attempts"]["target"] == {"execution_attempts": 100}
    assert report["frozen"] is None and report["observations"] == []
    assert report["grants_acceptance"] is False
    assert all(row["status"] == "not preregistered" for row in gates["attempts"]["coverage"])
    assert all(row["allocation"] is None for row in gates["attempts"]["coverage"])


def test_legacy_snapshot_keeps_every_exclusion_reason_when_ordinary_replaces_them():
    snapshot = Snapshot(
        (Receipt("receipt-1", "h", "{}"),), (), ("accept-1",),
        (Exclusion("receipt-1", "invalid receipt: x"), Exclusion("orphan", "O9 freeze and release baseline unavailable")),
    )
    gates = render(snapshot)["gates"]
    for name in ("consecutive", "sustained", "attempts"):
        pairs = {(row["record_id"], row["reason"]) for row in gates[name]["exclusions"]}
        assert ("receipt-1", "invalid receipt: x") in pairs
        assert ("orphan", "O9 freeze and release baseline unavailable") in pairs
    ordinary = {(r["record_id"], r["reason"]) for r in gates["consecutive"]["exclusions"]}
    assert ("receipt-1", "missing O9 freeze or release baseline") in ordinary


def mixed(monkeypatch):
    protocol = FrozenProtocol(
        ("freeze-proof",), "freeze", SCOPE, "baseline", ("t1", "t2"), START,
        START + timedelta(hours=72), (("alloc", 2),), ("backend-proof",),
    )
    outcomes = OutcomeTotals(accepted=1, failed=1, blocked=1, cancelled=1)
    ordinary = lambda gate, count: OrdinaryGateReport(
        gate, "incomplete", count, 4, outcomes, ("src-1", "src-2"),
        (Exclusion("retry-1", "attached retry; not a separate task"),),
        (Reset("counted non-accepted", "t2", "src-2"),),
        ScopeEnd("t3", "src-3", ("recipe",), "failed"), timedelta(hours=72), True,
        Coverage(("oh-my-pi",), ("media-discovery",), ("a",), False), ("duplicate effect",),
        CategoryTotals(negative_science=1), ("t2",), ("grant repair",), False,
        "preregistered", (("alloc", 2),), (),
    )
    reports = OrdinaryGateReports(ordinary(BOTH[0], 1), ordinary(BOTH[1], 4), (
        SharedObservation("t1", "src-1", (BOTH[0],), (Attachment("retry-1", "retry"),)),
        SharedObservation("t1", "src-1", (BOTH[1],), (Attachment("retry-1", "retry"),)),
        SharedObservation("t2", "src-2", (), ()),
    ))
    row = ExecutionAttempt(
        "delivery-1", "freeze", SCOPE, START, "exec-1", "exp", "trial", "t1", "job-1", "cpu",
        "trial-proof", "job-proof", "start-proof", START, "experiment-execution", "failed",
        resources=(ResourceUsage("cpu", "measured", "seconds", 12.5, ("res-proof",)),
                   ResourceUsage("cash", "cash", "USD", None, ())),
    )
    attempts = AttemptGateReport(
        "within-run/100-execution-attempt", "available", 1,
        ("freeze-proof", "delivery-1", "res-proof"),
        (Exclusion("poll", "duplicate delivery, polling or reconciliation of execution attempt"),),
        "preregistered", (("alloc", 2),), ("preregistered stratum: recovery",),
        execution_attempt_ids=("exec-1",), queued_record_ids=("q-1",), queued_job_ids=("qj",),
        queued_count=1, execution_outcomes=(("failed", 1), ("completed", 2)),
        scientific_outcomes=((None, 1), ("negative science", 1)),
        coverage=(StratumCoverage("repository", "r", "preregistered", 2, ("p",), ("exec-1",), ("e",)),),
        retained_candidates=(row,), scope_end=START + timedelta(hours=5),
        scope_endings=(Exclusion("drift", "frozen scope drift: recipe"),), protocol=protocol,
    )
    monkeypatch.setattr(counter, "ordinary_gate_reports", lambda snapshot: reports)
    monkeypatch.setattr(counter, "attempt_gate_report", lambda snapshot: attempts)
    return Snapshot(
        (Receipt("src-1", "sha", '{"a": 1}'),),
        (Observation("src-2", "sha2", '{"b": 2}', START, "ref", "trial", "failed"),),
        (), (Exclusion("snap", "snapshot reason"),), protocol,
    )


def test_mixed_report_serializes_every_contributor_exclusion_and_resource(monkeypatch):
    report = render(mixed(monkeypatch))
    sustained, attempts = report["gates"]["sustained"], report["gates"]["attempts"]
    assert sustained["outcomes"] == {"accepted": 1, "failed": 1, "blocked": 1, "cancelled": 1, "incomplete": 0}
    assert sustained["contributing_record_ids"] == ["src-1", "src-2"]
    assert sustained["resets"] == [{"cause": "counted non-accepted", "task_id": "t2", "record_id": "src-2"}]
    assert sustained["scope_end"]["fields"] == ["recipe"]
    assert sustained["elapsed_seconds"] == 72 * 3600.0
    assert sustained["run_failures"] == ["duplicate effect"]
    assert sustained["coverage"]["missing_repositories"] == ["media-discovery"]
    assert [r["record_id"] for r in sustained["exclusions"]] == ["retry-1", "snap"]
    assert report["frozen"]["proof_record_ids"] == ["freeze-proof"]
    assert report["frozen"]["run_duration_seconds"] == 72 * 3600.0
    assert attempts["contributing_record_ids"] == ["freeze-proof", "delivery-1", "res-proof"]
    assert attempts["queued"] == {"record_ids": ["q-1"], "job_ids": ["qj"], "count": 1}
    assert attempts["execution_outcomes"] == [
        {"outcome": "completed", "count": 2}, {"outcome": "failed", "count": 1}]
    assert attempts["scientific_outcomes"][-1] == {"outcome": None, "count": 1}
    assert attempts["scope_end"] == (START + timedelta(hours=5)).isoformat()
    assert attempts["scope_endings"] == [{"record_id": "drift", "reason": "frozen scope drift: recipe"}]
    resources = attempts["retained_candidates"][0]["resources"]
    assert resources[0] == {"name": "cpu", "basis": "measured", "unit": "seconds",
                            "amount": 12.5, "evidence_ids": ["res-proof"]}
    assert resources[1]["amount"] is None
    assert attempts["retained_candidates"][0]["started_at"] == START.isoformat()


def test_shared_observation_appears_once_with_both_uses_and_source_data(monkeypatch):
    report = render(mixed(monkeypatch))
    shared = [o for o in report["observations"] if o["record_id"] == "src-1"]
    assert len(shared) == 1
    assert shared[0]["uses"] == list(BOTH)
    assert shared[0]["attachments"] == [{"record_id": "retry-1", "kind": "retry"}]
    assert shared[0]["source"]["payload"] == {"a": 1} and shared[0]["source"]["kind"] == "receipt"
    other = next(o for o in report["observations"] if o["record_id"] == "src-2")
    assert other["source"]["kind"] == "observation" and other["source"]["observed_at"] == START.isoformat()
    gates = report["gates"]
    assert gates["consecutive"]["observation_ids"] == ["src-1"]
    assert gates["sustained"]["observation_ids"] == ["src-1"]
    assert "shared_observations" not in report and "observations" not in gates["sustained"]


def test_output_is_stable_across_calls(monkeypatch):
    snapshot = mixed(monkeypatch)
    first = json.dumps(counter.build_report(snapshot), sort_keys=True)
    assert json.dumps(counter.build_report(snapshot), sort_keys=True) == first
