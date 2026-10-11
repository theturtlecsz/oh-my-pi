"""OmpRpcAgent plan arms: OMP_HARBOR_PLAN + OMP_HARBOR_ARM drive one arm.

Arms come from the real ``plans/h5-fleet9-v1.json``: A and B carry
``{instruction}`` with different settings, F carries ``/execute {key}`` whose
``{key}`` is the sole item in ``fixtures/f1/ledger-seed.json``, and C is
unavailable. Every docker interaction goes through ``fake_docker.py``; the
``omp`` shim execs ``fake_omp_rpc.py``, which records every inbound command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import pytest
import yaml
from harbor.models.agent.context import AgentContext

from omp_harbor_eval.evidence import load_evidence
from omp_harbor_eval.harbor_agent import OmpRpcAgent, worker_settings_yml
from omp_harbor_eval.run_plan import load_plan, schedule

TESTS = Path(__file__).resolve().parent
DOCKER = TESTS / "fake_docker.py"
FAKE_RPC = TESTS / "fake_omp_rpc.py"
FIXTURES = TESTS.parent / "fixtures"
PLAN = TESTS.parent / "plans" / "h5-fleet9-v1.json"

WORKER = "w1"
WORKSERVICE = "ws1"
BEARER = "harbor-test-token"
WORKSPACE_ID = "00000000-0000-4000-8000-0000000000aa"
WORK_ID = "00000000-0000-4000-8000-0000000000bb"
INSTRUCTION = "repair the fixture"

F1_WORK = "session-system/extensions/workflow/work.ts"
F1_HEAD = "\t\t\tconst scope = fields.scope !== undefined ? fields.scope.trim() : previous.scope;"

_F1_FILLER = 1596


def test_fleet_plan_schedules_both_repetitions_and_task_scopes() -> None:
    """Fleet runs every task/model/arm twice, with long-horizon and availability filters."""
    plan = load_plan(PLAN)
    trials = schedule(plan)
    assert plan.manifest is not None
    tasks = plan.manifest["tasks"]
    all_ids = set(tasks)
    long_horizon_ids = {task_id for task_id, task in tasks.items() if task.get("long_horizon") is True}
    assert long_horizon_ids and long_horizon_ids < all_ids

    assert (plan.max_input_tokens, plan.max_output_tokens, plan.trial_timeout_s) == (2000000, 100000, 3600)
    assert {experiment.id: experiment.tasks for experiment in plan.experiments} == {
        "arms": "all",
        "routing": "all",
        "long_horizon": "long_horizon",
    }
    assert {experiment.id: set(experiment.arms) for experiment in plan.experiments} == {
        "arms": set("ABCDEFG"), "routing": {"F"}, "long_horizon": {"F"},
    }
    for experiment in plan.experiments:
        assert experiment.repetitions == 2
        assert all(model.thinking == "high" for model in experiment.models)

    repetitions = defaultdict(Counter)
    for trial in trials:
        assert trial.repetition in (1, 2)
        assert trial.model.thinking == "high"
        key = (trial.experiment_id, trial.task_id, trial.arm_id, trial.model.provider, trial.model.id)
        repetitions[key][trial.repetition] += 1
    expected_keys = {
        (experiment.id, task_id, arm_id, model.provider, model.id)
        for experiment in plan.experiments
        for task_id in (long_horizon_ids if experiment.id == "long_horizon" else all_ids)
        for arm_id in experiment.arms
        for model in experiment.models
    }
    assert set(repetitions) == expected_keys
    assert all(counts == Counter({1: 1, 2: 1}) for counts in repetitions.values())

    for experiment_id, expected_ids in (
        ("arms", all_ids), ("routing", all_ids), ("long_horizon", long_horizon_ids)
    ):
        assert {trial.task_id for trial in trials if trial.experiment_id == experiment_id} == expected_ids

    available = [trial for trial in trials if trial.status == "available"]
    assert sum(trial.experiment_id == "routing" for trial in available) == 180
    arm_counts = Counter(trial.arm_id for trial in available if trial.experiment_id == "arms")
    assert {arm_id: arm_counts[arm_id] for arm_id in "ABCDEFG"} == {
        "A": 60, "B": 60, "C": 0, "D": 0, "E": 0, "F": 60, "G": 0,
    }
    long_horizon_count = sum(trial.experiment_id == "long_horizon" for trial in available)
    assert long_horizon_count == 4 * len(long_horizon_ids)
    assert long_horizon_count >= 24

    assert set(plan.arms_by_id) == set("ABCDEFG")
    for arm_id in ("A", "B"):
        arm = plan.arms_by_id[arm_id]
        assert arm.status == "available"
        assert arm.command == "{instruction}"
        assert arm.to_dict()["terminal"] == {"agent_end": True}
        assert "extension-module:work-now" in arm.settings["disabledExtensions"]
    assert plan.arms_by_id["B"].settings["modelRoles"]["plan"] == "kimi-code/k3-256k"

    execute_terminal = {"pointer": "/execution/grant/state", "in": ["completed", "stopped", "canceled"]}
    reasons = {
        "C": "no intake/approved plan mechanism",
        "D": "no self-review mechanism",
        "E": "no independent audit mechanism",
        "G": "no typed contained harness mechanism",
    }
    for arm_id in ("F", "C", "D", "E", "G"):
        arm = plan.arms_by_id[arm_id]
        assert arm.command == "/execute {key}"
        assert arm.to_dict()["terminal"] == execute_terminal
        assert arm.status == ("available" if arm_id == "F" else "unavailable")
        if arm_id in reasons:
            assert arm.reason == reasons[arm_id]
    for trial in trials:
        assert trial.status == ("unavailable" if trial.arm_id in reasons else "available")
        if trial.arm_id in reasons:
            assert trial.reason == reasons[trial.arm_id]


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
    monkeypatch.delenv("OMP_HARBOR_PLAN", raising=False)
    monkeypatch.delenv("OMP_HARBOR_ARM", raising=False)
    return directory


@pytest.fixture
def session_dir() -> Path:
    directory = Path("/dev/shm") / f"omp-harbor-arms-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
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


def _omp_argv(
    record: Path,
    session_file: Path,
    evidence_dir: Path,
    session_id: str,
    *,
    events: Path | None = None,
) -> list[str]:
    argv = [
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
    if events is not None:
        argv.extend(["--events", str(events)])
    return argv


def _run_arm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dir: Path,
    session_dir: Path,
    *,
    environment_dir: Path,
    arm: str,
    events: Path | None = None,
) -> tuple[AgentContext, Path]:
    """Run one arm of the real plan against fixture f1; return context and record dir."""

    monkeypatch.setenv("OMP_HARBOR_PLAN", str(PLAN))
    monkeypatch.setenv("OMP_HARBOR_ARM", arm)
    session_id = f"sess-arm-{arm.lower()}"
    evidence_root = tmp_path / f"evidence-{arm.lower()}"
    record = tmp_path / f"rpc-record-{arm.lower()}"
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(
            record,
            session_dir / f"session-{arm.lower()}.jsonl",
            evidence_root / "f1-known_good",
            session_id,
            events=events,
        ),
        responses=_rpc_responses(),
    )
    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(evidence_root))
    agent = OmpRpcAgent(logs_dir=tmp_path / f"logs-{arm.lower()}", docker=str(DOCKER))
    context = AgentContext()
    asyncio.run(agent.run(INSTRUCTION, FakeEnv(session_id, environment_dir), context))
    return context, record


def _prompts(record: Path) -> list[str]:
    path = record / "commands.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row["command"]["message"] for row in rows if row["command"].get("type") == "prompt"]


def _config_bytes(fake_dir: Path) -> bytes:
    return (fake_dir / "root" / "home" / "agent" / ".omp" / "agent" / "config.yml").read_bytes()


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()


def _repo(fake_dir: Path) -> Path:
    repo = fake_dir / "root" / "workspace"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write_f1_source(repo: Path) -> None:
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


def _seeded_f1(tmp_path: Path, fake_dir: Path) -> Path:
    environment_dir = _fixture_copy(tmp_path, "f1")
    _write_f1_source(_repo(fake_dir))
    return environment_dir


def test_arms_a_and_b_write_different_configs_with_scripted_audit(
    fake_dir: Path,
    session_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _seeded_f1(tmp_path, fake_dir)
    events = tmp_path / "events.json"
    events.write_text(json.dumps([{"type": "agent_end"}]), encoding="utf-8")

    configs: dict[str, bytes] = {}
    for arm in ("A", "B"):
        context, record = _run_arm(
            monkeypatch,
            tmp_path,
            fake_dir,
            session_dir,
            environment_dir=environment_dir,
            arm=arm,
            events=events,
        )
        configs[arm] = _config_bytes(fake_dir)
        assert _prompts(record) == [INSTRUCTION]
        assert context.metadata is not None
        loaded = load_evidence(Path(context.metadata["evidence_dir"]), context.metadata["manifest_sha256"])
        assert loaded.read_json("arm.json") == {
            "plan_id": "h5-fleet9-v1",
            "arm": arm,
            "command": INSTRUCTION,
            "terminal": {"agent_end": True},
            "settings_sha256": hashlib.sha256(configs[arm]).hexdigest(),
        }

    assert configs["A"] != configs["B"]
    for payload in configs.values():
        settings = yaml.safe_load(payload)
        assert settings["modelRoles"]["audit"] == "scripted/scripted"
    assert "plan" not in yaml.safe_load(configs["A"])["modelRoles"]
    assert yaml.safe_load(configs["B"])["modelRoles"]["plan"] == "kimi-code/k3-256k"


def test_arm_f_executes_the_sole_ledger_seed_key(
    fake_dir: Path,
    session_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _seeded_f1(tmp_path, fake_dir)

    context, record = _run_arm(monkeypatch, tmp_path, fake_dir, session_dir, environment_dir=environment_dir, arm="F")

    assert _prompts(record) == ["/execute OMP-1"]
    config = _config_bytes(fake_dir)
    assert yaml.safe_load(config) == {"modelRoles": {"audit": "scripted/scripted"}}
    assert context.metadata is not None
    loaded = load_evidence(Path(context.metadata["evidence_dir"]), context.metadata["manifest_sha256"])
    assert loaded.read_json("arm.json") == {
        "plan_id": "h5-fleet9-v1",
        "arm": "F",
        "command": "/execute OMP-1",
        "terminal": {"pointer": "/execution/grant/state", "in": ["completed", "stopped", "canceled"]},
        "settings_sha256": hashlib.sha256(config).hexdigest(),
    }


def test_unavailable_arm_raises_before_any_docker_call(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _fixture_copy(tmp_path, "f1")
    monkeypatch.setenv("OMP_HARBOR_PLAN", str(PLAN))
    monkeypatch.setenv("OMP_HARBOR_ARM", "C")
    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(agent.run(INSTRUCTION, FakeEnv("sess-arm-c", environment_dir), AgentContext()))
    assert not (fake_dir / "calls.jsonl").exists()


def test_unknown_arm_raises_before_any_docker_call(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _fixture_copy(tmp_path, "f1")
    monkeypatch.setenv("OMP_HARBOR_PLAN", str(PLAN))
    monkeypatch.setenv("OMP_HARBOR_ARM", "Z")
    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    with pytest.raises(ValueError, match="no arm 'Z'"):
        asyncio.run(agent.run(INSTRUCTION, FakeEnv("sess-arm-z", environment_dir), AgentContext()))
    assert not (fake_dir / "calls.jsonl").exists()


@pytest.mark.parametrize(
    ("plan_value", "arm_value"),
    [
        ("plan", None),
        (None, "A"),
        ("plan", ""),
        ("", "A"),
        ("", ""),
        ("", None),
        (None, ""),
    ],
    ids=[
        "plan-only",
        "arm-only",
        "arm-empty",
        "plan-empty",
        "both-empty",
        "plan-empty-arm-unset",
        "plan-unset-arm-empty",
    ],
)
def test_lone_or_empty_arm_env_raises_before_any_docker_call(
    fake_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plan_value: str | None,
    arm_value: str | None,
) -> None:
    environment_dir = _fixture_copy(tmp_path, "f1")
    if plan_value is not None:
        monkeypatch.setenv("OMP_HARBOR_PLAN", str(PLAN) if plan_value == "plan" else plan_value)
    if arm_value is not None:
        monkeypatch.setenv("OMP_HARBOR_ARM", arm_value)
    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    with pytest.raises(ValueError, match="OMP_HARBOR_PLAN"):
        asyncio.run(agent.run(INSTRUCTION, FakeEnv("sess-lone", environment_dir), AgentContext()))
    assert not (fake_dir / "calls.jsonl").exists()


def test_unset_arm_variables_keep_worker_settings_and_skip_arm_json(
    fake_dir: Path,
    session_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_dir = _seeded_f1(tmp_path, fake_dir)
    session_id = "sess-no-arm"
    evidence_root = tmp_path / "evidence"
    record = tmp_path / "rpc-record"
    _write_config(
        fake_dir,
        containers={"worker": [WORKER], "workservice": [WORKSERVICE]},
        omp=_omp_argv(record, session_dir / "session.jsonl", evidence_root / "f1-known_good", session_id),
        responses=_rpc_responses(),
    )
    monkeypatch.setenv("OMP_HARBOR_VARIANT", "known_good")
    monkeypatch.setenv("OMP_HARBOR_EVIDENCE_ROOT", str(evidence_root))
    agent = OmpRpcAgent(logs_dir=tmp_path / "logs", docker=str(DOCKER))
    context = AgentContext()
    asyncio.run(agent.run(INSTRUCTION, FakeEnv(session_id, environment_dir), context))

    assert _config_bytes(fake_dir) == worker_settings_yml().encode("utf-8")
    assert context.metadata is not None
    loaded = load_evidence(Path(context.metadata["evidence_dir"]), context.metadata["manifest_sha256"])
    assert "arm.json" not in loaded.files
