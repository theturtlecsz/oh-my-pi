"""Tests that no product surfaces retain Grok Bot names or wiring (OMP-430).

Acceptance requirement:
- no omp_work module name (pkgutil.walk_packages) contains "grok" (any case);
- no subcommand --help text contains "grok" (any case);
- no contract.json scope contains "grok" (any case);
- no operations.capabilities scope tuple contains "grok" (any case).
"""

from __future__ import annotations

import json
import pkgutil
from pathlib import Path

import pytest

import omp_work
from omp_work.__main__ import main
from omp_work.operations import capabilities


def test_no_module_name_contains_grok() -> None:
    """No omp_work module name from pkgutil.walk_packages contains 'grok' (any case)."""
    grok_modules: list[str] = []
    for module_info in pkgutil.walk_packages(omp_work.__path__, prefix="omp_work."):
        if "grok" in module_info.name.lower():
            grok_modules.append(module_info.name)
    assert not grok_modules, f"Found module names containing 'grok': {grok_modules}"


def test_no_subcommand_help_contains_grok(capsys: pytest.CaptureFixture[str]) -> None:
    """No top-level or subcommand --help text contains 'grok' (any case)."""

    def _get_help(args: list[str]) -> str:
        with pytest.raises(SystemExit) as exc_info:
            main(args)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        return captured.out + captured.err

    # Root help
    root_help = _get_help(["--help"])
    assert "grok" not in root_help.lower(), f"Root help contains 'grok':\n{root_help}"

    # Top-level subcommands
    subcommands = [
        "schema",
        "hash",
        "approve",
        "validate",
        "ops",
        "serve",
        "headroom",
        "stall-check",
        "parallel-admit",
        "budget-alerts",
        "projects",
        "stop",
        "alarms",
        "jobs",
        "owner-key",
    ]
    for sub in subcommands:
        text = _get_help([sub, "--help"])
        assert "grok" not in text.lower(), f"Subcommand '{sub}' help contains 'grok':\n{text}"

    # Nested subcommands
    nested_subcommands = [
        ["ops", "capabilities", "--help"],
        ["ops", "capabilities", "init", "--help"],
        ["ops", "capabilities", "candidate-reader", "--help"],
        ["ops", "capabilities", "automation", "--help"],
        ["ops", "capabilities", "client", "--help"],
        ["ops", "capabilities", "event-push", "--help"],
        ["ops", "capabilities", "client-config", "--help"],
        ["alarms", "watch-credentials", "--help"],
        ["jobs", "worker", "--help"],
        ["jobs", "register-component", "--help"],
        ["jobs", "check", "--help"],
        ["projects", "seed", "--help"],
        ["projects", "show", "--help"],
        ["projects", "check", "--help"],
        ["stop", "status", "--help"],
        ["stop", "engage", "--help"],
        ["stop", "release", "--help"],
        ["stop", "watch", "--help"],
    ]
    for cmd in nested_subcommands:
        text = _get_help(cmd)
        assert "grok" not in text.lower(), f"Command '{' '.join(cmd)}' help contains 'grok':\n{text}"


def test_no_contract_scope_contains_grok() -> None:
    """No contract.json scope or security_policy scope contains 'grok' (any case)."""
    contract_file = (
        Path(__file__).parent.parent / "src" / "omp_work" / "contracts" / "v1" / "contract.json"
    )
    contract = json.loads(contract_file.read_text(encoding="utf-8"))

    scopes = contract.get("scopes", [])
    assert scopes, "contract.json must declare scopes"
    for scope in scopes:
        assert "grok" not in scope.lower(), f"contract.json scope contains 'grok': {scope}"

    security_policy = contract.get("security_policy", {})
    for policy_key, policy_val in security_policy.items():
        if isinstance(policy_val, (list, tuple)):
            for entry in policy_val:
                if isinstance(entry, str):
                    assert "grok" not in entry.lower(), (
                        f"security_policy.{policy_key} entry contains 'grok': {entry}"
                    )


def test_no_capabilities_scope_tuple_contains_grok() -> None:
    """No operations.capabilities scope tuple contains 'grok' (any case)."""
    scope_tuples = [
        capabilities.OWNER_SCOPES,
        capabilities.AUTOMATION_SCOPES,
        capabilities.CLIENT_SCOPES,
        capabilities.CLIENT_STOP_ONLY_SCOPES,
        capabilities.EVENT_PUSH_SCOPES,
    ]
    # Check all named scope tuples on the module
    for attr in dir(capabilities):
        val = getattr(capabilities, attr)
        if (
            isinstance(val, tuple)
            and attr.endswith("_SCOPES")
            and val not in scope_tuples
        ):
            scope_tuples.append(val)

    for st in scope_tuples:
        for scope in st:
            assert isinstance(scope, str)
            assert "grok" not in scope.lower(), (
                f"capabilities scope tuple contains 'grok': {scope}"
            )
