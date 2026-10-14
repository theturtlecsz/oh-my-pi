"""Rollout test helpers for OMP-622 rollout staging tests.

Provides:
- tmp git checkout of fixture flood-4fb53b8.py.txt plus a committed unrelated edit, isolated HOME/git;
- file/index/HEAD/evidence snapshots;
- foreground driver sourcing the script, running given functions (timeout 300 s).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any

ASSETS = Path(__file__).resolve().parents[3] / "infra" / "automation-user"
SCRIPT = ASSETS / "omp584-rollout.sh"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "omp622" / "flood-4fb53b8.py.txt"


def make_isolated_env(home_dir: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Create isolated HOME with gitconfig."""
    home_dir.mkdir(parents=True, exist_ok=True)
    gitconfig = home_dir / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n"
            "    name = Test\n"
            "    email = test@example.com\n"
            "[commit]\n"
            "    gpgsign = false\n"
        )
    env = dict(os.environ if base_env is None else base_env)
    env["HOME"] = str(home_dir)
    env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def create_checkout(tmp_path: Path, env: dict[str, str] | None = None) -> tuple[Path, dict[str, str]]:
    """Create a temporary git checkout of the fixture flood.py plus a committed unrelated edit."""
    home = tmp_path / "home"
    isolated_env = make_isolated_env(home, env)

    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)

    flood_path = checkout / "flood.py"
    flood_path.write_bytes(FIXTURE.read_bytes())
    flood_path.chmod(0o755)

    def git_run(argv: list[str]) -> subprocess.CompletedProcess:
        res = subprocess.run(
            ["git", *argv],
            cwd=checkout,
            capture_output=True,
            text=True,
            env=isolated_env,
            check=True,
        )
        return res

    git_run(["init", "-q", "-b", "main"])
    git_run(["add", "flood.py"])
    git_run(["commit", "-q", "-m", "initial fixture flood.py"])

    flood_path.write_bytes(flood_path.read_bytes() + b"\n# Unrelated operator edit after OMP-584 dependencies and start metrics.\n")
    unrelated = checkout / "unrelated.txt"
    unrelated.write_text("committed unrelated edit\n")
    git_run(["add", "flood.py", "unrelated.txt"])
    git_run(["commit", "-q", "-m", "committed unrelated edit"])

    return checkout, isolated_env


def file_snapshot(directory: Path) -> dict[str, tuple[str, int, int]]:
    """Snapshot relative path -> (sha256, mode, mtime_ns) for files excluding .git."""
    result: dict[str, tuple[str, int, int]] = {}
    if not directory.exists():
        return result
    for p in directory.rglob("*"):
        if ".git" in p.parts:
            continue
        if p.is_file():
            result[str(p.relative_to(directory))] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                p.stat().st_mode & 0o777,
                p.stat().st_mtime_ns,
            )
    return result


def index_snapshot(directory: Path) -> bytes | None:
    """Snapshot git index bytes."""
    index_file = directory / ".git" / "index"
    return index_file.read_bytes() if index_file.is_file() else None


def head_snapshot(directory: Path, env: dict[str, str] | None = None) -> str:
    """Snapshot git HEAD commit hash."""
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=directory,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return res.stdout.strip()


def evidence_snapshot(evidence: Path) -> dict[str, Any]:
    """Snapshot evidence directory presence and contents."""
    return {
        "exists": evidence.exists(),
        "files": file_snapshot(evidence) if evidence.is_dir() else {},
    }


def checkout_snapshot(directory: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Combined file/index/HEAD snapshot of a checkout."""
    return {
        "head": head_snapshot(directory, env=env),
        "index": index_snapshot(directory),
        "files": file_snapshot(directory),
    }


class DriverResult:
    """Result of running foreground driver."""

    def __init__(self, completed: subprocess.CompletedProcess, stage_dir: Path | None = None):
        self.completed = completed
        self.args = completed.args
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.stage_dir = stage_dir


def run_driver(
    source: Path,
    evidence: Path,
    functions: list[str] | tuple[str, ...] = ("omp584_preflight", "omp584_stage"),
    *,
    extra: str = "",
    preserve_stage: bool = False,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> DriverResult:
    """Foreground driver sourcing omp584-rollout.sh and executing given functions."""
    script_lines = ['source "$1"']
    if not preserve_stage:
        script_lines.append(
            r'''trap 'if [[ -n "${OMP584_STAGE:-}" ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT'''
        )
    else:
        script_lines.append(
            r'''trap 'rc=$?; if [[ -n "${OMP584_STAGE:-}" && $rc -ne 0 ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT'''
        )

    for fn in functions:
        if fn in ("omp584_preflight", "omp584_main"):
            script_lines.append(f'{fn} --flood-dir "$2" --evidence-dir "$3"')
        else:
            script_lines.append(fn)

    if extra:
        script_lines.append(extra)

    if preserve_stage:
        script_lines.append(r'''if [[ -n "${OMP584_STAGE:-}" ]]; then echo "STAGE_DIR:$OMP584_STAGE"; fi''')

    driver_code = "\n".join(script_lines) + "\n"

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    proc = subprocess.run(
        ["bash", "-c", driver_code, "test", str(SCRIPT), str(source), str(evidence)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
    )

    stage_dir: Path | None = None
    if preserve_stage:
        clean_lines: list[str] = []
        for line in proc.stdout.splitlines(keepends=True):
            if line.startswith("STAGE_DIR:"):
                stage_dir = Path(line.strip().split(":", 1)[1])
            else:
                clean_lines.append(line)
        proc.stdout = "".join(clean_lines)

    return DriverResult(proc, stage_dir=stage_dir)


def run_script(
    source: Path,
    evidence: Path,
    args: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess:
    """Run omp584-rollout.sh as a full CLI command."""
    if args is None:
        args = ["--flood-dir", str(source), "--evidence-dir", str(evidence)]
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
    )
