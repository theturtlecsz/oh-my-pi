"""Real omp584-rollout.sh entry for already-installed no-op and absent rules installation.

No stubs and no services. A foreground post-commit hook appends a reload line
and a loaded record of the committed file hashes and HEAD onto flood.log.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from omp622_rollout_helpers import (
    ASSETS,
    FIXTURE,
    checkout_snapshot,
    create_checkout,
    file_snapshot,
    run_script,
)

LEGACY_NAME = b'with_name("omp584-planner-rules.md")'
CANONICAL_NAME = b'with_name("planner-independence.md")'
RULES_NAME = "planner-independence.md"
NOTHING = "omp584: all parts already installed; nothing to install"
ACTIVATED_AT = "2026-10-15T12:00:00Z"
OLD_LOG = (
    "2026-10-14T09:00:00Z flood: old log line 1\n"
    "2026-10-14T09:00:01Z flood: old log line 2\n"
)


def git(source: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=source, env=env, capture_output=True, text=True, check=True,
    )


def git_bytes(source: Path, env: dict[str, str], *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=source, env=env, capture_output=True, check=True,
    )
    return result.stdout


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def apply_planner(source: Path, env: dict[str, str], *, canonical: bool, rules: bool) -> None:
    """Patch the fixture planner part, optionally canonicalize the rules name, and commit."""
    result = subprocess.run(
        ["git", "apply", str(ASSETS / "omp584-planner-prompt.patch")],
        cwd=source, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    flood = source / "flood.py"
    data = flood.read_bytes()
    assert LEGACY_NAME in data
    if canonical:
        flood.write_bytes(data.replace(LEGACY_NAME, CANONICAL_NAME))
        assert LEGACY_NAME not in flood.read_bytes()
    paths = ["flood.py"]
    if rules:
        (source / RULES_NAME).write_bytes((ASSETS / "omp584-planner-rules.md").read_bytes())
        paths.append(RULES_NAME)
    git(source, env, "add", "--", *paths)
    git(source, env, "commit", "-q", "-m", "fixture planner part")


def install_hook(source: Path, *, code_sha: str | None = None, at: str = ACTIVATED_AT) -> None:
    """Foreground post-commit hook: reload line plus loaded record, then return."""
    hooks = source / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    code_line = (
        f"code_sha={sh_quote(code_sha)}"
        if code_sha is not None
        else "code_sha=$(sha256sum flood.py | awk '{print $1}')"
    )
    lines = [
        "#!/bin/sh",
        "set -eu",
        f"cd {sh_quote(str(source))}",
        f"at={sh_quote(at)}",
        "head_commit=$(git rev-parse HEAD)",
        code_line,
        f"rules_sha=$(sha256sum {RULES_NAME} | awk '{{print $1}}')",
        "printf '%s [reload] flood.py or flood.json changed: exec in place (running jobs continue)\\n' \"$at\" >> flood.log",
        "printf '%s loaded {\\\"event\\\":\\\"loaded\\\",\\\"at\\\":\\\"%s\\\",\\\"pid\\\":4242,\\\"code_sha256\\\":\\\"%s\\\",\\\"rules_sha256\\\":\\\"%s\\\",\\\"external_head\\\":\\\"%s\\\"}\\n' \\",
        "  \"$at\" \"$at\" \"$code_sha\" \"$rules_sha\" \"$head_commit\" >> flood.log",
        "",
    ]
    path = hooks / "post-commit"
    path.write_text("\n".join(lines))
    path.chmod(0o755)


def write_older_log(source: Path) -> None:
    (source / "flood.log").write_text(OLD_LOG)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_rollout(source: Path, env: dict[str, str], evidence: Path, at: str) -> None:
    rollout_path = evidence / "rollout.json"
    assert rollout_path.is_file()
    rollout = json.loads(rollout_path.read_text())
    head = git(source, env, "rev-parse", "HEAD").stdout.strip()
    code = file_sha256(source / "flood.py")
    rules = file_sha256(source / RULES_NAME)
    record = rollout["matched_record"]
    assert rollout["record"] == record
    assert rollout["activated_at"] == record["at"] == at
    assert rollout["external_commit"] == record["external_head"] == head
    assert rollout["flood_sha256"] == record["code_sha256"] == code
    assert record["rules_sha256"] == rules
    installed = {"flood.py": code, RULES_NAME: rules}
    assert rollout["installed_file_hashes"] == installed
    assert rollout["installed_hashes"] == installed
    artifacts = {
        name: file_sha256(ASSETS / name)
        for name in (
            "omp584-dependencies.patch",
            "omp584-start-metrics.patch",
            "omp584-planner-prompt.patch",
            "omp584-planner-rules.md",
        )
    }
    assert rollout["sibling_hashes"] == artifacts
    assert rollout["artifact_hashes"] == artifacts
    repo = git(ASSETS.parents[1], env, "rev-parse", "HEAD").stdout.strip()
    assert rollout["repo_commit"] == repo
    for key, name in (
        ("compile_log", "compile.log"),
        ("pytest_log", "pytest.log"),
        ("reload_log", "reload.log"),
    ):
        path = Path(rollout[key])
        assert path.resolve() == (evidence / name).resolve()
        assert path.is_file() and path.stat().st_size > 0
    assert Path(rollout["log_paths"]["compile"]).resolve() == (evidence / "compile.log").resolve()
    assert Path(rollout["log_paths"]["pytest"]).resolve() == (evidence / "pytest.log").resolve()
    assert Path(rollout["log_paths"]["reload"]).resolve() == (evidence / "reload.log").resolve()
    reload_log = (evidence / "reload.log").read_text()
    assert "[reload] flood.py or flood.json changed: exec in place" in reload_log
    assert code in reload_log
    assert head in reload_log


def test_all_installed_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonical patched fixture with asset rules committed installs nothing, even with file '--' in caller dir."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    apply_planner(source, env, canonical=True, rules=True)
    before = checkout_snapshot(source, env)

    caller_dir = tmp_path / "caller"
    caller_dir.mkdir(parents=True, exist_ok=True)
    (caller_dir / "--").write_bytes(LEGACY_NAME + b"\n")
    monkeypatch.chdir(caller_dir)

    result = run_script(source, evidence, env=env, timeout=120)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == NOTHING
    assert "activation verification pending" not in result.stderr
    assert checkout_snapshot(source, env) == before
    assert not evidence.exists()


def test_all_parts_installed_rules_absent(tmp_path: Path) -> None:
    """Installed code parts with no rules file commit the asset rules and leave flood.py bytes."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    apply_planner(source, env, canonical=True, rules=False)
    assert not (source / RULES_NAME).exists()
    before_flood = (source / "flood.py").read_bytes()
    assert CANONICAL_NAME in before_flood and LEGACY_NAME not in before_flood
    before_head = git(source, env, "rev-parse", "HEAD").stdout.strip()
    write_older_log(source)
    install_hook(source)

    result = run_script(source, evidence, env=env, timeout=600)

    assert result.returncode == 0, result.stdout + result.stderr
    assert NOTHING not in result.stdout + result.stderr
    assert (source / "flood.py").read_bytes() == before_flood
    assert git_bytes(source, env, "show", "HEAD:flood.py") == before_flood
    names = set(git(source, env, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines())
    assert names == {RULES_NAME}
    assert git(source, env, "rev-parse", "HEAD").stdout.strip() != before_head
    asset_rules = (ASSETS / "omp584-planner-rules.md").read_bytes()
    assert (source / RULES_NAME).read_bytes() == asset_rules
    assert git_bytes(source, env, "show", f"HEAD:{RULES_NAME}") == asset_rules
    assert_rollout(source, env, evidence, ACTIVATED_AT)


def test_incremental_install(tmp_path: Path) -> None:
    """Install only missing planner code and rules, preserve operator dirt, and prove activation."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    before_flood = (source / "flood.py").read_bytes()
    before_head = git(source, env, "rev-parse", "HEAD").stdout.strip()
    (source / "staged.txt").write_text("operator staged content\n")
    git(source, env, "add", "--", "staged.txt")
    (source / "staged.txt").write_text("operator staged plus unstaged content\n")
    (source / "unrelated.txt").write_text("operator unstaged content\n")
    (source / "untracked.txt").write_text("operator untracked content\n")
    dirt_paths = ("staged.txt", "unrelated.txt", "untracked.txt")
    snapshot = file_snapshot(source)
    before_files = {name: snapshot[name] for name in dirt_paths}
    before_cached = git_bytes(source, env, "diff", "--cached", "--binary")
    before_unstaged = git_bytes(source, env, "diff", "--binary", "--", *dirt_paths)
    before_status = git_bytes(source, env, "status", "--porcelain", "--", *dirt_paths)
    write_older_log(source)
    install_hook(source)

    result = run_script(source, evidence, env=env, timeout=600)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(source, env, "rev-parse", "HEAD^").stdout.strip() == before_head
    names = set(git(source, env, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines())
    assert names == {"flood.py", RULES_NAME}
    installed_flood = (source / "flood.py").read_bytes()
    assert git_bytes(source, env, "show", "HEAD:flood.py") == installed_flood
    matcher = difflib.SequenceMatcher(
        a=before_flood.splitlines(keepends=True),
        b=installed_flood.splitlines(keepends=True),
        autojunk=False,
    )
    inserted = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        assert tag in ("equal", "insert"), "Previously installed code or operator edit changed"
        if tag == "insert":
            inserted.extend(matcher.b[j1:j2])
    patch = (ASSETS / "omp584-planner-prompt.patch").read_bytes()
    expected_inserted = [
        line[1:].replace(LEGACY_NAME, CANONICAL_NAME)
        for line in patch.splitlines(keepends=True)
        if line.startswith(b"+") and not line.startswith(b"+++")
    ]
    assert inserted == expected_inserted
    asset_rules = (ASSETS / "omp584-planner-rules.md").read_bytes()
    assert (source / RULES_NAME).read_bytes() == asset_rules
    assert git_bytes(source, env, "show", f"HEAD:{RULES_NAME}") == asset_rules
    after_files = file_snapshot(source)
    assert {name: after_files[name] for name in dirt_paths} == before_files
    assert git_bytes(source, env, "diff", "--cached", "--binary") == before_cached
    assert git_bytes(source, env, "diff", "--binary", "--", *dirt_paths) == before_unstaged
    assert git_bytes(source, env, "status", "--porcelain", "--", *dirt_paths) == before_status
    assert (source / "flood.log").read_text().startswith(OLD_LOG)
    assert_rollout(source, env, evidence, ACTIVATED_AT)
    loaded = json.loads((source / "flood.log").read_text().split(" loaded ", 1)[1])
    assert json.loads((evidence / "rollout.json").read_text())["matched_record"] == loaded
    assert "old log line" not in (evidence / "reload.log").read_text()


def test_wrong_code_hash(tmp_path: Path) -> None:
    """A loaded record for different code cannot publish successful rollout evidence."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    write_older_log(source)
    install_hook(source, code_sha="0" * 64)
    env["OMP584_POLL_TIMEOUT"] = "1"

    result = run_script(source, evidence, env=env, timeout=600)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Activation timeout" in result.stderr
    loaded = json.loads((source / "flood.log").read_text().split(" loaded ", 1)[1])
    assert loaded["code_sha256"] == "0" * 64 != file_sha256(source / "flood.py")
    assert loaded["rules_sha256"] == file_sha256(source / RULES_NAME)
    assert loaded["external_head"] == git(source, env, "rev-parse", "HEAD").stdout.strip()
    assert not (evidence / "rollout.json").exists()
