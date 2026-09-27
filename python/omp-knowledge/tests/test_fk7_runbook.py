"""Contract test for the FK-7 local runbook (OMP-312-s07).

Executes the runbook's fenced exercise block end-to-end and validates that
documented CLI flags match the shipped argument parsers without drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from omp_knowledge.inspection.cli import build_parser as build_inspection_show_parser
from omp_knowledge.learning.cli import _build_parser as build_learning_parser
from omp_knowledge.maintenance.cli import _build_parser as build_maintenance_parser
from omp_knowledge.maintenance.records import extract_tables
from omp_knowledge.source_import import _build_parser as build_source_import_parser

RUNBOOK_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "fleet-knowledge-fk7-runbook.md"
)


def _extract_exercise_block(markdown: str) -> str:
    """Extracts the fenced block tagged ```sh fk7-exercise from markdown."""
    pattern = r"```sh\s+fk7-exercise\s*\n(.*?)```"
    match = re.search(pattern, markdown, re.DOTALL)
    assert match is not None, "Fenced block tagged ```sh fk7-exercise not found in runbook"
    return match.group(1)


def _get_subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if name in action.choices:
                return action.choices[name]
    raise KeyError(f"Subparser {name!r} not found in parser {parser.prog}")


def _valid_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def _extract_all_commands(content: str) -> list[str]:
    """Extracts all CLI commands from fenced code blocks and inline code spans."""
    commands: list[str] = []

    # 1. From fenced blocks
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL)
    for block in code_blocks:
        joined = re.sub(r"\\\s*\n\s*", " ", block)
        for line in joined.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "omp_knowledge." in line:
                cleaned = line.lstrip("$ ").strip()
                if cleaned.startswith("python -m omp_knowledge."):
                    commands.append(cleaned)

    # 2. From inline code spans (e.g. in tables or prose)
    inline_spans = re.findall(r"`(python\s+-m\s+omp_knowledge\.[^`]+)`", content)
    for span in inline_spans:
        cleaned = span.strip()
        if cleaned not in commands:
            commands.append(cleaned)

    return commands


def _parser_and_flags_for_command(cmd_str: str) -> tuple[argparse.ArgumentParser, list[str]]:
    """Resolves an ArgumentParser and extracted flag list for a command invocation."""
    tokens = cmd_str.split()
    # Find module
    idx = 0
    while idx < len(tokens) and not tokens[idx].startswith("omp_knowledge."):
        idx += 1
    if idx >= len(tokens):
        raise ValueError(f"No omp_knowledge module in command: {cmd_str}")

    module = tokens[idx]
    args = tokens[idx + 1 :]
    flags = re.findall(r"(--[a-z0-9-]+)", cmd_str)

    if module == "omp_knowledge.source_import":
        return build_source_import_parser(), flags

    if module == "omp_knowledge.learning":
        if not args or args[0].startswith("-"):
            raise ValueError(f"Missing learning subcommand in: {cmd_str}")
        subcmd = args[0]
        learning_parser = build_learning_parser()
        return _get_subparser(learning_parser, subcmd), flags

    if module == "omp_knowledge.inspection":
        subcmd = args[0] if (args and not args[0].startswith("-")) else "show"
        if subcmd == "show":
            return _get_subparser(build_inspection_show_parser(), "show"), flags
        if subcmd == "serve":
            p = argparse.ArgumentParser(prog="python -m omp_knowledge.inspection serve")
            p.add_argument("--state-root", required=True)
            p.add_argument("--port", type=int, default=0)
            return p, flags
        raise ValueError(f"Unknown inspection subcommand {subcmd!r} in {cmd_str}")

    if module == "omp_knowledge.maintenance":
        if not args or args[0].startswith("-"):
            raise ValueError(f"Missing maintenance subcommand in: {cmd_str}")
        subcmd = args[0]
        maintenance_parser = build_maintenance_parser()
        return _get_subparser(maintenance_parser, subcmd), flags

    raise ValueError(f"Unsupported module {module!r} in: {cmd_str}")


def test_documented_command_flags_match_cli() -> None:
    """Every command documented in the runbook must match the shipped CLIs without flag drift."""
    assert RUNBOOK_PATH.is_file(), f"Runbook file not found at {RUNBOOK_PATH}"
    content = RUNBOOK_PATH.read_text(encoding="utf-8")

    commands = _extract_all_commands(content)
    # Filter out generic synopsis lines containing '<subcommand>'
    concrete_commands = [c for c in commands if "<subcommand>" not in c]

    assert len(concrete_commands) >= 15, f"Expected at least 15 concrete commands, found {len(concrete_commands)}"

    for cmd_str in concrete_commands:
        parser, flags = _parser_and_flags_for_command(cmd_str)
        valid_options = _valid_option_strings(parser)

        for flag in flags:
            assert flag in valid_options, (
                f"Documented flag {flag!r} in command {cmd_str!r} "
                f"does not exist in parser {parser.prog} (valid flags: {sorted(valid_options)})"
            )


def test_flag_drift_detection_rejects_unknown_flags() -> None:
    """The flag drift verifier must reject commands with unknown or misspelled flags."""
    fake_cmd = "python -m omp_knowledge.maintenance backup --state-root /tmp --backup-dir /tmp --drifted-flag"
    parser, flags = _parser_and_flags_for_command(fake_cmd)
    valid_options = _valid_option_strings(parser)

    assert "--drifted-flag" in flags
    assert "--drifted-flag" not in valid_options


def test_fk7_exercise_block_execution(tmp_path: Path) -> None:
    """Executes the ```sh fk7-exercise block from the runbook and asserts all contracts:

    1. Subprocess exits with code 0 under set -euo pipefail.
    2. Rebuilt per-table SHA-256 digests equal the backup manifest.
    3. Post-rollback inspection JSON matches pre-change inspection JSON.
    """
    assert RUNBOOK_PATH.is_file(), f"Runbook file not found at {RUNBOOK_PATH}"
    content = RUNBOOK_PATH.read_text(encoding="utf-8")
    exercise_script = _extract_exercise_block(content)

    state_dir = tmp_path / "state"
    backup_dir = tmp_path / "backup"
    rebuild_dir = tmp_path / "rebuilt"
    pre_inspect_path = tmp_path / "pre_inspect.json"
    post_inspect_path = tmp_path / "post_inspect.json"

    # Temporary git checkout with a valid remote and initial commit
    repo_dir = tmp_path / "checkout"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/theturtlecsz/oh-my-pi.git"],
        cwd=repo_dir,
        check=True,
    )
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo_dir,
        check=True,
    )

    fixture_a = Path(__file__).resolve().parent / "fixtures" / "A"
    assert (fixture_a / "facts.jsonl").is_file(), f"Fixture facts.jsonl missing in {fixture_a}"
    assert (fixture_a / "receipt.json").is_file(), f"Fixture receipt.json missing in {fixture_a}"

    env = os.environ.copy()
    env["STATE"] = str(state_dir)
    env["BACKUP"] = str(backup_dir)
    env["REBUILD"] = str(rebuild_dir)
    env["CHECKOUT"] = str(repo_dir)
    env["ENOLA_DIR"] = str(fixture_a)
    env["WORKSPACE"] = str(uuid4())
    env["REPO_ID"] = str(uuid4())
    env["SNAPSHOT_ID"] = "a" * 64
    env["PRE_INSPECT"] = str(pre_inspect_path)
    env["POST_INSPECT"] = str(post_inspect_path)
    # Guarantee python resolves to the active virtual environment
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"

    script_path = tmp_path / "run_exercise.sh"
    script_path.write_text(f"set -euo pipefail\n{exercise_script}", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(script_path)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        f"Exercise block exited with code {proc.returncode}:\n"
        f"STDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )

    # 1. Assert rebuilt per-table digests equal the backup manifest
    manifest_path = backup_dir / "manifest.json"
    assert manifest_path.is_file(), f"Manifest file missing at {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "stores" in manifest and manifest["stores"], "Manifest missing stores"

    for store_name, store_info in manifest["stores"].items():
        rebuilt_file = rebuild_dir / store_info["file"]
        assert rebuilt_file.is_file(), f"Rebuilt store {rebuilt_file} not found"
        conn = sqlite3.connect(str(rebuilt_file))
        try:
            actual_tables = extract_tables(conn)
            for table_name, expected_table in store_info["tables"].items():
                assert table_name in actual_tables, f"Table {table_name} missing from rebuilt {store_name}"
                assert actual_tables[table_name]["sha256"] == expected_table["sha256"], (
                    f"Digest mismatch in rebuilt {store_name}.{table_name}: "
                    f"{actual_tables[table_name]['sha256']} != {expected_table['sha256']}"
                )
        finally:
            conn.close()

    # 2. Assert post-rollback inspection JSON equals pre-change inspection JSON
    assert pre_inspect_path.is_file(), f"Pre-inspect file {pre_inspect_path} was not created"
    assert post_inspect_path.is_file(), f"Post-inspect file {post_inspect_path} was not created"

    pre_json = json.loads(pre_inspect_path.read_text(encoding="utf-8"))
    post_json = json.loads(post_inspect_path.read_text(encoding="utf-8"))

    assert post_json == pre_json, (
        "Post-rollback inspection JSON differs from pre-change inspection JSON:\n"
        f"PRE:\n{json.dumps(pre_json, indent=2)}\n"
        f"POST:\n{json.dumps(post_json, indent=2)}"
    )

    # Verify that the restored data reflects the original published state
    structural_snaps = post_json.get("sources", {}).get("structural_snapshots", [])
    assert len(structural_snaps) == 1
    assert structural_snaps[0]["state"] == "published"
