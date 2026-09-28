"""Interactive fixture env against the fake docker. No socket is bound."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omp_harbor_eval.interactive_env import main
from omp_harbor_eval.interactive_env import run_interactive_env
from omp_harbor_eval.scripted_model import models_yml

DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
WORK_TS = REPO_ROOT / "session-system" / "extensions" / "workflow" / "work.ts"
BEARER = "harbor-test-token"
WORKSPACE = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
WS = "ws"
WORKER = "wk"
MODEL_URL = "http://127.0.0.1:9090/v1"
HEALTH = {"ready": True, "live": True}
START_EXECUTION = {
    "grant": {"state": "active"},
    "active_item": {"work_id": WORK_ID},
    "view": "startup",
}
START_ITEM = {"work_id": WORK_ID, "state": "running", "view": "startup"}
NEW_EXECUTION = {
    "grant": {"state": "completed"},
    "active_item": {"work_id": WORK_ID},
    "view": "rewritten",
}
NEW_ITEM = {"work_id": WORK_ID, "state": "completed", "revision": {"revision_number": 2}, "view": "rewritten"}


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    _write_config(directory)
    return directory


def test_f1_prints_the_key_and_reads_the_service_after_stop(
    fake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OMP_HARBOR_BEARER", BEARER)
    monkeypatch.setenv("OMP_HARBOR_WORKSPACE_ID", WORKSPACE)
    _write_config(fake_dir, responses=_responses(START_EXECUTION, START_ITEM))
    _prepare_workspace(fake_dir)
    out = tmp_path / "out"
    seen = {"during_stop": False}

    def stop() -> None:
        assert not (out / "service-readback.json").exists()
        assert not any("/execution" in token for record in _calls(fake_dir) for token in record["argv"])
        session = fake_dir / "root" / "home" / "agent" / "omp-sessions"
        session.mkdir(parents=True)
        (session / "trial.jsonl").write_text("hello-session\n", encoding="utf-8")
        _write_config(fake_dir, responses=_responses(NEW_EXECUTION, NEW_ITEM))
        seen["during_stop"] = True

    code = run_interactive_env("f1", out, fixtures_root=FIXTURES, docker=str(DOCKER), stop=stop)

    assert code == 0
    assert seen["during_stop"] is True
    captured = capsys.readouterr()
    assert captured.out == (
        "key: OMP-1\n"
        f"omp: docker exec -it {WORKER} omp --model scripted/scripted --session-dir /home/agent/omp-sessions\n"
        f"session dir: {out / 'session'}\n"
    )
    readback = json.loads((out / "service-readback.json").read_text(encoding="utf-8"))
    assert readback == {"execution": NEW_EXECUTION, "health": HEALTH, "work_item": NEW_ITEM}
    assert "startup" not in (out / "service-readback.json").read_text(encoding="utf-8")
    assert (out / "session" / "trial.jsonl").read_text(encoding="utf-8") == "hello-session\n"
    assert (fake_dir / "root" / "home" / "agent" / ".omp" / "agent" / "models.yml").read_text(
        encoding="utf-8"
    ) == models_yml(MODEL_URL)
    assert not any(BEARER in token for record in _calls(fake_dir) for token in record["argv"])

    seed = hashlib.sha256((FIXTURES / "f1" / "seed.patch").read_bytes()).hexdigest()
    solution = hashlib.sha256((FIXTURES / "f1" / "solution.patch").read_bytes()).hexdigest()
    assert _milestones(fake_dir) == ["up", ("apply", seed), ("apply", solution), "run", "rm", "down"]
    compose = str(FIXTURES / "f1" / "environment" / "docker-compose.yaml")
    project = "omp-harbor-interactive-f1"
    calls = _calls(fake_dir)
    assert calls[0]["argv"] == [
        str(DOCKER),
        "compose",
        "-f",
        compose,
        "-p",
        project,
        "up",
        "-d",
        "workservice",
        "worker",
    ]
    assert calls[-1]["argv"] == [str(DOCKER), "compose", "-f", compose, "-p", project, "down", "-v"]
    run_argv = next(record["argv"] for record in calls if record["argv"][1] == "run")
    assert f"container:{WS}" in run_argv
    assert "omp-workservice:dev" in run_argv
    assert "9090" in run_argv
    sidecar = hashlib.sha256("\0".join(run_argv).encode()).hexdigest()[:12]
    assert next(record["argv"] for record in calls if record["argv"][1] == "rm")[1:] == ["rm", "-f", sidecar]


def test_privileged_worker_returns_2_without_a_docker_call(
    fake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_HARBOR_BEARER", BEARER)
    monkeypatch.setenv("OMP_HARBOR_WORKSPACE_ID", WORKSPACE)
    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / "f1", root / "f1")
    path = root / "f1" / "environment" / "docker-compose.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["services"]["worker"]["privileged"] = True
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    code = run_interactive_env("f1", tmp_path / "out", fixtures_root=root, docker=str(DOCKER))

    assert code == 2
    assert not (fake_dir / "calls.jsonl").exists()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("keep", ["bearer", "workspace", "neither"])
def test_missing_credential_returns_2_without_a_docker_call(
    fake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep: str,
) -> None:
    monkeypatch.delenv("OMP_HARBOR_BEARER", raising=False)
    monkeypatch.delenv("OMP_HARBOR_WORKSPACE_ID", raising=False)
    if keep == "bearer":
        monkeypatch.setenv("OMP_HARBOR_BEARER", BEARER)
    elif keep == "workspace":
        monkeypatch.setenv("OMP_HARBOR_WORKSPACE_ID", WORKSPACE)

    code = run_interactive_env("f1", tmp_path / "out", fixtures_root=FIXTURES, docker=str(DOCKER))

    assert code == 2
    assert not (fake_dir / "calls.jsonl").exists()


def test_cli_unknown_fixture_returns_2(tmp_path: Path) -> None:
    code = main(["--fixture", "missing", "--out", str(tmp_path / "out"), "--fixtures-root", str(tmp_path)])
    assert code == 2
    assert not (tmp_path / "out").exists()


def _responses(execution: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        WS: {"/": {"status": 404, "body": {"error": "not_found"}}},
        WORKER: {
            "/v1/health/ready": {"status": 200, "body": HEALTH},
            f"/v1/workspaces/{WORKSPACE}/execution": {"status": 200, "body": execution},
            f"/v1/work-items/{WORK_ID}": {"status": 200, "body": item},
        },
    }


def _prepare_workspace(fake_dir: Path) -> None:
    repo = fake_dir / "root" / "workspace"
    target = repo / "session-system" / "extensions" / "workflow" / "work.ts"
    target.parent.mkdir(parents=True)
    shutil.copyfile(WORK_TS, target)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {
        "containers": {"workservice": [WS], "worker": [WORKER]},
        "fail": [],
        "omp": [],
        "responses": {},
    }
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _calls(fake_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (fake_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def _milestones(fake_dir: Path) -> list[object]:
    found: list[object] = []
    for record in _calls(fake_dir):
        argv = record["argv"]
        command = argv[1]
        if command == "compose" and "up" in argv:
            found.append("up")
        elif command == "exec" and "apply" in argv:
            found.append(("apply", record["stdin_sha256"]))
        elif command == "run" and "-d" in argv:
            found.append("run")
        elif command == "rm":
            found.append("rm")
        elif command == "compose" and "down" in argv:
            found.append("down")
    return found
