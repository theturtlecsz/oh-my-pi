"""Tests for Harbor trial report ingestion (report.py)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from omp_harbor_eval import EvidenceWriter, load_fixture
from omp_harbor_eval.report import ingest, main


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
