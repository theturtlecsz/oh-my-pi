"""OmpRpcAgent gives the worker an ``@audit`` model role (OMP-463).

``worker_settings_yml`` sets ``modelRoles.audit`` to a provider/model id that
``scripted_model.models_yml`` defines, and ``OmpRpcAgent.run`` writes those
bytes to ``<home>/.omp/agent/config.yml`` so the native auditor runner resolves
``@audit`` instead of failing with "Could not resolve @audit role".
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml
from harbor.models.agent.context import AgentContext

from omp_harbor_eval.harbor_agent import OmpRpcAgent, worker_settings_yml
from omp_harbor_eval.scripted_model import models_yml

TESTS = Path(__file__).resolve().parent
DOCKER = TESTS / "fake_docker.py"
FAKE_RPC = TESTS / "fake_omp_rpc.py"
FIXTURES = TESTS.parent / "fixtures"

WORKER = "w1"
WORKSERVICE = "ws1"
BEARER = "harbor-test-token"
WORKSPACE_ID = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"

F1_WORK = "session-system/extensions/workflow/work.ts"
F1_HEAD = "\t\t\tconst scope = fields.scope !== undefined ? fields.scope.trim() : previous.scope;"

_F1_FILLER = 1596


class FakeEnv:
    def __init__(self, session_id: str, environment_dir: Path) -> None:
        self.session_id = session_id
        self.environment_dir = environment_dir


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "docker"
    directory.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    monkeypatch.setenv("OMP_HARBOR_BEARER", BEARER)
    monkeypatch.setenv("OMP_HARBOR_WORKSPACE_ID", WORKSPACE_ID)
    return directory


@pytest.fixture
def session_file() -> Path:
    directory = Path("/dev/shm") / f"omp-harbor-session-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory / "session.jsonl"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_worker_settings_audit_role_names_a_model_models_yml_defines() -> None:
    settings = yaml.safe_load(worker_settings_yml())
    selector = settings["modelRoles"]["audit"]
    provider_id, _, model_id = selector.partition("/")
    assert provider_id and model_id

    provider = yaml.safe_load(models_yml("http://127.0.0.1:8123/v1"))["providers"][provider_id]
    assert model_id in [model["id"] for model in provider["models"]]


def test_run_writes_the_worker_settings_config_yml(
    fake_dir: Path,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "sess-f1-good"
    environment_dir = _fixture_copy(tmp_path, "f1")
    evidence_root = tmp_path / "evidence"
    evidence_dir = evidence_root / "f1-known_good"
    repo = _repo(fake_dir)
    _write_source(repo, "f1")
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(tmp_path / "rpc-record", session_file, evidence_dir, session_id),
        responses=_rpc_responses(),
    )

    _run_agent(
        monkeypatch,
        environment_dir=environment_dir,
        session_id=session_id,
        variant="known_good",
        evidence_root=evidence_root,
        logs_dir=tmp_path / "logs",
    )

    config = fake_dir / "root" / "home" / "agent" / ".omp" / "agent" / "config.yml"
    assert config.read_bytes() == worker_settings_yml().encode("utf-8")


def _fixture_copy(tmp_path: Path, fixture_id: str) -> Path:
    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / fixture_id, root / fixture_id)
    return root / fixture_id / "environment"


def _write_config(
    fake_dir: Path,
    *,
    containers: dict[str, list[str]],
    omp: list[str],
    responses: dict[str, dict[str, dict[str, object]]],
) -> None:
    config = {"containers": containers, "fail": [], "omp": omp, "responses": responses}
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _rpc_responses() -> dict[str, dict[str, dict[str, object]]]:
    return {
        WORKER: {
            "/v1/health/ready": {"status": 200, "body": {"live": True, "ready": True}},
            f"/v1/workspaces/{WORKSPACE_ID}/execution": {
                "status": 200,
                "body": {
                    "grant": {"state": "completed"},
                    "items": [{"work_id": WORK_ID, "close_attempts_started": 1}],
                    "active_item": {"work_id": WORK_ID},
                },
            },
            f"/v1/work-items/{WORK_ID}": {
                "status": 200,
                "body": {
                    "work_id": WORK_ID,
                    "state": "completed",
                    "revision": {"revision_number": 2},
                },
            },
        },
        WORKSERVICE: {"/": {"status": 404, "body": {"error": "not_found"}}},
    }


def _omp_argv(record: Path, session_file: Path, evidence_dir: Path, session_id: str) -> list[str]:
    return [
        sys.executable,
        str(FAKE_RPC),
        "--record",
        str(record),
        "--session-file",
        str(session_file),
        "--evidence",
        str(evidence_dir),
        "--session-id",
        session_id,
    ]


def _run_agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    environment_dir: Path,
    session_id: str,
    variant: str,
    evidence_root: Path,
    logs_dir: Path,
) -> AgentContext:
    monkeypatch.setenv("OMP_HARBOR_VARIANT", variant)
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(evidence_root))
    agent = OmpRpcAgent(logs_dir=logs_dir, docker=str(DOCKER))
    context = AgentContext()
    asyncio.run(agent.run("repair the fixture", FakeEnv(session_id, environment_dir), context))
    return context


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    return completed.stdout


def _repo(fake_dir: Path) -> Path:
    repo = fake_dir / "root" / "workspace"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write_source(repo: Path, fixture_id: str) -> None:
    assert fixture_id == "f1"
    lines = ["// filler 0"] * _F1_FILLER + [
        "\t\t\t}",
        "\t\t\tconst title = (fields.title ?? previous.title).trim();",
        "\t\t\tconst description = fields.description ?? previous.description;",
        F1_HEAD,
        "\t\t\tconst acceptance_criteria = fields.acceptance_criteria ?? fields.criteria ?? previous.acceptance_criteria;",
        "\t\t\tconst contentSha = payloadHash({ title, description, scope, acceptance_criteria });",
        "\t\t\tconst revision = {",
        '\t\t\t\trevision_id: stableId("revision", item.work_id, previous.revision_id, contentSha),',
    ]
    target = repo / F1_WORK
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
