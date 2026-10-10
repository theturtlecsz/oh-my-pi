"""Tests for Harbor trial report ingestion (report.py)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from omp_harbor_eval import EvidenceWriter, load_fixture
from omp_harbor_eval.report import ingest, main, summarize


def _write_fixture(root: Path, fixture_id: str = "f1", *, rules: list[dict] | None = None) -> object:
    directory = root / fixture_id
    directory.mkdir(parents=True, exist_ok=True)
    fixture = {
        "id": fixture_id,
        "scored_experiment": "test-experiment",
        "seed_patch": "",
        "solution_patch": "",
        "independent_tests": [],
        "scenario": "scenario.json",
        "rules": [] if rules is None else rules,
    }
    scenario = {
        "command": "execute",
        "terminal": {"pointer": "/workflow/status", "in": ["completed"]},
        "model_script": [],
        "ui_script": [],
        "kill_at": None,
        "timeout_s": 30,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    (directory / "scenario.json").write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    return load_fixture(root, fixture_id)


def _write_base_evidence(
    directory: Path,
    fixture,
    *,
    run_id: str = "run-1",
    nonce: str = "nonce-1",
    arm_doc: dict | None = None,
    rpc_rows: list[dict] | None = None,
    ui_answers: list[dict] | None = None,
    outcome: str = "completed",
    raw_arm_json: str | None = None,
) -> str:
    writer = EvidenceWriter(
        directory,
        run_id,
        nonce,
        fixture.id,
        fixture.digest,
        "known_good",
        fixture.scored_experiment,
    )
    writer.write_json("outcome.json", {"outcome": outcome})

    if raw_arm_json is not None:
        (directory / "arm.json").write_text(raw_arm_json, encoding="utf-8")
    elif arm_doc is not None:
        writer.write_json("arm.json", arm_doc)

    if rpc_rows is not None:
        for row in rpc_rows:
            writer.append_jsonl("rpc-transcript.jsonl", row)

    if ui_answers is not None:
        for ans in ui_answers:
            writer.append_jsonl("ui-answers.jsonl", ans)

    return writer.seal()


@pytest.fixture
def sample_catalog(tmp_path: Path) -> Path:
    cat = {
        "test-provider": {
            "exact-model": {
                "cost": {
                    "input": 2.0,
                    "output": 10.0,
                    "cacheRead": 0.5,
                }
            },
            "lc-model": {
                "cost": {
                    "input": 2.0,
                    "output": 10.0,
                    "longContext": {
                        "input": 4.0,
                        "output": 20.0,
                    },
                }
            },
        }
    }
    cat_file = tmp_path / "models.json"
    cat_file.write_text(json.dumps(cat, indent=2), encoding="utf-8")
    return cat_file


def test_line_has_repetition_and_attempt(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence"
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "plan-1", "arm": "arm-A"},
    )
    out_file = tmp_path / "records.jsonl"

    record = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=3,
        repetition=2,
        catalog=sample_catalog,
        out=out_file,
    )

    assert record["repetition"] == 2
    assert record["attempt"] == 3
    assert record["experiment"] == "test-exp"
    assert record["model"] == "test-provider/exact-model"
    assert record["plan_id"] == "plan-1"
    assert record["arm"] == "arm-A"
    assert record["task_id"] == fixture.id

    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["repetition"] == 2
    assert lines[0]["attempt"] == 3


def test_missing_or_incomplete_arm_json(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")

    # Case 1: arm.json is completely absent
    ev1 = tmp_path / "evidence_no_arm"
    sha1 = _write_base_evidence(ev1, fixture, arm_doc=None)
    out1 = tmp_path / "out1.jsonl"
    rec1 = ingest(
        ev1,
        sha1,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out1,
    )
    assert rec1["status"] == "invalid_evidence"
    assert rec1["plan_id"] is None
    assert rec1["arm"] is None

    # Case 2: arm.json is malformed JSON
    ev2 = tmp_path / "evidence_bad_arm"
    sha2 = _write_base_evidence(ev2, fixture, raw_arm_json="not-json{{{")
    out2 = tmp_path / "out2.jsonl"
    rec2 = ingest(
        ev2,
        sha2,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out2,
    )
    assert rec2["status"] == "invalid_evidence"
    assert rec2["plan_id"] is None
    assert rec2["arm"] is None

    # Case 3: arm.json missing plan_id
    ev3 = tmp_path / "evidence_missing_plan_id"
    sha3 = _write_base_evidence(ev3, fixture, arm_doc={"arm": "A"})
    out3 = tmp_path / "out3.jsonl"
    rec3 = ingest(
        ev3,
        sha3,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out3,
    )
    assert rec3["status"] == "invalid_evidence"
    assert rec3["plan_id"] is None
    assert rec3["arm"] is None

    # Case 4: arm.json missing arm
    ev4 = tmp_path / "evidence_missing_arm"
    sha4 = _write_base_evidence(ev4, fixture, arm_doc={"plan_id": "p1"})
    out4 = tmp_path / "out4.jsonl"
    rec4 = ingest(
        ev4,
        sha4,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out4,
    )
    assert rec4["status"] == "invalid_evidence"
    assert rec4["plan_id"] is None
    assert rec4["arm"] is None

    # Case 5: arm.json with empty / null fields
    ev5 = tmp_path / "evidence_null_fields"
    sha5 = _write_base_evidence(ev5, fixture, arm_doc={"plan_id": None, "arm": ""})
    out5 = tmp_path / "out5.jsonl"
    rec5 = ingest(
        ev5,
        sha5,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out5,
    )
    assert rec5["status"] == "invalid_evidence"
    assert rec5["plan_id"] is None
    assert rec5["arm"] is None


def test_two_agent_end_frames_uses_last_only(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_two_ends"
    rpc_rows = [
        {"direction": "in", "frame": {"type": "agent_start"}},
        {
            "direction": "in",
            "frame": {
                "type": "agent_end",
                "telemetry": {"usage": {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 50}},
            },
        },
        {
            "direction": "in",
            "frame": {
                "type": "agent_end",
                "telemetry": {"usage": {"inputTokens": 1000, "cachedInputTokens": 400, "outputTokens": 200}},
            },
        },
    ]
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
        rpc_rows=rpc_rows,
    )
    out = tmp_path / "out.jsonl"

    record = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out,
    )

    assert record["tokens"] == {"input": 1000, "cached": 400, "output": 200}
    # Hand calculation: (600 * 2.0 + 400 * 0.5 + 200 * 10.0) / 1_000_000 = 0.0034
    assert record["cost_usd"] == "0.0034"


def test_no_telemetry_usage_gives_null_tokens_and_cost(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_no_telemetry"
    rpc_rows = [
        {"direction": "in", "frame": {"type": "agent_end"}},
    ]
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
        rpc_rows=rpc_rows,
    )
    out = tmp_path / "out.jsonl"

    record = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out,
    )

    assert record["tokens"] is None
    assert record["cost_usd"] is None
    assert record["cost_basis"] == "exact"


def test_temp_catalog_pricing_exact_and_upper_bound(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_pricing"
    rpc_rows = [
        {
            "direction": "in",
            "frame": {
                "type": "agent_end",
                "telemetry": {"usage": {"inputTokens": 1000, "cachedInputTokens": 400, "outputTokens": 200}},
            },
        },
    ]
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
        rpc_rows=rpc_rows,
    )

    # 1. Exact model with cacheRead
    out1 = tmp_path / "out_exact.jsonl"
    rec1 = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out1,
    )
    assert rec1["cost_basis"] == "exact"
    # Hand calculation:
    # (1000 - 400) * 2.0 = 1200
    # 400 * 0.5 = 200
    # 200 * 10.0 = 2000
    # total = 3400 / 1_000_000 = 0.0034
    assert rec1["cost_usd"] == "0.0034"

    # 2. Model with longContext tier -> cost_basis upper_bound
    out2 = tmp_path / "out_lc.jsonl"
    rec2 = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/lc-model",
        attempt=1,
        catalog=sample_catalog,
        out=out2,
    )
    assert rec2["cost_basis"] == "upper_bound"


def test_unpriced_model_exits_1_and_appends_no_line(
    tmp_path: Path, sample_catalog: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_unpriced"
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
    )
    out_file = tmp_path / "out_unpriced.jsonl"

    # 1. Direct function call raises SystemExit(1)
    with pytest.raises(SystemExit) as exc_info:
        ingest(
            evidence_dir,
            sha,
            fixture,
            run_id="run-1",
            nonce="nonce-1",
            experiment="test-exp",
            model="unknown-provider/nonexistent-model",
            attempt=1,
            catalog=sample_catalog,
            out=out_file,
        )
    assert exc_info.value.code == 1
    assert not out_file.exists()

    # 2. CLI main returns 1
    rc = main([
        "ingest",
        "--evidence",
        str(evidence_dir),
        "--manifest-sha256",
        sha,
        "--fixture",
        str(fixture.directory),
        "--run-id",
        "run-1",
        "--nonce",
        "nonce-1",
        "--experiment",
        "test-exp",
        "--model",
        "unknown-provider/nonexistent-model",
        "--attempt",
        "1",
        "--catalog",
        str(sample_catalog),
        "--out",
        str(out_file),
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unpriced or unknown model" in captured.err
    assert not out_file.exists()


def test_turns_interventions_and_latency(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_metrics"
    rpc_rows = [
        {"direction": "out", "frame": {"type": "turn_start"}},  # Outbound ignored
        {"direction": "in", "frame": {"type": "turn_start"}},
        {"direction": "in", "frame": {"type": "turn_start"}},
        {
            "direction": "in",
            "frame": {
                "type": "message_start",
                "message": {"timestamp": 10000},
            },
        },
        {
            "direction": "out",
            "frame": {
                "type": "message_end",
                "message": {"timestamp": 999999},  # Outbound ignored
            },
        },
        {
            "direction": "in",
            "frame": {
                "type": "message_end",
                "message": {"timestamp": 15500},
            },
        },
    ]
    ui_answers = [{"confirmed": True}, {"confirmed": False}]

    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
        rpc_rows=rpc_rows,
        ui_answers=ui_answers,
    )
    out_file = tmp_path / "out_metrics.jsonl"

    record = ingest(
        evidence_dir,
        sha,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=out_file,
    )

    assert record["turns"] == 2
    assert record["interventions"] == 2
    # Latency: (15500 - 10000) / 1000.0 = 5.5s
    assert record["latency_s"] == 5.5


def test_latency_under_two_seconds_or_fewer_than_two_timestamps_is_null(
    tmp_path: Path, sample_catalog: Path
) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")

    # Case 1: span < 2 seconds (e.g. 1.5s)
    ev1 = tmp_path / "ev_sub2"
    rpc_sub2 = [
        {"direction": "in", "frame": {"type": "message_start", "message": {"timestamp": 10000}}},
        {"direction": "in", "frame": {"type": "message_end", "message": {"timestamp": 11500}}},
    ]
    sha1 = _write_base_evidence(ev1, fixture, arm_doc={"plan_id": "p1", "arm": "A"}, rpc_rows=rpc_sub2)
    rec1 = ingest(
        ev1,
        sha1,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=tmp_path / "out1.jsonl",
    )
    assert rec1["latency_s"] is None

    # Case 2: only 1 timestamp
    ev2 = tmp_path / "ev_single_ts"
    rpc_single = [
        {"direction": "in", "frame": {"type": "message_start", "message": {"timestamp": 10000}}},
    ]
    sha2 = _write_base_evidence(ev2, fixture, arm_doc={"plan_id": "p1", "arm": "A"}, rpc_rows=rpc_single)
    rec2 = ingest(
        ev2,
        sha2,
        fixture,
        run_id="run-1",
        nonce="nonce-1",
        experiment="test-exp",
        model="test-provider/exact-model",
        attempt=1,
        catalog=sample_catalog,
        out=tmp_path / "out2.jsonl",
    )
    assert rec2["latency_s"] is None


def test_cli_ingest_appends_to_existing_file(tmp_path: Path, sample_catalog: Path) -> None:
    fixture = _write_fixture(tmp_path / "fixtures")
    evidence_dir = tmp_path / "evidence_cli"
    sha = _write_base_evidence(
        evidence_dir,
        fixture,
        arm_doc={"plan_id": "p1", "arm": "A"},
    )
    out_file = tmp_path / "multi_records.jsonl"

    rc1 = main([
        "ingest",
        "--evidence",
        str(evidence_dir),
        "--manifest-sha256",
        sha,
        "--fixture",
        str(fixture.directory),
        "--run-id",
        "run-1",
        "--nonce",
        "nonce-1",
        "--experiment",
        "test-exp",
        "--model",
        "test-provider/exact-model",
        "--attempt",
        "1",
        "--repetition",
        "1",
        "--catalog",
        str(sample_catalog),
        "--out",
        str(out_file),
    ])
    assert rc1 == 0

    rc2 = main([
        "ingest",
        "--evidence",
        str(evidence_dir),
        "--manifest-sha256",
        sha,
        "--fixture",
        str(fixture.directory),
        "--run-id",
        "run-1",
        "--nonce",
        "nonce-1",
        "--experiment",
        "test-exp",
        "--model",
        "test-provider/exact-model",
        "--attempt",
        "2",
        "--repetition",
        "1",
        "--catalog",
        str(sample_catalog),
        "--out",
        str(out_file),
    ])
    assert rc2 == 0

    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert lines[0]["attempt"] == 1
    assert lines[1]["attempt"] == 2


def _record(**overrides: object) -> dict:
    base: dict = {
        "experiment": "exp",
        "plan_id": "plan-1",
        "arm": "B",
        "model": "provider/model",
        "task_id": "task-1",
        "repetition": 1,
        "attempt": 1,
        "status": "pass",
        "tokens": {"input": 100, "cached": 0, "output": 10},
        "cost_usd": "1.25",
        "cost_basis": "exact",
        "turns": 2,
        "interventions": 1,
        "latency_s": 60,
    }
    base.update(overrides)
    return base


def _write_records(path: Path, records: list[dict]) -> None:
    lines = [json.dumps(row) for row in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _run_summary(tmp_path: Path, records: list[dict]) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "records.jsonl"
    _write_records(source, records)
    return summarize(
        source,
        out_json=tmp_path / "summary.json",
        out_md=tmp_path / "summary.md",
    )


def _markdown_rows(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def test_summarize_retry_keeps_repetition_first_outcome(tmp_path: Path) -> None:
    # One task, two repetitions. Repetition 2 fails, then passes on the retry.
    # The retry does not change the first outcome and does not add a repetition.
    payload = _run_summary(
        tmp_path,
        [
            _record(repetition=1, attempt=1, status="pass", cost_usd="1.25", latency_s=60, turns=2),
            _record(
                repetition=2,
                attempt=1,
                status="fail",
                tokens={"input": 50, "cached": 5, "output": 20},
                cost_usd="2.50",
                latency_s=60,
                turns=3,
                interventions=None,
            ),
            _record(
                repetition=2,
                attempt=2,
                status="pass",
                tokens=None,
                cost_usd="9.00",
                latency_s=999,
                turns=9,
                interventions=4,
            ),
        ],
    )
    row = payload["groups"][0]
    assert row["repetitions"] == 2
    assert row["records"] == 3
    assert row["undecided"] == 0
    assert row["repetitions_first_pass"] == 1
    assert row["repetitions_first_decidable"] == 2
    assert row["first_pass_rate"] == 0.5
    assert row["repetitions_final_pass"] == 2
    assert row["repetitions_final_decidable"] == 2
    assert row["ac_success"] == 1.0
    assert row["wilson_low"] == pytest.approx(0.3423802239546494)
    assert row["wilson_high"] == pytest.approx(1.0)
    assert row["status_counts"] == {
        "pass": 2,
        "fail": 1,
        "harness_defect": 0,
        "invalid_evidence": 0,
    }
    # Same task: one first-fail makes the task first_pass 0. Final stays 1.
    assert row["tasks"] == [
        {"task_id": "task-1", "repetitions": 2, "first_pass": 0, "final_pass": 1}
    ]
    assert row["task_rates"]["first"] == {"counted": 1, "passing": 0, "rate": 0.0}
    assert row["task_rates"]["final"] == {"counted": 1, "passing": 1, "rate": 1.0}
    # The null-token retry is unknown and is left out of the sums.
    assert row["unknown"] == 1
    assert row["sums"]["input"] == 150
    assert row["sums"]["cached"] == 5
    assert row["sums"]["output"] == 30
    assert row["sums"]["cost_usd"] == "3.75"
    assert row["sums"]["turns"] == 5
    assert row["sums"]["interventions"] == 1
    assert row["sums"]["latency_s"] == 120
    assert row["cost_per_success"] == "1.875"
    assert row["success_per_minute"] == 1.0
    assert payload["totals"]["ac_success"] == 1.0
    assert payload["totals"]["first_pass_rate"] == 0.5

    written = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert written == payload
    markdown = _markdown_rows((tmp_path / "summary.md").read_text(encoding="utf-8"))
    assert markdown == [
        {
            "experiment": "exp",
            "arm": "B",
            "model": "provider/model",
            "repetitions": "2",
            "undecided": "0",
            "repetitions_final_pass": "2",
            "ac_success": "1",
            "first_pass_rate": "0.5",
            "cost_per_success": "1.875",
            "success_per_minute": "1",
        }
    ]


def test_summarize_two_attempt_one_records_are_two_repetitions(tmp_path: Path) -> None:
    same_task = _run_summary(
        tmp_path / "same",
        [
            _record(task_id="task-1", repetition=1, attempt=1, status="pass"),
            _record(task_id="task-1", repetition=2, attempt=1, status="fail"),
        ],
    )
    same = same_task["groups"][0]
    assert same["repetitions"] == 2
    assert same["ac_success"] == 0.5
    assert same["first_pass_rate"] == 0.5
    assert same["wilson_low"] == pytest.approx(0.09453120463920084)
    assert same["wilson_high"] == pytest.approx(0.9054687953607992)

    two_tasks = _run_summary(
        tmp_path / "tasks",
        [
            _record(task_id="task-1", repetition=1, attempt=1, status="pass"),
            _record(task_id="task-2", repetition=1, attempt=1, status="fail"),
        ],
    )
    split = two_tasks["groups"][0]
    assert split["repetitions"] == 2
    assert split["ac_success"] == 0.5
    assert split["first_pass_rate"] == 0.5
    assert split["task_rates"]["first"] == {"counted": 2, "passing": 1, "rate": 0.5}
    assert split["task_rates"]["final"] == {"counted": 2, "passing": 1, "rate": 0.5}


def test_summarize_non_decidable_attempts_do_not_set_the_outcome(tmp_path: Path) -> None:
    # Attempt 1 harness_defect, attempt 2 pass: first is null, final is 1.
    defect_then_pass = _run_summary(
        tmp_path / "defect",
        [
            _record(attempt=1, status="harness_defect"),
            _record(attempt=2, status="pass"),
        ],
    )["groups"][0]
    assert defect_then_pass["first_pass_rate"] is None
    assert defect_then_pass["repetitions_first_decidable"] == 0
    assert defect_then_pass["ac_success"] == 1.0
    assert defect_then_pass["undecided"] == 0
    assert defect_then_pass["tasks"][0]["first_pass"] is None
    assert defect_then_pass["tasks"][0]["final_pass"] == 1
    assert defect_then_pass["status_counts"]["harness_defect"] == 1
    assert defect_then_pass["status_counts"]["pass"] == 1

    # Attempt 1 fail, attempt 2 invalid_evidence: the fail still decides.
    fail_then_invalid = _run_summary(
        tmp_path / "invalid",
        [
            _record(attempt=1, status="fail"),
            _record(attempt=2, status="invalid_evidence"),
        ],
    )["groups"][0]
    assert fail_then_invalid["first_pass_rate"] == 0.0
    assert fail_then_invalid["ac_success"] == 0.0
    assert fail_then_invalid["undecided"] == 0
    assert fail_then_invalid["repetitions"] == 1
    assert fail_then_invalid["repetitions_final_decidable"] == 1
    assert fail_then_invalid["status_counts"]["invalid_evidence"] == 1

    # No attempt 1: first stays null. Final uses the decidable attempts.
    no_first = _run_summary(
        tmp_path / "nofirst",
        [
            _record(attempt=2, status="fail"),
            _record(attempt=3, status="pass"),
        ],
    )["groups"][0]
    assert no_first["first_pass_rate"] is None
    assert no_first["ac_success"] == 1.0
    assert no_first["tasks"][0]["first_pass"] is None
    assert no_first["tasks"][0]["final_pass"] == 1

    # A later defect does not replace an earlier fail. Pass then fail ends at 0.
    later_defect = _run_summary(
        tmp_path / "later",
        [
            _record(attempt=1, status="fail"),
            _record(attempt=2, status="pass"),
            _record(attempt=3, status="harness_defect"),
        ],
    )["groups"][0]
    assert later_defect["first_pass_rate"] == 0.0
    assert later_defect["ac_success"] == 1.0
    assert later_defect["repetitions"] == 1

    pass_then_fail = _run_summary(
        tmp_path / "down",
        [
            _record(attempt=1, status="pass"),
            _record(attempt=2, status="fail"),
        ],
    )["groups"][0]
    assert pass_then_fail["first_pass_rate"] == 1.0
    assert pass_then_fail["ac_success"] == 0.0


def test_summarize_defect_and_invalid_stay_out_of_ac_success(tmp_path: Path) -> None:
    row = _run_summary(
        tmp_path,
        [
            _record(task_id="only-defect", repetition=1, attempt=1, status="harness_defect"),
            _record(task_id="only-invalid", repetition=1, attempt=1, status="invalid_evidence"),
            _record(task_id="ok", repetition=1, attempt=1, status="pass"),
        ],
    )["groups"][0]
    assert row["repetitions"] == 3
    assert row["undecided"] == 2
    assert row["repetitions_final_pass"] == 1
    assert row["repetitions_final_decidable"] == 1
    assert row["ac_success"] == 1.0
    assert row["status_counts"]["harness_defect"] == 1
    assert row["status_counts"]["invalid_evidence"] == 1
    assert row["status_counts"]["pass"] == 1
    assert row["tasks"] == [
        {"task_id": "ok", "repetitions": 1, "first_pass": 1, "final_pass": 1},
        {"task_id": "only-defect", "repetitions": 1, "first_pass": None, "final_pass": None},
        {"task_id": "only-invalid", "repetitions": 1, "first_pass": None, "final_pass": None},
    ]
    assert row["task_rates"]["final"] == {"counted": 1, "passing": 1, "rate": 1.0}


def test_summarize_null_tokens_make_cost_rates_null(tmp_path: Path) -> None:
    all_null = _run_summary(
        tmp_path / "all",
        [
            _record(status="pass", tokens=None, cost_usd="4.00", latency_s=30, turns=4),
            _record(
                repetition=2,
                status="fail",
                tokens=None,
                cost_usd="1.00",
                latency_s=30,
                turns=1,
            ),
        ],
    )["groups"][0]
    assert all_null["unknown"] == 2
    assert all_null["ac_success"] == 0.5
    assert all_null["sums"]["input"] is None
    assert all_null["sums"]["cost_usd"] is None
    assert all_null["sums"]["latency_s"] is None
    assert all_null["sums"]["turns"] is None
    assert all_null["cost_per_success"] is None
    assert all_null["success_per_minute"] is None

    # Tokens present but no input value: the summed input is null, so both rates are null.
    missing_input = _run_summary(
        tmp_path / "partial",
        [
            _record(
                status="pass",
                tokens={"cached": 1},
                cost_usd="1.00",
                latency_s=60,
            )
        ],
    )["groups"][0]
    assert missing_input["unknown"] == 0
    assert missing_input["sums"]["input"] is None
    assert missing_input["sums"]["cached"] == 1
    assert missing_input["sums"]["cost_usd"] == "1"
    assert missing_input["cost_per_success"] is None
    assert missing_input["success_per_minute"] is None


def test_summarize_rates_null_when_a_divisor_is_zero(tmp_path: Path) -> None:
    no_success = _run_summary(
        tmp_path / "fail",
        [_record(status="fail", cost_usd="2.00", latency_s=30, tokens={"input": 10, "cached": 0, "output": 1})],
    )["groups"][0]
    assert no_success["ac_success"] == 0.0
    assert no_success["cost_per_success"] is None
    assert no_success["success_per_minute"] == 0.0
    assert no_success["wilson_low"] == pytest.approx(0.0)
    assert no_success["wilson_high"] == pytest.approx(0.7934506882081973)

    no_latency = _run_summary(
        tmp_path / "nolat",
        [
            _record(
                status="pass",
                latency_s=None,
                cost_usd="2.00",
                tokens={"input": 10, "cached": 0, "output": 1},
            )
        ],
    )["groups"][0]
    assert no_latency["cost_per_success"] == "2"
    assert no_latency["success_per_minute"] is None

    zero_latency = _run_summary(
        tmp_path / "zero",
        [
            _record(
                status="pass",
                latency_s=0,
                cost_usd="2.00",
                tokens={"input": 10, "cached": 0, "output": 1},
            )
        ],
    )["groups"][0]
    assert zero_latency["success_per_minute"] is None


def test_summarize_duplicate_record_exits_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "records.jsonl"
    _write_records(
        source,
        [
            _record(attempt=1, status="pass"),
            _record(attempt=1, status="fail"),
        ],
    )
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    out_json.write_text("keep\n", encoding="utf-8")
    out_md.write_text("keep\n", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        summarize(source, out_json=out_json, out_md=out_md)
    assert raised.value.code == 1
    assert out_json.read_text(encoding="utf-8") == "keep\n"
    assert out_md.read_text(encoding="utf-8") == "keep\n"
    assert "duplicate record" in capsys.readouterr().err

    rc = main(
        [
            "summarize",
            "--records",
            str(source),
            "--out-json",
            str(tmp_path / "cli.json"),
            "--out-md",
            str(tmp_path / "cli.md"),
        ]
    )
    assert rc == 1
    assert not (tmp_path / "cli.json").exists()
    assert not (tmp_path / "cli.md").exists()
    assert "duplicate record" in capsys.readouterr().err


def test_summarize_sorts_groups_and_pools_totals(tmp_path: Path) -> None:
    payload = _run_summary(
        tmp_path,
        [
            _record(experiment="b", arm="A", model="m", task_id="t", status="pass"),
            _record(experiment="a", arm="B", model="m", task_id="t", status="fail"),
            _record(experiment="a", arm=None, model="m", task_id="t", status="pass"),
            _record(experiment="a", arm="A", model="z", task_id="t", status="fail"),
            _record(experiment="a", arm="A", model="m", task_id="t1", status="pass"),
            _record(experiment="a", arm="A", model="m", task_id="t2", repetition=1, status="fail"),
            _record(experiment="a", arm="A", model="m", task_id="t2", repetition=2, status="fail"),
        ],
    )
    identities = [(row["experiment"], row["arm"], row["model"]) for row in payload["groups"]]
    assert identities == [
        ("a", None, "m"),
        ("a", "A", "m"),
        ("a", "A", "z"),
        ("a", "B", "m"),
        ("b", "A", "m"),
    ]
    # Arm A / model m is 1 pass and 2 fails. The five groups pool to 3 passes of 7.
    arm = payload["groups"][1]
    assert arm["repetitions"] == 3
    assert arm["repetitions_final_pass"] == 1
    assert arm["ac_success"] == pytest.approx(1 / 3)
    totals = payload["totals"]
    assert totals["repetitions"] == 7
    assert totals["repetitions_final_pass"] == 3
    assert totals["repetitions_final_decidable"] == 7
    assert totals["ac_success"] == pytest.approx(3 / 7)
    assert totals["wilson_low"] == pytest.approx(0.15821985399244537)
    assert totals["wilson_high"] == pytest.approx(0.7495416372469643)
    assert 0.0 <= totals["wilson_low"] <= totals["ac_success"] <= totals["wilson_high"] <= 1.0
    assert totals["first_pass_rate"] == pytest.approx(3 / 7)
    assert totals["unknown"] == 0
    assert [
        task["task_id"]
        for task in totals["tasks"]
        if task["experiment"] == "a" and task["arm"] == "A" and task["model"] == "m"
    ] == ["t1", "t2"]

    rc = main(
        [
            "summarize",
            "--records",
            str(tmp_path / "records.jsonl"),
            "--out-json",
            str(tmp_path / "cli.json"),
            "--out-md",
            str(tmp_path / "cli.md"),
        ]
    )
    assert rc == 0
    cli = json.loads((tmp_path / "cli.json").read_text(encoding="utf-8"))
    assert [(row["experiment"], row["arm"], row["model"]) for row in cli["groups"]] == identities
    markdown = _markdown_rows((tmp_path / "cli.md").read_text(encoding="utf-8"))
    assert [row["experiment"] + "/" + row["arm"] + "/" + row["model"] for row in markdown] == [
        "a//m",
        "a/A/m",
        "a/A/z",
        "a/B/m",
        "b/A/m",
    ]
    assert markdown[1]["ac_success"].startswith("0.333333")
    assert markdown[0]["cost_per_success"] != ""
