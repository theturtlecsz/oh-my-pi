"""Tests for procedural lesson compilation into stage context bundles (OMP-607-s03).

Active procedures require both a journey pass and owner-approved promotion batch
to be supplied. Supplies are recorded in SQLite in a single transaction, immutable
supply lines are rendered, compiler failure rolls back supplies, and exclusions
delete provisional supplies.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_knowledge.context.cli import main
from omp_knowledge.context.compiler import BudgetInsufficientError
from omp_knowledge.context.stage import ContextSettings, compile_stage
from omp_knowledge.learning.promotion import (
    answer_promotion,
    record_journey_pass,
    request_promotion,
)
from omp_knowledge.learning.store import LearningStore
from omp_work.knowledge_source import normalize_remote_url
from omp_work.v1.api_models import WorkflowView
from support.stage_fixtures import (
    _ORIGIN,
    _TOKEN_BUDGET,
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


def _setup_environment(tmp_path: Path) -> tuple[Path, Path, ContextSettings, WorkflowView]:
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)

    settings = ContextSettings(
        command=["python", "-m", "omp_knowledge.context"],
        token_cmd=[sys.executable, str(counter)],
        state_dir=str(state_dir),
        encoding="test-words",
        token_budget=_TOKEN_BUDGET,
    )

    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    project_id = uuid4()
    receipt = _receipt(work_id=work_id, revision_id=revision_id, candidate_id=candidate_id)
    view = _view(
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        project_id=project_id,
        receipts=(receipt,),
    )
    return repo, state_dir, settings, view


def _seed_procedure(
    store: LearningStore,
    *,
    procedure_id: str = "proc-1",
    title: str = "Active Lesson Title",
    steps: list[str] | None = None,
    project_id: UUID | str | None = None,
    repository: str | None = None,
    status: str = "active",
    version: int = 1,
) -> None:
    preconditions: list[dict[str, str]] = []
    if project_id is not None:
        preconditions.append({"key": "project_id", "op": "eq", "value": str(project_id)})
    if repository is not None:
        preconditions.append({"key": "repository", "op": "eq", "value": repository})

    steps_list = steps if steps is not None else ["step one", "step two"]

    with store.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO procedures (
                procedure_id, fingerprint, status, current_version, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '2026-10-13T00:00:00Z', '2026-10-13T00:00:00Z')
            """,
            (procedure_id, f"fp-{procedure_id}", status, version, title),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO procedure_versions (
                procedure_id, version, title, steps_json, preconditions_json,
                model, profile, source_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'm', 'p', '{}', '2026-10-13T00:00:00Z')
            """,
            (
                procedure_id,
                version,
                title,
                json.dumps(steps_list),
                json.dumps(preconditions),
            ),
        )


def _approve_procedure(
    store: LearningStore,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str | None = None,
) -> str:
    record_journey_pass(store, workspace_id=workspace_id, work_id=None)
    decision = request_promotion(store, workspace_id=workspace_id, project_id=project_id)
    if decision is None:
        raise RuntimeError("failed to request promotion")
    answer_promotion(
        store,
        decision["decision_id"],
        approve=True,
        answer_ref="owner-test-approval",
    )
    return decision["decision_id"]


def test_active_procedure_with_journey_pass_and_approved_batch(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    norm_repo = normalize_remote_url(_ORIGIN)
    _seed_procedure(
        store,
        procedure_id="proc-success",
        title="Verified Lesson",
        steps=["run diagnostics", "apply fix"],
        project_id=view.item.project_id,
        repository=norm_repo,
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    text = result["text"]
    assert "## procedural" in text
    assert "Verified Lesson" in text
    assert "run diagnostics; apply fix" in text
    assert "PROCEDURE proc-success@v1" in text

    supply_ids = result.get("lesson_supply_ids")
    assert isinstance(supply_ids, list)
    assert len(supply_ids) == 1
    supply_id = supply_ids[0]
    assert f"supply={supply_id}" in text
    assert f"- lesson#{supply_id}:" in text

    # Verify matching supply recorded in SQLite
    rows = store.connection.execute(
        "SELECT * FROM supplies WHERE supply_id = ?", (supply_id,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["procedure_id"] == "proc-success"
    assert row["version"] == 1
    assert row["workspace_id"] == str(view.item.workspace_id)
    assert row["work_key"] == "OMP-311"
    ctx = json.loads(row["context_json"])
    assert ctx["project_id"] == str(view.item.project_id)
    assert ctx["repository"] == norm_repo


def test_missing_journey_pass_yields_no_lesson_text(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-no-pass",
        title="Unpassed Lesson",
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    # No journey pass recorded

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    assert "Unpassed Lesson" not in result["text"]
    assert result["lesson_supply_ids"] == []
    supplies = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert supplies == 0


def test_unapproved_promotion_batch_yields_no_lesson_text(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-rejected",
        title="Rejected Lesson",
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    record_journey_pass(store, workspace_id=view.item.workspace_id, work_id=None)
    decision = request_promotion(
        store, workspace_id=view.item.workspace_id, project_id=view.item.project_id
    )
    assert decision is not None
    answer_promotion(
        store, decision["decision_id"], approve=False, answer_ref="rejected-decision"
    )

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    assert "Rejected Lesson" not in result["text"]
    assert result["lesson_supply_ids"] == []
    supplies = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert supplies == 0


def test_mismatched_repository_yields_no_lesson_text(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-diff-repo",
        title="Wrong Repo Lesson",
        project_id=view.item.project_id,
        repository="git@github.com:other/unrelated.git",
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    assert "Wrong Repo Lesson" not in result["text"]
    assert result["lesson_supply_ids"] == []
    supplies = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert supplies == 0


def test_mismatched_project_yields_no_lesson_text(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    different_project = uuid4()
    _seed_procedure(
        store,
        procedure_id="proc-diff-proj",
        title="Wrong Project Lesson",
        project_id=different_project,
        repository=normalize_remote_url(_ORIGIN),
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=different_project)

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    assert "Wrong Project Lesson" not in result["text"]
    assert result["lesson_supply_ids"] == []
    supplies = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert supplies == 0


def test_omitted_learning_store_yields_no_lesson_text(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-store-omitted",
        title="Store Omitted Lesson",
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=None,
    )

    assert "Store Omitted Lesson" not in result["text"]
    assert "lesson_supply_ids" not in result
    supplies = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert supplies == 0


def test_cli_stage_uses_same_learning_db(tmp_path: Path) -> None:
    repo, state_dir, _settings, view = _setup_environment(tmp_path)
    counter = tmp_path / "counter.py"
    settings_file = tmp_path / "context.json"
    settings_payload = {
        "command": ["python", "-m", "omp_knowledge.context"],
        "token_cmd": [sys.executable, str(counter)],
        "state_dir": str(state_dir),
        "encoding": "test-words",
        "token_budget": _TOKEN_BUDGET,
    }
    settings_file.write_text(json.dumps(settings_payload), encoding="utf-8")

    learning_dir = tmp_path / "learning"
    store = LearningStore(learning_dir)
    _seed_procedure(
        store,
        procedure_id="proc-cli",
        title="CLI Verified Lesson",
        steps=["step via cli"],
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)
    store.close()

    stdin_payload = {
        "stage": "implement",
        "attempt_id": "attempt-1",
        "cwd": str(repo),
        "workflow": json.loads(view.model_dump_json()),
    }
    stdin = io.StringIO(json.dumps(stdin_payload))
    stdout = io.StringIO()

    exit_code = main(
        [
            "stage",
            "--settings",
            str(settings_file),
            "--learning-state-dir",
            str(learning_dir),
        ],
        stdin=stdin,
        stdout=stdout,
    )
    assert exit_code == 0, stdout.getvalue()

    payload = json.loads(stdout.getvalue())
    assert "CLI Verified Lesson" in payload["text"]
    lesson_supply_ids = payload.get("lesson_supply_ids")
    assert isinstance(lesson_supply_ids, list)
    assert len(lesson_supply_ids) == 1

    # Reopen DB to verify supplies recorded
    store_check = LearningStore(learning_dir)
    rows = store_check.connection.execute("SELECT * FROM supplies").fetchall()
    store_check.close()
    assert len(rows) == 1
    assert rows[0]["supply_id"] == lesson_supply_ids[0]
    assert rows[0]["procedure_id"] == "proc-cli"


def test_mutating_versions_between_supply_selection_and_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-tamper",
        title="Original Recorded Lesson",
        steps=["original step"],
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    # Monkeypatch finish_compile to verify that mutating procedure_versions in the DB
    # cannot alter the rendered text because ContextItems are built from SupplyResult.lines
    from omp_knowledge.context import stage as stage_module

    original_finish_compile = stage_module.finish_compile

    def _tampered_finish_compile(**kwargs: object) -> dict[str, object]:
        # Attempt to mutate procedure_versions during compile
        store.connection.execute(
            "UPDATE procedure_versions SET title = 'MUTATED_EVIL_TITLE' WHERE procedure_id = 'proc-tamper'"
        )
        return original_finish_compile(**kwargs)

    monkeypatch.setattr(stage_module, "finish_compile", _tampered_finish_compile)

    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )

    # The rendered text MUST contain the original recorded title, NOT the mutated title
    assert "Original Recorded Lesson" in result["text"]
    assert "MUTATED_EVIL_TITLE" not in result["text"]

    # Also verify that while the transaction is held, another connection cannot write
    other_conn = sqlite3.connect(store.path, timeout=0.001)
    with store.transaction():
        with pytest.raises(sqlite3.OperationalError):
            other_conn.execute("INSERT INTO journey_passes VALUES ('x', 'y', 'z', 'w', 'v', 'now')")
    other_conn.close()


def test_compiler_failure_rolls_back_supplies(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    # Set token budget to 1 so finish_compile raises BudgetInsufficientError
    failing_settings = ContextSettings(
        command=settings.command,
        token_cmd=settings.token_cmd,
        state_dir=settings.state_dir,
        encoding=settings.encoding,
        token_budget=1,
    )
    store = LearningStore(tmp_path / "learning")

    _seed_procedure(
        store,
        procedure_id="proc-fail",
        title="Lesson Should Roll Back",
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    with pytest.raises(BudgetInsufficientError):
        compile_stage(
            failing_settings,
            view=view,
            stage="implement",
            attempt_id="attempt-1",
            cwd=str(repo),
            learning_store=store,
        )

    # Supplies inserted by supply() during the failed compile MUST be rolled back
    count = store.connection.execute("SELECT COUNT(*) FROM supplies").fetchone()[0]
    assert count == 0


def test_procedural_exclusions_delete_provisional_supplies(tmp_path: Path) -> None:
    repo, _state_dir, settings, view = _setup_environment(tmp_path)
    store = LearningStore(tmp_path / "learning")

    norm_repo = normalize_remote_url(_ORIGIN)
    # Create two approved lessons with distinct titles
    _seed_procedure(
        store,
        procedure_id="proc-keep",
        title="Lesson To Keep In Budget",
        steps=["short step"],
        project_id=view.item.project_id,
        repository=norm_repo,
    )
    _seed_procedure(
        store,
        procedure_id="proc-drop",
        title="Lesson To Exclude Due To Budget Limit Very Long Text Here",
        steps=["another long step exceeding budget constraints"],
        project_id=view.item.project_id,
        repository=norm_repo,
    )
    _approve_procedure(store, workspace_id=view.item.workspace_id, project_id=view.item.project_id)

    # First compile with full budget to find baseline tokens
    full_res = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        learning_store=store,
    )
    assert len(full_res["lesson_supply_ids"]) == 2
    full_tokens = full_res["tokens"]

    # Now compile with reduced budget that fits 1 lesson but not 2
    restricted_settings = ContextSettings(
        command=settings.command,
        token_cmd=settings.token_cmd,
        state_dir=settings.state_dir,
        encoding=settings.encoding,
        token_budget=full_tokens - 4,
    )
    restricted_res = compile_stage(
        restricted_settings,
        view=view,
        stage="implement",
        attempt_id="attempt-2",
        cwd=str(repo),
        learning_store=store,
    )

    procedural_exclusions = [
        exc for exc in restricted_res["exclusions"] if exc["section"] == "procedural"
    ]
    assert len(procedural_exclusions) == 1
    dropped_supply_id = procedural_exclusions[0]["ref"]

    retained_ids = restricted_res["lesson_supply_ids"]
    assert len(retained_ids) == 1
    retained_supply_id = retained_ids[0]
    assert dropped_supply_id != retained_supply_id

    # Verify: the dropped supply was deleted from the supplies table
    supplies_in_db = [
        row["supply_id"]
        for row in store.connection.execute(
            "SELECT supply_id FROM supplies WHERE work_key = 'OMP-311' AND supply_id IN (?, ?)",
            (dropped_supply_id, retained_supply_id),
        ).fetchall()
    ]
    assert dropped_supply_id not in supplies_in_db
    assert retained_supply_id in supplies_in_db
