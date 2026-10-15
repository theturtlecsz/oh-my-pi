"""OMP-615-s09-s04: the ``omp-work offerings check`` command and its immutable records.

Failure mode defended: the daily offerings check must append exactly one
immutable evaluation record per run and act on nothing else. If it wrote outside
its record directory, resumed a malformed history instead of refusing, let a
current gap erase the last successful snapshot, reused a record name so a later
run overwrote an earlier one, or mislabelled a retiring in-use model as a weekly
queue item, the owner's review schedule would silently lose exactly the findings
it exists to surface. These tests drive the real CLI with real capture files and
a real stub ``omp``, and hash the policy, the whole routing directory and every
capture before and after the urgent and gap runs.
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.__main__ import main
from omp_work.routing.offerings_evaluation import (
    EVALUATION_VERSION,
    OWNER_REPORT_VERSION,
    STATUS_CHECK_GAP,
    STATUS_PENDING_QUALIFICATION,
    STATUS_URGENT_REVIEW,
    discover_previous,
    policy_path,
    run_offerings_check,
)
from omp_work.routing.policy import load_policy

SCHEDULE_KEYS = ("daily_check", "weekly_qualification", "urgent_review")

# Every provider the governing policy maps to a recipe needs a lane, or the
# comparison reports an unmapped provider. Lane names equal their catalog
# provider here, and the lane's in-use model is the one the policy routes to it.
LANE_KINDS = {
    "gemini_flash": "omp-cli",
    "openai_codex": "omp-cli",
    "anthropic_fable": "listing-file",
    "chatgpt_astra": "listing-file",
    "kimi": "listing-file",
    "kimi-code": "listing-file",
    "ollama_cloud": "listing-file",
    "openai-codex": "listing-file",
}
IN_USE = {
    "gemini_flash": "gemini-3.8-flash-high",
    "openai_codex": "gpt-5.6-sol",
    "anthropic_fable": "claude-opus-5-5",
    "chatgpt_astra": "gpt-6-sol",
    "kimi": "kimi-code/k3-256k",
    "kimi-code": "k3-256k",
    "ollama_cloud": "deepseek-v4.1-flash:cloud",
    "openai-codex": "gpt-5.6-sol:medium",
}
COST = {"input": 1, "output": 2}

OMP_STUB = """\
#!/usr/bin/env python3
import json, os, sys

docs = json.loads(os.environ["OMP_STUB_LISTINGS"])
sys.stdout.write(docs.get(sys.argv[3], ""))
raise SystemExit(0)
"""


class Captures:
    """A materialized capture set: the sources file and its lane files."""

    def __init__(self, sources: Path, cli_docs: dict[str, str]) -> None:
        self.sources = sources
        self.cli_docs = cli_docs


def _row(provider: str, model_id: str, cost: dict) -> dict:
    return {
        "provider": provider,
        "id": model_id,
        "name": model_id,
        "contextWindow": 1000,
        "maxTokens": 100,
        "reasoning": True,
        "thinking": ["low"],
        "input": ["text"],
        "cost": cost,
    }


def _file_doc(provider: str, ids: list[str], cost: dict, retrieved: datetime) -> str:
    return json.dumps({
        "models": [_row(provider, model_id, cost) for model_id in ids],
        "retrieval": {
            "provider": provider,
            "source": "https://capture.example/models",
            "retrieved_at": retrieved.isoformat(),
            "success": True,
        },
    })


def _cli_doc(provider: str, ids: list[str], cost: dict, retrieved: datetime) -> str:
    return json.dumps({
        "models": [_row(provider, model_id, cost) for model_id in ids],
        "discovery": {
            provider: {
                "provider": provider,
                "status": "ok",
                "stale": False,
                "source": "provider",
                "fetchedAt": int(retrieved.timestamp() * 1000),
            }
        },
    })


def _dimension(source: str, retrieved: datetime, **body: object) -> dict:
    return {
        "coverage": "checked",
        "source": source,
        "retrieved_at": retrieved.isoformat(),
        **body,
    }


def _notices(retrieved: datetime) -> str:
    providers = {
        lane: {
            "notices": _dimension("https://capture.example/notices", retrieved, items=[]),
            "status": _dimension("https://capture.example/status", retrieved, value="operational"),
        }
        for lane in LANE_KINDS
    }
    return json.dumps({"version": "offerings-notices.v1", "providers": providers})


def write_captures(
    root: Path,
    *,
    checked_at: datetime,
    overrides: dict[str, object] | None = None,
) -> Captures:
    """Materialize a full capture set whose every lane covers its in-use model.

    ``overrides`` maps a lane to ``None`` (make it unavailable), a list of
    replacement model ids, or ``{"ids": [...], "cost": {...}}``.
    """
    root.mkdir(parents=True, exist_ok=True)
    retrieved = checked_at - timedelta(hours=1)
    notices_path = root / "offerings-notices.json"
    notices_path.write_text(_notices(retrieved), encoding="utf-8")
    cli_docs: dict[str, str] = {}
    lanes: dict[str, dict] = {}
    for lane, kind in LANE_KINDS.items():
        override = (overrides or {}).get(lane, [IN_USE[lane]])
        if override is None:
            ids, cost = [IN_USE[lane]], COST
        elif isinstance(override, dict):
            ids, cost = list(override["ids"]), dict(override.get("cost", COST))
        else:
            ids, cost = list(override), COST
        if kind == "omp-cli":
            cli_docs[lane] = "" if override is None else _cli_doc(lane, ids, cost, retrieved)
            lanes[lane] = {"kind": kind, "catalog_provider": lane}
        else:
            path = root / f"listing-{lane}.json"
            if override is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(_file_doc(lane, ids, cost, retrieved), encoding="utf-8")
            lanes[lane] = {"kind": kind, "catalog_provider": lane, "path": str(path)}
    sources = root / "offerings-sources.json"
    sources.write_text(
        json.dumps({
            "version": "offerings-sources.v1",
            "notices": str(notices_path),
            "lanes": lanes,
        }),
        encoding="utf-8",
    )
    return Captures(sources, cli_docs)


def install_stub(monkeypatch: pytest.MonkeyPatch, root: Path, captures: Captures) -> str:
    """Write a real ``omp`` executable that prints the captures' CLI listings."""
    stub = root / "omp-stub"
    stub.write_text(OMP_STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("OMP_STUB_LISTINGS", json.dumps(captures.cli_docs))
    return str(stub)


def evaluation_paths(record_dir: Path) -> list[Path]:
    return sorted(record_dir.glob("evaluation-*.json"))


def records_of(record_dir: Path) -> dict[str, dict]:
    """Parsed evaluation records only; owner reports share the directory."""
    return {path.name: json.loads(path.read_text()) for path in evaluation_paths(record_dir)}


def filenames(record_dir: Path) -> set[str]:
    """Every record-dir entry name, without parsing, so a malformed one is inert."""
    return {path.name for path in record_dir.glob("*.json")}


def tree_hashes(root: Path) -> dict[str, str]:
    """Byte digest of every file under ``root``; interpreter bytecode is not data."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def read_only_state(captures: Captures) -> dict[str, dict[str, str]]:
    return {
        "policy": {str(policy_path()): hashlib.sha256(policy_path().read_bytes()).hexdigest()},
        "routing": tree_hashes(policy_path().parent),
        "captures": tree_hashes(captures.sources.parent),
    }


def stem_of(evaluation_name: str) -> str:
    return evaluation_name[len("evaluation-"):-len(".json")]


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- argv and exit contracts -------------------------------------------------


def test_offerings_without_a_subcommand_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main(["offerings"])
    assert excinfo.value.code == 2


def test_check_requires_sources_flag():
    with pytest.raises(SystemExit) as excinfo:
        main(["offerings", "check"])
    assert excinfo.value.code == 2


def test_missing_sources_file_is_an_input_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    record_dir = tmp_path / "records"
    code = main([
        "offerings", "check", "--sources", str(tmp_path / "absent.json"), "--record-dir", str(record_dir),
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("offerings check:")
    assert not record_dir.exists()


@pytest.mark.parametrize("age", ["0", "-1", "inf", "nan"])
def test_non_positive_or_non_finite_age_is_an_input_error(
    age: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = main(["offerings", "check", "--sources", str(tmp_path / "s.json"), "--max-age-hours", age])
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "max_age_hours" in captured.err


# --- a clean run: one record, no owner report, exit 0 -----------------------


def test_clean_run_defaults_to_the_state_dir_and_writes_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    captures = write_captures(tmp_path / "captures", checked_at=now())
    stub = install_stub(monkeypatch, tmp_path, captures)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    before = read_only_state(captures)

    code = main(["offerings", "check", "--sources", str(captures.sources), "--omp", stub])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner_report"] is None
    assert payload["counts"] == {
        "total": 0,
        STATUS_URGENT_REVIEW: 0,
        STATUS_CHECK_GAP: 0,
        STATUS_PENDING_QUALIFICATION: 0,
    }
    record_dir = state / "omp" / "work-ledger" / "offerings"
    assert set(records_of(record_dir)) == {payload["evaluation"]}
    assert read_only_state(captures) == before


def test_evaluation_record_has_the_declared_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"
    outcome = run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)

    (record_name, record), = records_of(record_dir).items()
    assert record_name == outcome.report["evaluation"]
    assert set(record) == {
        "version",
        "created_ns",
        "checked_at",
        "policy_sha256",
        "task_set_digest",
        "rubric",
        "sources_digest",
        "snapshot",
        "findings",
    }
    assert record["version"] == EVALUATION_VERSION
    assert record["findings"] == []
    assert set(record["snapshot"]) == set(LANE_KINDS)


# --- the record binds the actual policy and task digests ---------------------


def test_record_binds_the_actual_policy_sources_and_task_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"
    run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)

    (record,) = records_of(record_dir).values()
    assert record["policy_sha256"] == hashlib.sha256(policy_path().read_bytes()).hexdigest()
    assert record["sources_digest"] == hashlib.sha256(captures.sources.read_bytes()).hexdigest()
    assert record["task_set_digest"] == load_policy().raw["review"]["task_set_digest"]
    assert record["rubric"] == "docs/model-review.md"


# --- urgent review: owner report shares the stem, routing stays frozen -------


def test_retiring_in_use_model_writes_an_owner_report_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    captures = write_captures(
        tmp_path / "captures", checked_at=now(), overrides={"gemini_flash": ["gemini-2.0-flash"]}
    )
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"
    before = read_only_state(captures)

    code = main([
        "offerings", "check", "--sources", str(captures.sources),
        "--record-dir", str(record_dir), "--omp", stub,
    ])

    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    evaluation_name = payload["evaluation"]
    owner_report_name = payload["owner_report"]
    assert payload["counts"][STATUS_URGENT_REVIEW] == 1 and payload["counts"][STATUS_CHECK_GAP] == 0
    assert owner_report_name == f"owner-report-{stem_of(evaluation_name)}.json"
    assert filenames(record_dir) == {evaluation_name, owner_report_name}
    report = json.loads((record_dir / owner_report_name).read_text())
    (finding,) = report["findings"]
    assert finding["kind"] == "retired" and finding["lane"] == "gemini_flash"
    assert finding["status"] == STATUS_URGENT_REVIEW and finding["urgent"] and finding["in_use"]
    assert finding["model"] == "gemini-3.8-flash-high" and finding["recipes"]
    assert finding["evidence"]["raw_sha256"]
    assert report["version"] == OWNER_REPORT_VERSION
    assert report["evaluation"] == evaluation_name
    assert report["rubric"] == "docs/model-review.md"
    assert report["procedure"]["rubric"] == "docs/model-review.md"
    assert sorted(report["procedure"]["steps"]) == sorted(SCHEDULE_KEYS)
    assert all(report["procedure"]["steps"][key] for key in SCHEDULE_KEYS)
    assert read_only_state(captures) == before


def test_missing_omp_gaps_only_the_cli_lanes(tmp_path: Path):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    outcome = run_offerings_check(
        captures.sources, record_dir=tmp_path / "records", omp=str(tmp_path / "nope"), clock=lambda: checked_at
    )
    assert outcome.exit_code == 3
    assert outcome.report["counts"][STATUS_CHECK_GAP] == 2
    findings = json.loads((tmp_path / "records" / outcome.report["evaluation"]).read_text())["findings"]
    gap_lanes = {finding["lane"] for finding in findings if finding["status"] == STATUS_CHECK_GAP}
    assert gap_lanes == {"gemini_flash", "openai_codex"}


# --- check gap: the retained snapshot survives an unavailable lane ----------


def test_unavailable_listing_is_a_check_gap_that_keeps_the_last_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checked_at = now()
    baseline = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, baseline)
    record_dir = tmp_path / "records"
    run_offerings_check(baseline.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)
    (first,) = records_of(record_dir).values()

    gaps = write_captures(tmp_path / "captures", checked_at=checked_at, overrides={"kimi-code": None})
    install_stub(monkeypatch, tmp_path, gaps)
    before = read_only_state(gaps)
    outcome = run_offerings_check(gaps.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)

    assert outcome.exit_code == 3
    assert outcome.report["counts"][STATUS_CHECK_GAP] == 1
    records = records_of(record_dir)
    assert len(records) == 2
    second = records[outcome.report["evaluation"]]
    assert second["snapshot"]["kimi-code"] == first["snapshot"]["kimi-code"]
    assert read_only_state(gaps) == before


# --- malformed readable history: refuse, never fall back --------------------


def test_malformed_newest_record_is_an_input_error_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"
    run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)
    (valid_name,) = records_of(record_dir)
    valid = json.loads((record_dir / valid_name).read_text())

    bad_name = f"evaluation-{valid['created_ns'] + 10}-{uuid4()}.json"
    bad = dict(valid)
    bad["created_ns"] = valid["created_ns"] + 10
    bad["snapshot"] = {lane: {"catalog_provider": lane, "models": {}} for lane in LANE_KINDS}  # no evidence
    (record_dir / bad_name).write_text(json.dumps(bad), encoding="utf-8")
    before = filenames(record_dir)

    code = main([
        "offerings", "check", "--sources", str(captures.sources),
        "--record-dir", str(record_dir), "--omp", stub,
    ])

    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert bad_name in captured.err and "evidence" in captured.err
    # No fallback to the older, well-formed record and no new record written.
    assert filenames(record_dir) == before


def test_unreadable_newest_record_is_an_input_error_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"
    run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)
    (valid_name,) = records_of(record_dir)
    valid = json.loads((record_dir / valid_name).read_text())
    bad_name = f"evaluation-{valid['created_ns'] + 10}-{uuid4()}.json"
    (record_dir / bad_name).write_text("{not json", encoding="utf-8")
    before = filenames(record_dir)

    code = main([
        "offerings", "check", "--sources", str(captures.sources),
        "--record-dir", str(record_dir), "--omp", stub,
    ])

    assert code == 2
    assert capsys.readouterr().out == ""
    assert filenames(record_dir) == before


# --- baseline -> gap -> recovered change compares against the last success --


def test_recovered_change_compares_against_the_last_successful_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checked_at = now()
    record_dir = tmp_path / "records"
    baseline = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, baseline)
    assert run_offerings_check(
        baseline.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at
    ).exit_code == 0

    gap = write_captures(tmp_path / "captures", checked_at=checked_at, overrides={"kimi-code": None})
    install_stub(monkeypatch, tmp_path, gap)
    assert run_offerings_check(
        gap.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at
    ).exit_code == 3

    recovered = write_captures(
        tmp_path / "captures",
        checked_at=checked_at,
        overrides={"kimi-code": {"ids": ["k3-256k"], "cost": {"input": 9, "output": 9}}},
    )
    install_stub(monkeypatch, tmp_path, recovered)
    third = run_offerings_check(recovered.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)

    records = records_of(record_dir)
    assert len(records) == 3
    third_record = records[third.report["evaluation"]]
    (changed,) = third_record["findings"]
    assert changed["kind"] == "changed" and changed["lane"] == "kimi-code"
    assert changed["status"] == STATUS_PENDING_QUALIFICATION and changed["in_use"]
    assert "cost" in changed["evidence"]["detail"]
    assert third_record["snapshot"]["kimi-code"]["models"]["k3-256k"]["cost"] == {"input": 9, "output": 9}
    assert third.exit_code == 0


# --- repeated checked_at: distinct, immutable, selected in creation order ---


def test_repeated_checked_at_creates_distinct_records_selected_in_creation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checked_at = now()
    captures = write_captures(tmp_path / "captures", checked_at=checked_at)
    stub = install_stub(monkeypatch, tmp_path, captures)
    record_dir = tmp_path / "records"

    first = run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)
    first_name = first.report["evaluation"]
    first_bytes = (record_dir / first_name).read_bytes()
    second = run_offerings_check(captures.sources, record_dir=record_dir, omp=stub, clock=lambda: checked_at)

    records = records_of(record_dir)
    assert len(records) == 2
    assert {first_name, second.report["evaluation"]} == set(records)
    assert records[first_name]["checked_at"] == records[second.report["evaluation"]]["checked_at"]
    assert records[first_name]["created_ns"] < records[second.report["evaluation"]]["created_ns"]
    # The earlier record is untouched by the later run.
    assert (record_dir / first_name).read_bytes() == first_bytes
    # The newest record — the greater creation nanoseconds — is the one resumed.
    previous = discover_previous(record_dir)
    assert previous is not None and previous.creation_ns == records[second.report["evaluation"]]["created_ns"]
    assert previous.document == records[second.report["evaluation"]]
