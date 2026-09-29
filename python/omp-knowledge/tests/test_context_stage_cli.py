"""Contract tests for the stage context CLI subcommand (OMP-419-s08).

The ``stage`` subcommand compiles an in-process stage context bundle using
settings loaded from ``context.json``. It reads the workflow view and optional
project context from stdin, compiles the bundle, records it in sqlite, and prints
a single JSON line to stdout. Failure scenarios (missing/invalid settings,
learning_db presence, invalid stdin, insufficient budget, counter failure)
exit 2 with empty stdout.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from omp_knowledge.context.cli import main
from omp_knowledge.context.rerank import OrderReranker
from omp_work.v1.api_models import WorkflowView
from support.null_engine import NullEngine
from support.stage_fixtures import (
    _counter_script,
    _git_repo,
    _receipt,
    _view,
)


@pytest.fixture(autouse=True)
def _no_knowledge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("OMP_KNOWLEDGE_"):
            monkeypatch.delenv(key, raising=False)


def _one_view() -> tuple[object, WorkflowView]:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    receipt = _receipt(work_id=work_id, revision_id=revision_id, candidate_id=candidate_id)
    view = _view(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        project_id=uuid4(),
        receipts=(receipt,),
    )
    return work_id, view


def _settings_file(
    path: Path,
    *,
    state_dir: Path,
    counter: Path,
    token_budget: int = 100_000,
    structural_state_dir: Path | None = None,
    engine: str = "none",
    extra: dict[str, object] | None = None,
) -> Path:
    payload: dict[str, object] = {
        "command": ["python", "-m", "omp_knowledge.context"],
        "token_cmd": [sys.executable, str(counter)],
        "state_dir": str(state_dir),
        "encoding": "test-words",
        "token_budget": token_budget,
        "engine": engine,
        "reranker_url": None,
        "reranker_model": None,
        "structural_state_dir": None if structural_state_dir is None else str(structural_state_dir),
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _stdin(
    cwd: Path,
    view: WorkflowView,
    *,
    stage: str = "plan",
    project: dict[str, object] | None = None,
) -> io.StringIO:
    payload: dict[str, object] = {
        "stage": stage,
        "attempt_id": "attempt-1",
        "cwd": str(cwd),
        "workflow": json.loads(view.model_dump_json()),
    }
    if project is not None:
        payload["project"] = project
    return io.StringIO(json.dumps(payload))


def test_stage_with_settings_flag(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)

    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == 0, stdout.getvalue()

    raw = stdout.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    payload = json.loads(raw)
    assert payload["stage"] == "plan"
    assert isinstance(payload["bundle_id"], str)
    assert isinstance(payload["bundle_sha256"], str)

    db_path = state_dir / "context-bundles.sqlite"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT bundle_id, stage, bundle_sha256 FROM context_bundles WHERE bundle_id = ?",
            (payload["bundle_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["stage"] == payload["stage"]
    assert row["bundle_sha256"] == payload["bundle_sha256"]


def test_stage_default_settings_from_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings_file = config_dir / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)
    monkeypatch.setenv("OMP_KNOWLEDGE_CONFIG_DIR", str(config_dir))

    stdout = io.StringIO()
    code = main(["stage"], stdin=_stdin(repo, view), stdout=stdout)
    assert code == 0, stdout.getvalue()

    raw = stdout.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    payload = json.loads(raw)
    assert payload["stage"] == "plan"

    db_path = state_dir / "context-bundles.sqlite"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT bundle_id, stage, bundle_sha256 FROM context_bundles WHERE bundle_id = ?",
            (payload["bundle_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["stage"] == payload["stage"]
    assert row["bundle_sha256"] == payload["bundle_sha256"]


def test_stage_stdin_project_includes_roadmap(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)

    project_payload = {
        "refs": [
            {"kind": "roadmap", "ref": "road-1", "title": "ROADMAP-MARKER"},
        ]
    }
    stdin = _stdin(repo, view, project=project_payload)
    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=stdin,
        stdout=stdout,
    )
    assert code == 0, stdout.getvalue()
    payload = json.loads(stdout.getvalue())
    assert "roadmap#" in payload["text"]
    assert "ROADMAP-MARKER" in payload["text"]


def test_stage_settings_with_learning_db_fails(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(
        settings_file,
        state_dir=state_dir,
        counter=counter,
        extra={"learning_db": str(tmp_path / "learning.sqlite")},
    )

    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == 2
    assert stdout.getvalue() == ""


def test_stage_budget_insufficient_fails_no_row(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter, token_budget=1)

    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == 2
    assert stdout.getvalue() == ""

    db_path = state_dir / "context-bundles.sqlite"
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


def test_stage_missing_settings_fails(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(tmp_path / "missing.json")],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == 2
    assert stdout.getvalue() == ""


def test_stage_bad_stdin_fails(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)

    stdout = io.StringIO()
    assert main(["stage", "--settings", str(settings_file)], stdin=io.StringIO(""), stdout=stdout) == 2
    assert stdout.getvalue() == ""

    stdout = io.StringIO()
    assert main(["stage", "--settings", str(settings_file)], stdin=io.StringIO("not json"), stdout=stdout) == 2
    assert stdout.getvalue() == ""

    stdout = io.StringIO()
    bad_missing = {"stage": "plan", "attempt_id": "attempt-1", "workflow": json.loads(view.model_dump_json())}
    assert (
        main(
            ["stage", "--settings", str(settings_file)],
            stdin=io.StringIO(json.dumps(bad_missing)),
            stdout=stdout,
        )
        == 2
    )
    assert stdout.getvalue() == ""

    stdout = io.StringIO()
    bad_unknown = {
        "stage": "plan",
        "attempt_id": "attempt-1",
        "cwd": str(repo),
        "workflow": json.loads(view.model_dump_json()),
        "extra_field": "bad",
    }
    assert (
        main(
            ["stage", "--settings", str(settings_file)],
            stdin=io.StringIO(json.dumps(bad_unknown)),
            stdout=stdout,
        )
        == 2
    )
    assert stdout.getvalue() == ""


def test_stage_counter_failure_fails(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=True)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)

    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=_stdin(repo, view),
        stdout=stdout,
    )
    assert code == 2
    assert stdout.getvalue() == ""


def test_stage_passes_injected_engine_and_reranker(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings_file = tmp_path / "context.json"
    _settings_file(settings_file, state_dir=state_dir, counter=counter)

    engine = NullEngine()
    reranker = OrderReranker()

    stdout = io.StringIO()
    code = main(
        ["stage", "--settings", str(settings_file)],
        stdin=_stdin(repo, view),
        stdout=stdout,
        engine=engine,
        reranker=reranker,
    )
    assert code == 0, stdout.getvalue()
    payload = json.loads(stdout.getvalue())
    assert payload["routes"] == [
        {
            "role": "reranker",
            "name": "order",
            "provider": "order",
            "model": "",
            "accelerator": "cpu",
            "used": "primary",
            "reason": "",
        }
    ]
