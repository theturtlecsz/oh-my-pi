"""The model-proxy drill fills one validated settlement request and stops."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.orchestrator.candidate_git import git
from omp_work.orchestrator.service import load_request
from omp_work.v1.canonical import text_sha256
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_DEPLOY = Path(__file__).parents[1] / "deploy"
_TEMPLATE = _DEPLOY / "model-proxy-request.json"
_DRILL = _DEPLOY / "model_proxy_drill.py"
_NOTE = b"proxy settlement drill\n"
_BUDGET = {
    "usd": "5",
    "tokens": 2000000,
    "wall_clock_seconds": 1800,
    "max_subagents": 0,
}


def _init_repo(path: Path) -> str:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@omp.dev")
    (path / "README").write_text("drill\n", encoding="utf-8")
    git(path, "add", "README")
    git(path, "commit", "-m", "init")
    return git(path, "rev-parse", "HEAD").decode("ascii").strip()


def _workspace(service, workspace_id: UUID) -> None:
    _grant(service, workspace_id)
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )


def _write_config(
    root: Path,
    *,
    workspace_id: UUID,
    control_repo: Path,
    operations,
    workspaces: list[str] | None = None,
    scopes: list[str] | None = None,
) -> Path:
    capability = root / "capability.json"
    capability.write_text(
        json.dumps(
            {
                "actor_id": str(OWNER),
                "actor_kind": "automation",
                "workspaces": [str(workspace_id)] if workspaces is None else workspaces,
                "scopes": ["work.read"] if scopes is None else scopes,
            }
        ),
        encoding="utf-8",
    )
    config = root / "drill.json"
    config.write_text(
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "capability": str(capability),
                "control_repo": str(control_repo),
                "operations": {
                    "config_dir": str(operations.config_dir),
                    "state_dir": str(operations.state_dir),
                    "data_dir": str(operations.data_dir),
                    "host": operations.host,
                    "port": operations.port,
                    "database": operations.database,
                },
            }
        ),
        encoding="utf-8",
    )
    return config


def _run_drill(
    config: Path, project_key: str, out: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_DRILL),
            "--config",
            str(config),
            "--project-key",
            project_key,
            "--out",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _note_command(
    root: Path, command: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def test_drill_fills_distinct_missions_from_the_project(
    service, tmp_path: Path
) -> None:
    template = json.loads(_TEMPLATE.read_text(encoding="utf-8"))
    head = _init_repo(tmp_path / "control")
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    store = PostgresWorkStore(service.config)
    project_key = "proxy-drill"
    repo_key = "oh-my-pi"
    repo_url = "https://example.test/oh-my-pi.git"
    default_branch = "trunk"
    project_id = store.ensure_project(
        workspace_id, OWNER, project_key, "Proxy drill", "surface"
    )
    store.update_profile(
        workspace_id,
        OWNER,
        project_id,
        repositories=[
            {
                "key": repo_key,
                "url": repo_url,
                "default_branch": default_branch,
            }
        ],
    )
    config = _write_config(
        tmp_path,
        workspace_id=workspace_id,
        control_repo=tmp_path / "control",
        operations=service.config,
    )

    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_run = _run_drill(config, project_key, first_path)
    second_run = _run_drill(config, project_key, second_path)
    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr

    first_id = UUID(first_run.stdout.strip())
    second_id = UUID(second_run.stdout.strip())
    assert first_run.stdout == f"{first_id}\n"
    assert second_run.stdout == f"{second_id}\n"
    assert first_id != second_id

    first = load_request(first_path)
    second = load_request(second_path)
    assert first.mission_id == first_id
    assert second.mission_id == second_id
    text = first.intake.source.text
    assert text == template["intake"]["source"]["text"]
    assert "NOTES-OMP-615.md" in text
    assert "proxy settlement drill\\n" in text
    assert first.intake.source.sha256 == text_sha256(text)
    assert first.intake.source.sha256 == template["intake"]["source"]["sha256"]
    assert first.instruction.text == text
    assert second.instruction.text == text
    assert first.work.description == text
    assert list(first.work.acceptance_criteria) == [text]
    assert first.intake.acceptance_criteria[0].statement == text
    assert first.intake.goal.statement == text

    for request, mission_id in ((first, first_id), (second, second_id)):
        assert request.project_id == project_id
        assert request.scope.project_id == project_id
        assert request.work.project_id == project_id
        assert request.work.client_ref == str(mission_id)
        assert request.repository == repo_key
        assert request.remote == repo_url
        assert request.target_ref == default_branch
        assert request.base_commit == head
        assert request.candidate_ref == f"refs/heads/omp/{mission_id}"
        assert request.scope.repositories == (repo_key,)
        assert request.scope.requested_capabilities == ("git_read", "git_write")
        assert request.scope.risk_policy == "standard"
        assert request.scope.approval_policy == "standard"
        assert request.scope.effort_policy == "standard"
        assert request.scope.budget_policy is not None
        assert request.scope.budget_policy.model_dump() == _BUDGET
        assert request.intake.budget is not None
        assert request.intake.budget.model_dump() == _BUDGET
        assert request.controls == {"wall_clock_seconds": 1800}
        assert request.allowed_paths == ("NOTES-OMP-615.md",)
        assert list(request.test_command) == template["test_command"]
        assert request.test_command[0] == "/usr/bin/python3"
        assert list(request.worker_argv) == template["worker_argv"]
        assert len(request.worker_argv) == 2
        assert Path(request.worker_argv[0]).is_absolute()
        assert request.worker_argv[1] == "orchestrator-worker"

    assert first.work.client_ref != second.work.client_ref
    assert first.candidate_ref != second.candidate_ref

    command = first.test_command
    missing = tmp_path / "missing-note"
    missing.mkdir()
    refused = _note_command(missing, command)
    assert refused.returncode != 0

    wrong = tmp_path / "wrong-note"
    wrong.mkdir()
    (wrong / "NOTES-OMP-615.md").write_bytes(b"other drill\n")
    refused = _note_command(wrong, command)
    assert refused.returncode != 0

    crlf = tmp_path / "crlf-note"
    crlf.mkdir()
    (crlf / "NOTES-OMP-615.md").write_bytes(b"proxy settlement drill\r\n")
    refused = _note_command(crlf, command)
    assert refused.returncode != 0

    exact = tmp_path / "exact-note"
    exact.mkdir()
    (exact / "NOTES-OMP-615.md").write_bytes(_NOTE)
    accepted = _note_command(exact, command)
    assert accepted.returncode == 0, accepted.stderr


def test_drill_refuses_without_membership_or_work_read(tmp_path: Path) -> None:
    workspace_id = uuid4()
    control = tmp_path / "control"
    control.mkdir()
    operations = type(
        "Ops",
        (),
        {
            "config_dir": tmp_path / "config",
            "state_dir": tmp_path / "state",
            "data_dir": tmp_path / "data",
            "host": "127.0.0.1",
            "port": 1,
            "database": "omp_work",
        },
    )()
    cases = (
        ([str(uuid4())], ["work.read"]),
        ([str(workspace_id)], ["work.mutate"]),
    )
    for workspaces, scopes in cases:
        case = tmp_path / f"case-{uuid4().hex}"
        case.mkdir()
        config = _write_config(
            case,
            workspace_id=workspace_id,
            control_repo=control,
            operations=operations,
            workspaces=workspaces,
            scopes=scopes,
        )
        out = case / "request.json"
        result = _run_drill(config, "proxy-drill", out)
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr.strip() == "forbidden"
        assert not out.exists()
