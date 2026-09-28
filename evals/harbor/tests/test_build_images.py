"""build-images.sh refuses to start without the prebuilt linux-x64 addons.

The agent image copies the repository and does not compile Rust. The build
script must see both pi_natives addons before it provisions credentials or
calls docker, and Dockerfile.agent.dockerignore must let those two files
through the context.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HARBOR = REPO / "evals" / "harbor"
BUILD_SCRIPT = HARBOR / "scripts" / "build-images.sh"
ROOT_DOCKERIGNORE = REPO / ".dockerignore"
AGENT_DOCKERIGNORE = HARBOR / "docker" / "Dockerfile.agent.dockerignore"

ADDONS = (
    "packages/natives/native/pi_natives.linux-x64-modern.node",
    "packages/natives/native/pi_natives.linux-x64-baseline.node",
)


def _active_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _pattern_matches(path: str, pattern: str) -> bool:
    """True when a dockerignore pattern body matches path or a parent directory.

    `**` crosses directories. fnmatch's `*` does too, which is what `**` needs;
    a pattern with no slash also matches in any directory, same as dockerignore.
    """

    directory_only = pattern.endswith("/")
    body = pattern.rstrip("/")
    if not body:
        return False

    forms: list[str] = []
    if not directory_only:
        forms.append(body)
        if "/" not in body.replace("**", ""):
            forms.append(f"**/{body}")
    directory_forms = [body]
    if "/" not in body.replace("**", ""):
        directory_forms.append(f"**/{body}")
    forms.extend(f"{form}/**" for form in directory_forms)
    return any(fnmatch.fnmatchcase(path, form) for form in forms)


def _is_excluded(path: str, patterns: list[str]) -> bool:
    """Last matching pattern wins. A later `!` re-include puts the path back."""

    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        body = pattern[1:] if negated else pattern
        if _pattern_matches(path, body):
            excluded = not negated
    return excluded


def _stage(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path
    scripts = repo / "evals" / "harbor" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(BUILD_SCRIPT, scripts / "build-images.sh")
    log = repo / "calls.log"
    log.write_text("", encoding="utf-8")
    quoted = shlex.quote(str(log))
    (scripts / "provision-credentials.sh").write_text(
        "#!/bin/sh\n" f"printf '%s\\n' provision >> {quoted}\n",
        encoding="utf-8",
    )
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n" f"printf '%s\\n' \"docker $*\" >> {quoted}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return repo, log, env


def _run(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "evals" / "harbor" / "scripts" / "build-images.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_prebuilt_addon_stops_before_docker(tmp_path: Path) -> None:
    repo, log, env = _stage(tmp_path)

    result = _run(repo, env)

    assert result.returncode != 0
    assert "build-images: missing prebuilt native addon" in result.stderr
    assert "build or copy it into packages/natives/native first" in result.stderr
    assert any(Path(addon).name in result.stderr for addon in ADDONS)
    assert log.read_text(encoding="utf-8") == ""


def test_prebuilt_addons_present_invokes_docker(tmp_path: Path) -> None:
    repo, log, env = _stage(tmp_path)
    for addon in ADDONS:
        path = repo / addon
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    result = _run(repo, env)

    assert result.returncode == 0, result.stderr
    assert any(line.startswith("docker") for line in log.read_text(encoding="utf-8").splitlines())


def test_agent_dockerignore_keeps_root_rules_and_ships_addons() -> None:
    root_lines = _active_lines(ROOT_DOCKERIGNORE.read_text(encoding="utf-8"))
    shadow_lines = _active_lines(AGENT_DOCKERIGNORE.read_text(encoding="utf-8"))
    for line in root_lines:
        if line == "**/*.node":
            continue
        assert line in shadow_lines, line

    sample = ADDONS[0]
    assert _is_excluded(sample, ["**/*.node"])
    assert not _is_excluded(sample, ["**/*.node", f"!{sample}"])
    assert _is_excluded(sample, [f"!{sample}", "**/*.node"])

    for addon in ADDONS:
        assert not _is_excluded(addon, shadow_lines), addon
