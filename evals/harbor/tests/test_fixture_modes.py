"""Every Harbor fixture verifier script must be executable.

Harbor's verifier copies ``tests/test.sh`` into a container and runs
``chmod +x`` before executing it. With no capabilities available the chmod
fails with EPERM, the script never runs, and grading ends in
``RewardFileNotFoundError``. The mode must therefore be executable where git
materializes it, which is what makes the fixture usable at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = HARBOR_DIR.parents[1]
SCRIPT_GLOB = "*/tests/test.sh"
EXECUTABLE_MODE = "100755"


def _fixture_scripts() -> list[Path]:
    return sorted((HARBOR_DIR / "fixtures").glob(SCRIPT_GLOB))


def _is_git_checkout() -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _git_mode(path: Path) -> str:
    listed = subprocess.run(
        ["git", "ls-files", "-s", "--", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    entries = [line for line in listed.stdout.splitlines() if line.strip()]
    assert len(entries) == 1, f"expected one index entry for {path}, got {entries!r}"
    return entries[0].split()[0]


def test_fixture_scripts_are_executable() -> None:
    scripts = _fixture_scripts()

    names = {path.parent.parent.name for path in scripts}
    assert {"f1", "f2"} <= names, f"missing fixture scripts in {HARBOR_DIR / 'fixtures'}: {names!r}"

    git_checkout = _is_git_checkout()
    for path in scripts:
        if git_checkout:
            assert _git_mode(path) == EXECUTABLE_MODE, f"{path} is not committed executable"
        assert os.access(path, os.X_OK), f"{path} is not executable on disk"
