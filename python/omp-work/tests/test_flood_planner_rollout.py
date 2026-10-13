"""Execute the composed planner rollout candidate, never the selected external source.

Set OMP584_FLOOD_SOURCE explicitly. A missing source skips every node; rollout
qualification must supply it and run them all.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[3] / "infra" / "automation-user"
PROMPT_PATCH = ASSETS / "omp584-prompt.patch"
if not PROMPT_PATCH.exists():
    PROMPT_PATCH = ASSETS / "omp584-planner-prompt.patch"
PATCHES = [ASSETS / "omp584-dependencies.patch", ASSETS / "omp584-start-metrics.patch", PROMPT_PATCH]
RULES = "omp584-planner-rules.md"
SPEC_RULES = "planner-independence.md"
ITEM = "OMP-584"


def command(argv: list[str], cwd: Path):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)


def compose(directory: Path, patches: list[Path]) -> None:
    """Reverse only fully reverse-applicable patches, then check and apply each forward."""
    for patch in reversed(patches):
        reverse = ["git", "apply", "--reverse", str(patch)]
        if command([*reverse, "--check"], directory).returncode == 0:
            result = command(reverse, directory)
            assert result.returncode == 0, result.stderr
    for patch in patches:
        result = command(["git", "apply", "--check", str(patch)], directory)
        assert result.returncode == 0, f"{patch.name}: {result.stderr}"
        result = command(["git", "apply", str(patch)], directory)
        assert result.returncode == 0, result.stderr


def snapshot(directory: Path):
    return {p.relative_to(directory): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in directory.rglob("*") if p.is_file() and p.suffix in (".py", ".pyc", ".md")}


def init_repo(directory: Path) -> None:
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "isolated fixture"],
    ):
        result = command(argv, directory)
        assert result.returncode == 0, result.stderr


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    if not selected:
        pytest.skip("rollout qualification requires explicit OMP584_FLOOD_SOURCE")
    source = Path(selected).expanduser().resolve()
    assert (source / "flood.py").is_file(), f"no flood.py in {source}"
    originals = snapshot(source)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    directory = tmp_path / "candidate"
    directory.mkdir()
    for p in source.iterdir():
        if p.is_file() and p.suffix in (".py", ".md"):
            shutil.copy2(p, directory / p.name)
    for name in (RULES, SPEC_RULES):
        shutil.copyfile(ASSETS / RULES, directory / name)
    init_repo(directory)
    compose(directory, PATCHES)
    path = directory / "flood.py"
    compile(path.read_bytes(), str(path), "exec")
    try:
        yield path
    finally:
        assert snapshot(source) == originals, "selected source or bytecode changed"


def slice_text(n: int, depends_on: str, files: str, body: str) -> str:
    return (f"---\nid: {ITEM}-s0{n}\nfiles: [{files}]\ndepends_on: {depends_on}\n---\n"
            f"{body}\nExtend tests/test_{ITEM.replace('-', '_')}_s0{n}.py.\n"
            "Done when: the new test passes.\n")


def check_plan(path: Path, tmp_path: Path, slices: list[str]):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    init_repo(repo)
    cfg_dir = tmp_path / "config"
    pdir = cfg_dir / "tasks" / "proposed" / ITEM
    pdir.mkdir(parents=True)
    for n, text in enumerate(slices, 1):
        (pdir / f"{ITEM}-s0{n}.md").write_text(text)
    cfg_file = cfg_dir / "flood.json"
    cfg_file.write_text(json.dumps({"repo": str(repo), "providers": {}, "control_files": [],
                                    "worktrees_dir": str(tmp_path / "worktrees"),
                                    "rider_dir": str(tmp_path / "rider")}))
    return command([sys.executable, "-B", str(path), "check-plan", ITEM, "--config", str(cfg_file)],
                   path.parent)


@pytest.mark.parametrize("section", ["Do not touch", "Assumptions", "Deferred"])
def test_combined_check_plan_dependency_boundaries(candidate, tmp_path, section):
    """Mentions inside excluded sections must not justify a depends_on; real reads still do."""
    producer = "src/producer.py"
    first = slice_text(1, "[]", producer, f"Implement {producer}.\nDo not touch: unrelated files.")
    excluded = f"Implement src/consumer.py.\n{section}: {producer} belongs to {ITEM}-s01.\n"
    if section != "Do not touch":
        excluded += "Do not touch: unrelated files.\n"

    ordered = check_plan(candidate, tmp_path / "a", [
        first, slice_text(2, f"[{ITEM}-s01]", "src/consumer.py", excluded)])
    assert ordered.returncode == 1, ordered.stdout + ordered.stderr
    assert f"{ITEM}-s02: depends_on {ITEM}-s01 orders the slice only" in ordered.stdout
    assert "names no test file" not in ordered.stdout

    independent = check_plan(candidate, tmp_path / "b", [
        first, slice_text(2, "[]", "src/consumer.py", excluded)])
    assert independent.returncode == 0, independent.stdout + independent.stderr
    assert "OK: 2 slices" in independent.stdout

    reads = check_plan(candidate, tmp_path / "c", [
        first, slice_text(2, f"[{ITEM}-s01]", "src/consumer.py",
                          f"Implement src/consumer.py; it reads {producer}.\n"
                          "Do not touch: unrelated files.\n")])
    assert reads.returncode == 0, reads.stdout + reads.stderr


def test_combined_candidate_compiles_and_requalifies(candidate):
    """Composing an installed candidate again must be a byte-identical no-op."""
    before = candidate.read_bytes()
    compose(candidate.parent, PATCHES)
    assert candidate.read_bytes() == before
    compile(candidate.read_bytes(), str(candidate), "exec")
