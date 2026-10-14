"""Counter report and the read-only qualification count command."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from omp_work import qualification_counter as counter
from omp_work import qualification_snapshot
from omp_work.__main__ import main
from omp_work.operations.config import OperationsConfig
from omp_work.qualification_attempts import AttemptGateReport, StratumCoverage
from omp_work.qualification_ordinary import (
    Attachment, CategoryTotals, Coverage, OrdinaryGateReport, OrdinaryGateReports,
    OutcomeTotals, Reset, ScopeEnd, SharedObservation,
)
from omp_work.qualification_records import (
    Exclusion, ExecutionAttempt, FrozenProtocol, Observation, Receipt, ResourceUsage, ScopeIdentity,
    Snapshot, decode_evidence,
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


_CORRUPTION_PREFIXES = (
    "invalid receipt:",
    "invalid observation:",
    "invalid acceptance evidence:",
)


def _document(snapshot):
    return json.dumps(counter.build_report(snapshot), sort_keys=True) + "\n"


def _corruption_text(snapshot):
    return "".join(
        f"{item.record_id}: {item.reason}\n"
        for item in snapshot.exclusions
        if item.reason.startswith(_CORRUPTION_PREFIXES)
    )


def _patch(monkeypatch, snapshot=None, error=None):
    """One module-level read patch for this test. Restored when the test ends."""
    calls = {"defaults": 0, "reads": 0, "bootstrap": 0, "approve": 0}
    config = object()

    def defaults(cls):
        calls["defaults"] += 1
        return config

    def read(received):
        calls["reads"] += 1
        calls["received"] = received
        if error is not None:
            raise error
        return snapshot

    def bootstrap(*_args, **_kwargs):
        calls["bootstrap"] += 1

    def approve(*_args, **_kwargs):
        calls["approve"] += 1

    monkeypatch.setattr(OperationsConfig, "defaults", classmethod(defaults))
    monkeypatch.setattr(qualification_snapshot, "load_snapshot", read)
    monkeypatch.setattr("omp_work.operations.database.bootstrap", bootstrap)
    monkeypatch.setattr("omp_work.__main__._approve", approve)
    return calls, config


def _assert_idle(calls):
    assert calls["bootstrap"] == 0 and calls["approve"] == 0


def _forbid_report(monkeypatch):
    composed = []

    def wrapped(snapshot):
        composed.append(snapshot)
        raise AssertionError("report composed")

    monkeypatch.setattr(counter, "build_report", wrapped)
    return composed


def _observation(payload, payload_sha256):
    return {
        "observation_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "campaign_id": str(uuid4()),
        "trial_id": str(uuid4()),
        "issuer_kind": "legacy_autoresearch",
        "source_ref": "job:qualification-counter:test",
        "execution_status": "completed",
        "payload": payload,
        "payload_sha256": payload_sha256,
        "observed_at": "2026-10-13T00:00:00Z",
        "recorded_at": "2026-10-13T00:00:00Z",
    }


def test_count_empty_snapshot_exits_zero_after_one_read(monkeypatch, capsys):
    snapshot = Snapshot((), (), (), ())
    calls, config = _patch(monkeypatch, snapshot)
    real = counter.run_count

    def wrapped(received):
        calls["run_count"] = calls.get("run_count", 0) + 1
        assert received is config
        return real(received)

    monkeypatch.setattr(counter, "run_count", wrapped)
    assert main(["qualification", "count"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == _document(snapshot)
    report = json.loads(captured.out)
    assert report["grants_acceptance"] is False
    assert report["frozen"] is None and report["observations"] == []
    for gate in report["gates"].values():
        assert gate["status"] == "not started" and gate["count"] is None
    assert calls["defaults"] == 1 and calls["reads"] == 1 and calls["run_count"] == 1
    _assert_idle(calls)


def test_run_count_repeats_one_read_and_the_same_document(monkeypatch, capsys):
    snapshot = Snapshot((), (), (), ())
    calls, config = _patch(monkeypatch, snapshot)
    assert counter.run_count(config) == 0
    first = capsys.readouterr().out
    assert counter.run_count(config) == 0
    assert capsys.readouterr().out == first
    assert calls["reads"] == 2 and calls["defaults"] == 0
    _assert_idle(calls)


def test_count_legacy_missing_freeze_exits_zero(monkeypatch, capsys):
    snapshot = Snapshot(
        (Receipt("receipt-1", "h", "{}"),),
        (),
        ("accept-1",),
        (Exclusion("orphan", "O9 freeze and release baseline unavailable"),),
    )
    calls, _config = _patch(monkeypatch, snapshot)
    assert main(["qualification", "count"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == _document(snapshot)
    assert "O9 freeze and release baseline unavailable" in captured.out
    assert "invalid receipt:" not in captured.out
    report = json.loads(captured.out)
    assert report["grants_acceptance"] is False
    assert report["gates"]["consecutive"]["status"] == "not started"
    assert report["gates"]["consecutive"]["count"] is None
    assert calls["reads"] == 1 and calls["defaults"] == 1
    _assert_idle(calls)


def test_count_mixed_snapshot_exits_zero_after_one_read(monkeypatch, capsys):
    snapshot = mixed(monkeypatch)
    calls, _config = _patch(monkeypatch, snapshot)
    assert main(["qualification", "count"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == _document(snapshot)
    assert json.loads(captured.out)["grants_acceptance"] is False
    assert calls["reads"] == 1 and calls["defaults"] == 1
    _assert_idle(calls)


@pytest.mark.parametrize("argv", [
    [],
    ["qualification"],
    ["qualification", "trial"],
    ["qualification", "run"],
    ["qualification", "freeze"],
    ["qualification", "count", "--trial"],
    ["qualification", "count", "--run"],
    ["qualification", "count", "--freeze"],
    ["qualification", "count", "--write"],
    ["qualification", "count", "--approve"],
    ["qualification", "count", "now"],
    ["no-such-command"],
])
def test_missing_root_count_or_mutating_args_exit_2_before_config_or_read(argv, monkeypatch, capsys):
    calls, _config = _patch(monkeypatch, Snapshot((), (), (), ()))
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""
    assert calls["defaults"] == 0 and calls["reads"] == 0
    _assert_idle(calls)


def test_help_lists_qualification_count_without_reading(monkeypatch, capsys):
    calls, _config = _patch(monkeypatch, Snapshot((), (), (), ()))
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "qualification" in capsys.readouterr().out
    with pytest.raises(SystemExit) as excinfo:
        main(["qualification", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "{count}" in text
    assert "trial" not in text and "freeze" not in text
    with pytest.raises(SystemExit) as excinfo:
        main(["qualification", "count", "--help"])
    assert excinfo.value.code == 0
    assert "acceptance" in capsys.readouterr().out
    assert calls["defaults"] == 0 and calls["reads"] == 0
    _assert_idle(calls)


def _refuse(monkeypatch, snapshot, capsys):
    calls, _config = _patch(monkeypatch, snapshot)
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _corruption_text(snapshot)
    assert calls["reads"] == 1 and calls["defaults"] == 1
    assert composed == []
    _assert_idle(calls)
    return captured


def test_malformed_receipt_from_decoder_fails_without_stdout(monkeypatch, capsys):
    receipt_id = str(uuid4())
    decoded = decode_evidence(receipts=[{"receipt_id": receipt_id}])
    assert any(
        item.record_id == receipt_id and item.reason.startswith("invalid receipt:")
        for item in decoded.exclusions
    )
    snapshot = replace(
        decoded,
        exclusions=decoded.exclusions + (
            Exclusion("orphan", "O9 freeze and release baseline unavailable"),
        ),
    )
    captured = _refuse(monkeypatch, snapshot, capsys)
    assert receipt_id in captured.err
    assert "O9 freeze and release baseline unavailable" not in captured.err


def test_hash_mismatched_observation_from_decoder_fails_without_stdout(monkeypatch, capsys):
    row = _observation({"result": "negative science"}, "b" * 64)
    snapshot = decode_evidence(observations=[row])
    assert snapshot.observations == ()
    assert any(
        item.record_id == row["observation_id"]
        and item.reason.startswith("invalid observation:")
        and "payload hash mismatch" in item.reason
        for item in snapshot.exclusions
    )
    captured = _refuse(monkeypatch, snapshot, capsys)
    assert row["observation_id"] in captured.err
    assert "payload hash mismatch" in captured.err


def test_invalid_acceptance_evidence_from_decoder_fails_without_stdout(monkeypatch, capsys):
    snapshot = decode_evidence(completions=[("accept-1", {"result": "PASS"})])
    assert any(
        item.record_id == "accept-1" and item.reason.startswith("invalid acceptance evidence:")
        for item in snapshot.exclusions
    )
    captured = _refuse(monkeypatch, snapshot, capsys)
    assert "accept-1" in captured.err


def test_connection_failure_keeps_the_server_diagnostic(monkeypatch, capsys):
    message = (
        'connection failed: connection to server at "127.0.0.1", port 54321 failed: '
        'FATAL: password authentication failed for user "omp_work_readonly"'
    )
    error = psycopg.OperationalError(message)
    calls, _config = _patch(monkeypatch, error=error)
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{error}\n"
    assert calls["reads"] == 1 and calls["defaults"] == 1
    assert composed == []
    _assert_idle(calls)


def test_query_failure_keeps_the_server_diagnostic(monkeypatch, capsys):
    error = psycopg.errors.UndefinedTable(
        'relation "omp_work.qualification_missing_table" does not exist'
    )
    calls, _config = _patch(monkeypatch, error=error)
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{error}\n"
    assert calls["reads"] == 1 and calls["defaults"] == 1
    assert composed == []
    _assert_idle(calls)


def test_config_failure_exits_before_read(monkeypatch, capsys):
    calls, _config = _patch(monkeypatch, Snapshot((), (), (), ()))

    def defaults(cls):
        calls["defaults"] += 1
        raise ValueError("invalid literal for int() with base 10: 'nope'")

    monkeypatch.setattr(OperationsConfig, "defaults", classmethod(defaults))
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid literal for int() with base 10: 'nope'\n"
    assert calls["defaults"] == 1 and calls["reads"] == 0
    assert composed == []
    _assert_idle(calls)


def test_credential_validation_failure_during_read_has_no_fallback(monkeypatch, capsys):
    error = ValueError("missing credential file: /tmp/work-ledger/credentials/workspace-id")
    calls, _config = _patch(monkeypatch, error=error)
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{error}\n"
    assert calls["reads"] == 1 and calls["defaults"] == 1
    assert composed == []
    _assert_idle(calls)


def test_connection_error_strips_password_and_keeps_the_diagnostic(tmp_path, monkeypatch, capsys):
    secret = "s3cret-password-value"
    config = OperationsConfig(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    path = config.secret_path("omp_work_readonly")
    path.parent.mkdir(parents=True)
    path.write_text(secret + "\n")
    path.chmod(0o600)
    calls = {"reads": 0, "defaults": 0}

    def defaults(cls):
        calls["defaults"] += 1
        return config

    def read(received):
        calls["reads"] += 1
        assert received is config
        raise psycopg.OperationalError(
            f"connection failed token {secret} at host=127.0.0.1 password={secret} "
            f"url=postgresql://omp_work_readonly:{secret}@127.0.0.1/omp_work"
        )

    monkeypatch.setattr(OperationsConfig, "defaults", classmethod(defaults))
    monkeypatch.setattr(qualification_snapshot, "load_snapshot", read)
    composed = _forbid_report(monkeypatch)
    assert main(["qualification", "count"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert "connection failed token [redacted] at host=127.0.0.1" in captured.err
    assert "password=[redacted]" in captured.err
    assert "postgresql://omp_work_readonly:[redacted]@127.0.0.1/omp_work" in captured.err
    assert calls == {"reads": 1, "defaults": 1}
    assert composed == []
