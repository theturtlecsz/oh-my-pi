"""In-process stage compiler (OMP-419-s02).

Plan, implement and audit compiles of one work item run with no
OMP_KNOWLEDGE_* variable. Each context kind is present only when its source
is, a learning store in the state dir contributes no lesson, learning_db is
refused, and a budget below the mandatory items writes no bundle row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omp_knowledge.context.compiler import BudgetInsufficientError
from omp_knowledge.context.stage import (
    ContextSettings,
    compile_stage,
    default_settings_path,
    load_settings,
)
from omp_work.knowledge_source import normalize_remote_url
from omp_work.v1.canonical import sha256
from support.null_engine import NullEngine
from support.stage_fixtures import (
    _APPLICABLE_TITLE,
    _ORIGIN,
    _SNAP_PUBLISHED,
    _TITLE,
    _TOKEN_BUDGET,
    _WITHDRAWN_TITLE,
    _counter_script,
    _git_repo,
    _publish_fixture,
    _receipt,
    _seed_procedures,
    _view,
)

_ORDER_ROUTE = {
    "role": "reranker",
    "name": "order",
    "provider": "order",
    "model": "",
    "accelerator": "cpu",
    "used": "primary",
    "reason": "",
}
_KINDS = (
    "adr",
    "repository",
    "roadmap",
    "decision",
    "mission",
    "research",
    "enola",
    "retrieval",
)
_LESSON_TITLES = (
    _APPLICABLE_TITLE,
    _WITHDRAWN_TITLE,
    "UNCONDITIONAL-LESSON",
    "WITHDRAWN-LESSON",
)


@pytest.fixture(autouse=True)
def _no_knowledge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("OMP_KNOWLEDGE_"):
            monkeypatch.delenv(key, raising=False)


def _settings_file(
    path: Path,
    *,
    state_dir: Path,
    counter: Path,
    structural_state_dir: Path | None,
    token_budget: int,
    engine: str = "none",
) -> ContextSettings:
    payload = {
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
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(path)


def _one_view():
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


def _commit_adr(path: Path) -> str:
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    adr = path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-stage.md").write_text(
        "# ADR-MARKER-HEADING\n\nThe in-process stage path keeps this paragraph.\n\n## Later\n\nnope\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _project_payload() -> dict[str, object]:
    return {
        "refs": [
            {"kind": "decision", "ref": "dec-1", "title": "DECISION-MARKER"},
            {"kind": "roadmap", "ref": "road-1", "title": "ROADMAP-MARKER"},
        ],
        "missions": [
            {"mission_id": "mission-1", "objective": "MISSION-MARKER", "status": "completed"},
        ],
        "history": [
            {"mission_id": "mission-1", "kind": "finish", "summary": "landed"},
        ],
        "research": [
            {
                "campaign_id": "research-1",
                "domain": "RESEARCH-MARKER",
                "outcome": "adopted",
                "outcome_reason": "fits the stage",
            }
        ],
    }


def _seed_unconditional(db_path: Path) -> None:
    from omp_knowledge.learning.store import LearningStore

    store = LearningStore(db_path)
    with store.transaction() as conn:
        for procedure_id, title, status in (
            ("proc-always", "UNCONDITIONAL-LESSON", "active"),
            ("proc-always-old", "WITHDRAWN-LESSON", "withdrawn"),
        ):
            conn.execute(
                """
                INSERT INTO procedures (
                    procedure_id, fingerprint, status, current_version, title, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, '2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')
                """,
                (procedure_id, sha256([procedure_id]), status, title),
            )
            conn.execute(
                """
                INSERT INTO procedure_versions (
                    procedure_id, version, title, steps_json, preconditions_json,
                    model, profile, source_json, created_at
                ) VALUES (?, 1, ?, ?, '[]', 'm', 'p', '{}', '2026-09-26T00:00:00Z')
                """,
                (procedure_id, title, json.dumps(["do not include"])),
            )
    store.close()


def _seed_retrieval(engine: NullEngine, workspace_id: UUID, repository_id: UUID) -> None:
    key = f"{workspace_id}:{repository_id}@{_SNAP_PUBLISHED}"
    engine.snapshots[key] = [
        {
            "id": "fact-retrieval",
            "name": _TITLE,
            "kind": "symbol",
            "file": "src/retrieval.py",
            "line": 4,
        }
    ]
    engine.publish(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=_SNAP_PUBLISHED,
    )


def _has_source(text: str, source: str) -> bool:
    return f"- {source}#" in text


def _bundle_rows(state_dir: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(state_dir / "context-bundles.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "SELECT work_id, stage, bundle_sha256 FROM context_bundles"
            )
        )
    finally:
        connection.close()


def _bundle_count(state_dir: Path) -> int:
    db_path = state_dir / "context-bundles.sqlite"
    if not db_path.exists():
        return 0
    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM context_bundles").fetchone()[0]
    finally:
        connection.close()
    return int(count)


def _route_rows(state_dir: Path, bundle_id: str) -> list[dict[str, str]]:
    connection = sqlite3.connect(state_dir / "context-routes.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT role, name, provider, model, accelerator, used, reason
            FROM context_bundle_routes
            WHERE bundle_id = ?
            """,
            (bundle_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _exclusion_sources(state_dir: Path) -> set[str]:
    db_path = state_dir / "context-bundles.sqlite"
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT source FROM context_exclusions").fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _assert_no_lesson(result: dict[str, object], state_dir: Path) -> None:
    text = str(result["text"])
    for title in _LESSON_TITLES:
        assert title not in text
    exclusions = result["exclusions"]
    assert isinstance(exclusions, list)
    for exclusion in exclusions:
        assert exclusion["source"] != "lesson"
        assert exclusion["section"] != "procedural"
    assert "lesson" not in _exclusion_sources(state_dir)


def test_plan_implement_audit_include_present_kinds(tmp_path: Path) -> None:
    assert not any(key.startswith("OMP_KNOWLEDGE_") for key in os.environ)
    workspace_id = uuid4()
    repository_id = uuid4()
    work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    head = _commit_adr(repo)
    structural_dir = tmp_path / "structural"
    _publish_fixture(structural_dir, workspace_id, repository_id)
    state_dir = tmp_path / "state"
    learning_db = state_dir / "learning.sqlite"
    state_dir.mkdir()
    _seed_procedures(
        learning_db,
        project_id=view.item.project_id,
        repository=normalize_remote_url(_ORIGIN),
    )
    _seed_unconditional(learning_db)
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)
    settings = _settings_file(
        tmp_path / "context.json",
        state_dir=state_dir,
        counter=counter,
        structural_state_dir=structural_dir,
        token_budget=_TOKEN_BUDGET,
    )
    engine = NullEngine()
    _seed_retrieval(engine, workspace_id, repository_id)
    project = _project_payload()
    snapshots = ((workspace_id, repository_id, _SNAP_PUBLISHED),)
    permitted = (repository_id,)

    results = [
        compile_stage(
            settings,
            view=view,
            stage=stage,
            attempt_id="attempt-1",
            cwd=str(repo),
            project=project,
            snapshots=snapshots,
            permitted_repositories=permitted,
            engine=engine,
        )
        for stage in ("plan", "implement", "audit")
    ]

    assert [result["stage"] for result in results] == ["plan", "implement", "audit"]
    assert len({result["bundle_id"] for result in results}) == 3
    rows = _bundle_rows(state_dir)
    assert len(rows) == 3
    assert {(row["stage"], row["bundle_sha256"]) for row in rows} == {
        (result["stage"], result["bundle_sha256"]) for result in results
    }
    assert {row["work_id"] for row in rows} == {str(work_id)}

    for result in results:
        text = result["text"]
        assert set(result) == {
            "bundle_id",
            "bundle_sha256",
            "stage",
            "tokens",
            "token_budget",
            "exclusions",
            "text",
            "routes",
        }
        assert result["routes"] == [_ORDER_ROUTE]
        assert _route_rows(state_dir, result["bundle_id"]) == [_ORDER_ROUTE]
        assert _TITLE in text
        assert "ADR-MARKER-HEADING" in text
        assert head in text
        assert "DECISION-MARKER" in text
        assert "ROADMAP-MARKER" in text
        assert "MISSION-MARKER" in text
        assert "RESEARCH-MARKER" in text
        assert "py/pkg/alpha.normalize" in text
        assert "src/retrieval.py" in text
        for kind in _KINDS:
            assert _has_source(text, kind)
        _assert_no_lesson(result, state_dir)


def test_kinds_absent_when_sources_missing(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "empty-repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)
    settings = _settings_file(
        tmp_path / "context.json",
        state_dir=state_dir,
        counter=counter,
        structural_state_dir=None,
        token_budget=_TOKEN_BUDGET,
    )
    result = compile_stage(
        settings,
        view=view,
        stage="implement",
        attempt_id="attempt-1",
        cwd=str(repo),
        project=None,
        snapshots=(),
        permitted_repositories=(),
        engine=None,
    )
    text = result["text"]
    assert _TITLE in text
    for marker in (
        "ADR-MARKER-HEADING",
        "DECISION-MARKER",
        "ROADMAP-MARKER",
        "MISSION-MARKER",
        "RESEARCH-MARKER",
        "py/pkg/alpha.normalize",
        "src/retrieval.py",
    ):
        assert marker not in text
    for kind in _KINDS:
        assert not _has_source(text, kind)


def test_cognee_setting_retrieves_with_engine_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid4()
    repository_id = uuid4()
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)
    settings = _settings_file(
        tmp_path / "context.json",
        state_dir=state_dir,
        counter=counter,
        structural_state_dir=None,
        token_budget=_TOKEN_BUDGET,
        engine="cognee",
    )
    engine = NullEngine()
    _seed_retrieval(engine, workspace_id, repository_id)
    seen: dict[str, object] = {}

    def _fake_engine_for(engine_name: str, injected: object) -> NullEngine:
        seen["engine_name"] = engine_name
        seen["injected"] = injected
        return engine

    monkeypatch.setattr("omp_knowledge.context.cli.engine_for", _fake_engine_for)
    result = compile_stage(
        settings,
        view=view,
        stage="plan",
        attempt_id="attempt-1",
        cwd=str(repo),
        snapshots=((workspace_id, repository_id, _SNAP_PUBLISHED),),
        permitted_repositories=(repository_id,),
        engine=None,
    )
    assert seen == {"engine_name": "cognee", "injected": None}
    assert _has_source(result["text"], "retrieval")
    assert "src/retrieval.py" in result["text"]
    assert not _has_source(result["text"], "enola")


def test_learning_db_settings_refused(tmp_path: Path) -> None:
    payload = {
        "command": ["python", "-m", "omp_knowledge.context"],
        "token_cmd": [sys.executable, "counter.py"],
        "state_dir": str(tmp_path / "state"),
        "encoding": "test-words",
        "token_budget": _TOKEN_BUDGET,
        "engine": "none",
        "reranker_url": None,
        "reranker_model": None,
        "structural_state_dir": None,
        "learning_db": str(tmp_path / "learning.sqlite"),
    }
    path = tmp_path / "context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)
    with pytest.raises(ValidationError):
        ContextSettings.model_validate(payload)
    with pytest.raises(ValidationError):
        ContextSettings(
            command=["python"],
            token_cmd=[sys.executable],
            state_dir=str(tmp_path),
            encoding="test-words",
            token_budget=1,
            learning_db=str(tmp_path / "learning.sqlite"),
        )
    with pytest.raises(ValidationError):
        ContextSettings(
            command=["python"],
            token_cmd=[sys.executable],
            token_budget=_TOKEN_BUDGET,
            reranker_url="http://127.0.0.1:9/rerank",
        )
    with pytest.raises(ValidationError):
        ContextSettings(command=[], token_cmd=[sys.executable], token_budget=_TOKEN_BUDGET)
    with pytest.raises(ValidationError):
        ContextSettings(command=["python"], token_cmd=[""], token_budget=_TOKEN_BUDGET)
    with pytest.raises(ValidationError):
        ContextSettings(command=["python"], token_cmd=[sys.executable], token_budget=0)
    with pytest.raises(ValidationError):
        ContextSettings(
            command=["python"],
            token_cmd=[sys.executable],
            token_budget=_TOKEN_BUDGET,
            engine="bogus",
        )


def test_default_settings_path_follows_config_dir(tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    xdg = tmp_path / "xdg"
    home = tmp_path / "home"
    assert default_settings_path({"OMP_KNOWLEDGE_CONFIG_DIR": str(custom)}) == str(
        custom / "context.json"
    )
    assert default_settings_path(
        {"XDG_CONFIG_HOME": str(xdg), "HOME": str(home)}
    ) == str(xdg / "omp-knowledge" / "context.json")
    assert default_settings_path({"HOME": str(home)}) == str(
        home / ".config" / "omp-knowledge" / "context.json"
    )
    assert default_settings_path(
        {"OMP_KNOWLEDGE_CONFIG_DIR": "", "XDG_CONFIG_HOME": "", "HOME": str(home)}
    ) == str(home / ".config" / "omp-knowledge" / "context.json")


def test_budget_below_mandatory_writes_no_row(tmp_path: Path) -> None:
    _work_id, view = _one_view()
    repo = tmp_path / "repo"
    _git_repo(repo)
    state_dir = tmp_path / "state"
    counter = tmp_path / "counter.py"
    _counter_script(counter, fail=False)
    settings = _settings_file(
        tmp_path / "context.json",
        state_dir=state_dir,
        counter=counter,
        structural_state_dir=None,
        token_budget=1,
    )
    with pytest.raises(BudgetInsufficientError):
        compile_stage(
            settings,
            view=view,
            stage="audit",
            attempt_id="attempt-1",
            cwd=str(repo),
        )
    assert _bundle_count(state_dir) == 0
    assert not (state_dir / "context-routes.sqlite").exists()
