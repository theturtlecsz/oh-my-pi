"""F2 durable continuation: seed, repair, Harbor task layout, and fixture load."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from harbor.models.task.task import Task

from omp_harbor_eval.fixtures import fixture_digest, load_fixture
from omp_harbor_eval.isolation import check_topology, load_compose

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "evals" / "harbor" / "fixtures"
SHIPPED_COMPOSE = REPO / "evals" / "harbor" / "topology" / "compose.yaml"
INSTALLED_TARGET = (
    "python/omp-work/tests/test_installed_execution_recovery.py"
    "::test_killed_controller_recovers_queued_resume_continuation"
)
ACK_TARGET = "session-system/tests/f2-continuation-ack.test.ts"


def _run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=check, capture_output=True, text=True)


def _tree(worktree: Path) -> str:
    _run(["git", "add", "-A"], worktree)
    return _run(["git", "write-tree"], worktree).stdout.strip()


def _remove_worktree(worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(REPO), "worktree", "remove", "--force", str(worktree)],
        check=False,
        capture_output=True,
        text=True,
    )
    if worktree.exists():
        shutil.rmtree(worktree)
    subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], check=False, capture_output=True, text=True)


def _copy_fixture_files(worktree: Path, fixture) -> list[Path]:
    copied: list[Path] = []
    for test in fixture.independent_tests:
        for relative in test.files:
            source = fixture.directory / relative
            destination = worktree / "session-system" / "tests" / Path(relative).name
            destination.write_bytes(source.read_bytes())
            copied.append(destination)
    return copied


def _bun(worktree: Path, target: str) -> subprocess.CompletedProcess[str]:
    return _run(["bun", "test", target], worktree, check=False)


def test_fixture_f2_loads_and_seed_reverses(tmp_path: Path) -> None:
    node_modules = REPO / "node_modules"
    if not node_modules.is_dir():
        pytest.fail("node_modules is required to run the seeded session-system test")

    fixture = load_fixture(FIXTURES, "f2")
    assert fixture.id == "f2"
    assert fixture.scored_experiment == "repair"
    assert fixture.seed_patch == "seed.patch"
    assert fixture.solution_patch == "solution.patch"
    assert fixture_digest(fixture.directory) == fixture.digest
    assert fixture_digest(fixture.directory) == fixture_digest(fixture.directory)
    assert len(fixture.digest) == 64

    bun_tests = [item for item in fixture.independent_tests if item.runner == "bun"]
    installed = [item for item in fixture.independent_tests if item.requires]
    assert [item.target for item in bun_tests] == [ACK_TARGET]
    assert bun_tests[0].files == ("independent/f2-continuation-ack.test.ts",)
    assert [(item.runner, item.target, item.requires) for item in installed] == [
        ("pytest", INSTALLED_TARGET, ("OMP_INSTALLED_RELEASE",))
    ]

    assert fixture.scenario.command == "/execute OMP-246"
    assert fixture.scenario.terminal.pointer == "/execution/grant/state"
    assert fixture.scenario.terminal.accepted == ("completed",)
    assert fixture.scenario.kill_at is not None
    assert fixture.scenario.kill_at.match == {"type": "prompt", "message": "/execute OMP-246"}
    pointers = [rule["pointer"] for rule in fixture.rules if rule["type"] == "readback_equals"]
    assert "/execution/grant/continuations_scheduled" in pointers
    assert "/execution/items/0/close_attempts_started" in pointers
    assert any(rule["type"] == "readback_equals" and rule["pointer"] == "/execution/grant/state" and rule["value"] == "completed" for rule in fixture.rules)
    assert any(rule["type"] == "outcome_is" and rule["outcome"] == "completed" for rule in fixture.rules)
    assert any(rule["type"] == "independent_tests_pass" for rule in fixture.rules)

    instruction = (fixture.directory / "instruction.md").read_text(encoding="utf-8")
    assert instruction.strip()
    for leaked in ("host.ts", "legacyDelivered", "seed.patch", "solution.patch"):
        assert leaked not in instruction

    task = Task(fixture.directory)
    assert task.instruction.strip() == instruction.strip()
    assert task.config.metadata["scored_experiment"] == "repair"
    assert task.config.metadata["kill_run"] == "harness_crash_injection"
    assert (fixture.directory / "solution" / "solve.sh").is_file()
    assert (fixture.directory / "tests" / "test.sh").is_file()

    compose_path = fixture.directory / "environment" / "docker-compose.yaml"
    assert compose_path.read_bytes() == SHIPPED_COMPOSE.read_bytes()
    assert check_topology(load_compose(compose_path)) == []

    worktree = tmp_path / "wt"
    _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], REPO)
    try:
        (worktree / "node_modules").symlink_to(node_modules, target_is_directory=True)
        head = _run(["git", "rev-parse", "HEAD^{tree}"], worktree).stdout.strip()
        seed = fixture.directory / fixture.seed_patch
        solution = fixture.directory / fixture.solution_patch
        _run(["git", "apply", "--check", str(seed)], worktree)
        _run(["git", "apply", str(seed)], worktree)
        assert _tree(worktree) != head

        copied = _copy_fixture_files(worktree, fixture)
        seeded = _bun(worktree, ACK_TARGET)
        assert seeded.returncode != 0, seeded.stdout + seeded.stderr
        for path in copied:
            path.unlink()

        _run(["git", "apply", str(solution)], worktree)
        copied = _copy_fixture_files(worktree, fixture)
        repaired = _bun(worktree, ACK_TARGET)
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr
        for path in copied:
            path.unlink()
        assert _tree(worktree) == head
    finally:
        _remove_worktree(worktree)
