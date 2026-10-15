"""Prove unchanged omp584-report.py reads rollout.json written by real omp584-rollout.sh.

Consumes rollout.json from running omp584-rollout.sh on the incremental fixture checkout,
with the foreground post-commit hook pattern from test_omp622_install.py, no qualification
stubs and no services.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import omp622_rollout_helpers
from omp622_rollout_helpers import (
    ASSETS,
    create_checkout,
    run_script,
)

RULES_NAME = "planner-independence.md"
ACTIVATED_AT = "2026-10-15T12:00:00Z"
OLD_LOG = (
    "2026-10-14T09:00:00Z flood: old log line 1\n"
    "2026-10-14T09:00:01Z flood: old log line 2\n"
)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


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
        'printf \'%s loaded {"event":"loaded","at":"%s","pid":4242,"code_sha256":"%s","rules_sha256":"%s","external_head":"%s"}\\n\' \\',
        "  \"$at\" \"$at\" \"$code_sha\" \"$rules_sha\" \"$head_commit\" >> flood.log",
        "",
    ]
    hook_text = "\n".join(lines)
    assert '\\"' not in hook_text, "hook text must not contain backslash-quote sequence"
    path = hooks / "post-commit"
    path.write_text(hook_text)
    path.chmod(0o755)


def write_older_log(source: Path) -> None:
    (source / "flood.log").write_text(OLD_LOG)


def test_omp584_report_reads_new_rollout(tmp_path: Path) -> None:
    """Unchanged omp584-report.py reads the rollout.json from incremental rollout on fixture checkout."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"

    write_older_log(source)
    install_hook(source)

    rollout_proc = run_script(source, evidence, env=env, timeout=600)
    assert rollout_proc.returncode == 0, rollout_proc.stdout + rollout_proc.stderr

    rollout_path = evidence / "rollout.json"
    assert rollout_path.is_file()
    rollout = json.loads(rollout_path.read_text())

    state_path = source / "flood-state.json"
    state_path.write_text('{"tasks": {}}')

    rollout_before = rollout_path.read_bytes()
    flood_py_path = source / "flood.py"
    flood_py_before = flood_py_path.read_bytes()

    out_path = tmp_path / "report.json"
    report_script = ASSETS / "omp584-report.py"

    report_proc = subprocess.run(
        ["python3", str(report_script), "--flood-dir", str(source), "--rollout", str(rollout_path), "--output", str(out_path)],
        capture_output=True,
        text=True,
    )

    assert report_proc.returncode == 2, f"Expected exit 2, got {report_proc.returncode}: {report_proc.stderr}"

    assert out_path.is_file()
    report_data = json.loads(out_path.read_text())

    assert report_data["status"] == "pending"
    assert report_data["error"] is None
    assert "flood import" not in (report_proc.stderr or "")
    assert "flood import" not in str(report_data.get("error"))

    assert report_data["cutoff"]["activated_at"] == rollout["activated_at"]
    assert report_data["cutoff"]["flood_sha256"] == rollout["flood_sha256"]
    assert report_data["cutoff"]["repo_commit"] == rollout["repo_commit"]

    assert report_data["input_hashes"]["rollout_sha256"] == hashlib.sha256(rollout_before).hexdigest()
    assert report_data["input_hashes"]["flood_state_sha256"] == hashlib.sha256(state_path.read_bytes()).hexdigest()

    assert rollout_path.read_bytes() == rollout_before
    assert flood_py_path.read_bytes() == flood_py_before
