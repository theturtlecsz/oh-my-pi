"""Verifier runtime mounts use resolved path boundaries, not substrings."""

from pathlib import Path
import site

import pytest

import omp_work
from omp_work.orchestrator import worker_sandbox


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    installed = tmp_path / "work" / "oh-my-pi" / "python" / "omp-work" / "src"
    package = installed / "omp_work"
    package.mkdir(parents=True)
    monkeypatch.setattr(omp_work, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(site, "getsitepackages", lambda: [])
    worktree = tmp_path / "jobs" / "job"
    worktree.mkdir(parents=True)
    return installed, worktree


def test_install_under_work_component_is_returned(runtime: tuple[Path, Path]) -> None:
    installed, worktree = runtime
    assert worker_sandbox._verifier_runtime_roots(worktree) == (installed.resolve(),)


@pytest.mark.parametrize(
    "kind",
    ["root", "home", "cwd", "relative", "user_site", "work", "work_child",
     "worktree", "worktree_child", "worktree_parent"],
)
def test_unsafe_runtime_root_is_refused(
    runtime: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    _, worktree = runtime
    root = {
        "root": Path("/"),
        "home": Path.home(),
        "cwd": Path.cwd(),
        "relative": Path("relative/site-packages"),
        "user_site": Path(site.getusersitepackages()),
        "work": Path("/work"),
        "work_child": Path("/work/pkg"),
        "worktree": worktree,
        "worktree_child": worktree / "pkg",
        "worktree_parent": worktree.parent,
    }[kind]
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(root)])
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        worker_sandbox._verifier_runtime_roots(worktree)
    assert exc.value.code == "runtime_not_allowed"


@pytest.mark.parametrize("kind", ["workspace", "work_component", "worktree_prefix"])
def test_unrelated_runtime_root_is_returned(
    runtime: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    installed, worktree = runtime
    root = {
        "workspace": Path("/workspace"),
        "work_component": installed.parents[4] / "repro" / "work" / "oh-my-pi",
        "worktree_prefix": worktree.with_name("job-other"),
    }[kind]
    is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: path == root or is_dir(path))
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(root)])
    assert worker_sandbox._verifier_runtime_roots(worktree) == (installed.resolve(), root.resolve())


@pytest.mark.parametrize("overlap", ["child", "ancestor"])
def test_symlink_runtime_overlap_is_refused(
    runtime: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlap: str,
) -> None:
    _, worktree = runtime
    child = worktree / "pkg"
    child.mkdir()
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(child if overlap == "child" else worktree.parent, target_is_directory=True)
    worktree_alias = tmp_path / "worktree-alias"
    worktree_alias.symlink_to(worktree, target_is_directory=True)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(alias)])
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        worker_sandbox._verifier_runtime_roots(worktree_alias)
    assert exc.value.code == "runtime_not_allowed"


def test_missing_install_parent_is_refused(
    runtime: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed, worktree = runtime
    monkeypatch.setattr(omp_work, "__file__", str(installed / "missing" / "omp_work" / "__init__.py"))
    with pytest.raises(worker_sandbox.WorkerSandboxRefused) as exc:
        worker_sandbox._verifier_runtime_roots(worktree)
    assert exc.value.code == "runtime_not_allowed"
