"""OmpRpcAgent: fixture staging, docker-only RPC argv, sealed evidence, bundle.

Every interaction goes through ``fake_docker.py``: ``ps -q`` resolves the worker
and workservice containers, ``exec python -c SCRIPT URL`` is answered from the
canned ``responses`` table (so ``ExecProbe`` and the model sidecar readiness
poll never open a socket), and the ``omp`` shim execs ``fake_omp_rpc.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from harbor.models.agent.context import AgentContext

from omp_harbor_eval.evidence import load_evidence
from omp_harbor_eval.harbor_agent import OmpRpcAgent

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
F1_SEEDED = "\t\t\tconst scope = previous.scope;"
F2_HOST = "session-system/extensions/workflow/host.ts"
F2_HEAD = "\t\t\t\tif (matches || legacyDelivered) {"
F2_SEEDED = '\t\t\t\tif (matches || legacyDelivered || (data.status === "queued" && !persisted)) {'

_F1_FILLER = 1596
_F2_FILLER = 2301


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


def _calls(fake_dir: Path) -> list[dict[str, object]]:
    path = fake_dir / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _argv_of(record: dict[str, object]) -> list[str]:
    argv = record["argv"]
    assert isinstance(argv, list)
    return [str(token) for token in argv]


def _omp_rows(fake_dir: Path) -> list[list[str]]:
    path = fake_dir / "omp-argv.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line)["argv"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def _apply_calls(fake_dir: Path, workdir: str) -> list[dict[str, object]]:
    return [
        record
        for record in _calls(fake_dir)
        if _argv_of(record)[1:] == ["exec", "-i", WORKER, "git", "-C", workdir, "apply", "-"]
    ]


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
    if fixture_id == "f1":
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
    else:
        lines = ["// filler 0"] * _F2_FILLER + [
            "\t\t\t\tif (matches && persisted && data.sessionId && data.workId && data.revisionId) {",
            "\t\t\t\t\tpersistedIntents.push({ entryId: persisted.entryId, intent: data });",
            "\t\t\t\t}",
            F2_HEAD,
            "\t\t\t\t\tdeliveredPreReservations.add(`${data.grantId}:${data.preReservationVersion}`);",
            "\t\t\t\t\tdeliveredPostVersions.add(`${data.grantId}:${data.postVersion}`);",
            "\t\t\t\t} else {",
        ]
        target = repo / F2_HOST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _seed_sha(environment_dir: Path) -> str:
    return hashlib.sha256((environment_dir.parent / "seed.patch").read_bytes()).hexdigest()


def _clone_bundle(tmp_path: Path, loaded) -> Path:
    bundle = tmp_path / f"bundle-{uuid.uuid4().hex}"
    bundle.write_bytes(loaded.read_bytes("worker-repo.bundle"))
    clone = tmp_path / f"clone-{uuid.uuid4().hex}"
    cloned = subprocess.run(["git", "clone", str(bundle), str(clone)], capture_output=True, text=True, check=False)
    assert cloned.returncode == 0, cloned.stderr
    return clone


def test_known_good_applies_seed_then_solution_and_seals_a_clonable_bundle(
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
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(evidence_root))
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(tmp_path / "rpc-record", session_file, evidence_dir, session_id),
        responses=_rpc_responses(),
    )

    context = _run_agent(
        monkeypatch,
        environment_dir=environment_dir,
        session_id=session_id,
        variant="known_good",
        evidence_root=evidence_root,
        logs_dir=tmp_path / "logs",
    )

    applies = _apply_calls(fake_dir, "/workspace")
    assert [record["stdin_sha256"] for record in applies] == [
        _seed_sha(environment_dir),
        hashlib.sha256((environment_dir.parent / "solution.patch").read_bytes()).hexdigest(),
    ]

    rpc_args = [
        "omp",
        "--mode",
        "rpc",
        "--model",
        "scripted/scripted",
        "--session-dir",
        f"{fake_dir}/root/home/agent/omp-sessions",
    ]
    assert _omp_rows(fake_dir) == [rpc_args]

    models_call = next(
        record
        for record in _calls(fake_dir)
        if _argv_of(record)[1] == "exec" and "mkdir -p" in " ".join(_argv_of(record))
    )
    written = models_call["stdin_sha256"]
    assert written == hashlib.sha256(
        (fake_dir / "root" / "home" / "agent" / ".omp" / "agent" / "models.yml").read_bytes()
    ).hexdigest()

    assert context.metadata is not None
    metadata = context.metadata
    assert metadata["evidence_dir"] == str(evidence_dir)
    assert metadata["outcome"] == "completed"
    assert not Path(metadata["evidence_dir"]).is_relative_to(environment_dir)

    loaded = load_evidence(evidence_dir, metadata["manifest_sha256"])
    assert loaded.fixture_id == "f1"
    assert loaded.variant == "known_good"
    assert loaded.run_id == session_id
    assert loaded.read_json("run.json")["experiment"] == "repair"
    assert json.loads((tmp_path / "rpc-record" / "eof.json").read_text(encoding="utf-8")) == {
        "manifest_exists": True
    }

    clone = _clone_bundle(tmp_path, loaded)
    assert F1_HEAD in (clone / F1_WORK).read_text(encoding="utf-8")
    assert F1_SEEDED not in (clone / F1_WORK).read_text(encoding="utf-8")


def test_known_bad_applies_only_the_seed(
    fake_dir: Path,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "sess-f1-bad"
    environment_dir = _fixture_copy(tmp_path, "f1")
    evidence_root = tmp_path / "evidence"
    evidence_dir = evidence_root / "f1-known_bad"
    repo = _repo(fake_dir)
    _write_source(repo, "f1")
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(tmp_path / "rpc-record", session_file, evidence_dir, session_id),
        responses=_rpc_responses(),
    )

    context = _run_agent(
        monkeypatch,
        environment_dir=environment_dir,
        session_id=session_id,
        variant="known_bad",
        evidence_root=evidence_root,
        logs_dir=tmp_path / "logs",
    )

    applies = _apply_calls(fake_dir, "/workspace")
    assert [record["stdin_sha256"] for record in applies] == [_seed_sha(environment_dir)]

    assert context.metadata is not None
    loaded = load_evidence(evidence_dir, context.metadata["manifest_sha256"])
    assert loaded.variant == "known_bad"
    clone = _clone_bundle(tmp_path, loaded)
    content = (clone / F1_WORK).read_text(encoding="utf-8")
    assert F1_SEEDED in content
    assert F1_HEAD not in content


def test_kill_at_stops_the_worker_pid_and_resumes_with_the_session(
    fake_dir: Path,
    session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = "sess-f2"
    environment_dir = _fixture_copy(tmp_path, "f2")
    evidence_root = tmp_path / "evidence"
    evidence_dir = evidence_root / "f2-known_good"
    repo = _repo(fake_dir)
    _write_source(repo, "f2")
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(tmp_path / "rpc-record", session_file, evidence_dir, session_id),
        responses=_rpc_responses(),
    )

    context = _run_agent(
        monkeypatch,
        environment_dir=environment_dir,
        session_id=session_id,
        variant="known_good",
        evidence_root=evidence_root,
        logs_dir=tmp_path / "logs",
    )

    kills = [
        _argv_of(record)
        for record in _calls(fake_dir)
        if _argv_of(record)[1:5] == ["exec", WORKER, "sh", "-c"] and "kill -KILL" in _argv_of(record)[5]
    ]
    assert len(kills) == 1

    rows = _omp_rows(fake_dir)
    assert len(rows) == 2
    assert rows[0] == [
        "omp",
        "--mode",
        "rpc",
        "--model",
        "scripted/scripted",
        "--session-dir",
        f"{fake_dir}/root/home/agent/omp-sessions",
    ]
    assert rows[1][-2:] == ["--session", str(session_file)]

    assert context.metadata is not None
    assert context.metadata["outcome"] == "completed"
    loaded = load_evidence(evidence_dir, context.metadata["manifest_sha256"])
    assert loaded.read_json("session.json")["restarts"] == [{"id": session_id, "file": str(session_file)}]
    clone = _clone_bundle(tmp_path, loaded)
    content = (clone / F2_HOST).read_text(encoding="utf-8")
    assert F2_HEAD in content
    assert F2_SEEDED not in content


def test_variant_must_be_known_good_or_known_bad(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _fixture_copy(tmp_path, "f1")
    for variant in ("candidate", "invalid_variant"):
        monkeypatch.setenv("OMP_HARBOR_VARIANT", variant)
        monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
        agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
        with pytest.raises(ValueError, match="OMP_HARBOR_VARIANT"):
            asyncio.run(
                agent.run("repair", FakeEnv("sess-variant", environment_dir), AgentContext())
            )
    assert not (fake_dir / "calls.jsonl").exists()


def test_privileged_worker_raises_before_any_docker_call(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import yaml

    environment_dir = _fixture_copy(tmp_path, "f1")
    compose_path = environment_dir / "docker-compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    document["services"]["worker"]["privileged"] = True
    compose_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    with pytest.raises(ValueError, match="worker is privileged"):
        asyncio.run(agent.run("repair", FakeEnv("sess-privileged", environment_dir), AgentContext()))

    assert not (fake_dir / "calls.jsonl").exists()


def test_bad_seed_raises_before_any_shell_call(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _fixture_copy(tmp_path, "f1")
    (environment_dir.parent / "seed.patch").write_bytes(b"this is not a patch\n")
    repo = _repo(fake_dir)
    _write_source(repo, "f1")
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(tmp_path / "rpc-record", tmp_path / "session.jsonl", tmp_path / "evidence", "sess-bad-seed"),
        responses=_rpc_responses(),
    )

    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    with pytest.raises(Exception, match="No valid patches"):
        asyncio.run(agent.run("repair", FakeEnv("sess-bad-seed", environment_dir), AgentContext()))

    applies = _apply_calls(fake_dir, "/workspace")
    assert [record["stdin_sha256"] for record in applies] == [hashlib.sha256(b"this is not a patch\n").hexdigest()]
    assert not any("sh" in _argv_of(record) for record in _calls(fake_dir))
