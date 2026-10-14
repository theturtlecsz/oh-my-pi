"""Classify exact flood fixture bytes without importing or modifying flood.py."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ASSETS = Path(__file__).resolve().parents[3] / "infra" / "automation-user"
SCRIPT = ASSETS / "omp584-parts.py"
FIXTURE = Path(__file__).parent / "fixtures" / "omp622" / "flood-4fb53b8.py.txt"
SPEC = importlib.util.spec_from_file_location("omp622_parts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PARTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARTS)
INSTALLED = dict.fromkeys(("dependencies", "start-metrics", "planner-prompt"), "installed")


@pytest.fixture
def candidate(tmp_path):
    target = tmp_path / "flood.py"
    target.write_bytes(FIXTURE.read_bytes())
    return target


def cli(candidate):
    before = candidate.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(candidate)], capture_output=True, text=True, timeout=20,
    )
    assert candidate.read_bytes() == before
    return result


def install_prompt(candidate):
    result = subprocess.run(
        ["git", "apply", str(ASSETS / "omp584-planner-prompt.patch")],
        cwd=candidate.parent, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_pinned_fixture_has_dependencies_and_metrics_but_no_prompt(candidate):
    source = candidate.read_bytes()
    expected = {**INSTALLED, "planner-prompt": "missing"}
    assert PARTS.classify(source) == expected
    result = cli(candidate)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [f"{part} {state}" for part, state in expected.items()]
    assert result.stderr == ""
    assert candidate.read_bytes() == source


def test_prompt_patch_completes_installation(candidate):
    install_prompt(candidate)
    assert PARTS.classify(candidate.read_bytes()) == INSTALLED
    assert cli(candidate).returncode == 0


def test_partial_independence_install_names_conflicting_part_and_missing_markers(candidate):
    patch = (ASSETS / "omp584-planner-independence.patch").read_text()
    line = next(line[1:] for line in patch.splitlines() if line.startswith("+PLANNER_INDEPENDENCE ="))
    candidate.write_bytes(candidate.read_bytes() + b"\n" + line.encode() + b"\n")
    assert PARTS.classify(candidate.read_bytes()) == {**INSTALLED, "planner-prompt": "conflict"}
    result = cli(candidate)
    assert result.returncode == 3
    assert "planner-prompt: absent markers:" in result.stderr
    assert "PLANNER_RULES_BYTES" in result.stderr
    assert "Flood.plan_prompt uses PLANNER_INDEPENDENCE" in result.stderr
    assert "dependencies:" not in result.stderr
    assert "start-metrics:" not in result.stderr


@pytest.mark.parametrize("suffix", [
    b"\n# PLANNER_RULES_BYTES = b'rules'\n",
    b'\n"PLANNER_RULES_BYTES = rules"\n',
])
def test_comments_and_strings_do_not_install_markers(candidate, suffix):
    candidate.write_bytes(candidate.read_bytes() + suffix)
    assert PARTS.classify(candidate.read_bytes())["planner-prompt"] == "missing"


def test_removed_spawn_call_conflicts_but_dict_unpack_call_is_installed(candidate):
    class SpawnCall(ast.NodeTransformer):
        def __init__(self, remove):
            self.remove = remove

        def visit_Call(self, node):
            self.generic_visit(node)
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "start_event"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                return ast.Dict(keys=[], values=[]) if self.remove else ast.Dict(keys=[None], values=[node])
            return node

    for remove, state in ((False, "installed"), (True, "conflict")):
        tree = SpawnCall(remove).visit(ast.parse(candidate.read_bytes()))
        source = ast.unparse(ast.fix_missing_locations(tree)).encode()
        assert PARTS.classify(source)["start-metrics"] == state


def test_unparseable_bytes_conflict_for_every_part(candidate):
    candidate.write_bytes(b"def broken(:\n")
    assert PARTS.classify(candidate.read_bytes()) == dict.fromkeys(INSTALLED, "conflict")
    result = cli(candidate)
    assert result.returncode == 3
    for part in INSTALLED:
        assert f"{part}: absent markers:" in result.stderr


@pytest.mark.parametrize("marker", [
    "dependency_tokens", "parse_dependency_context", "dependency_edge_violation", "validate_slices",
    "CODE_SHA256", "start_event", "plan_prompt", "start_plan_review", "PLANNER_RULES_BYTES",
    "PLANNER_INDEPENDENCE", "PLANNER_RULES_SHA256", "run",
])
def test_each_required_definition_or_assignment_detects_partial_install(candidate, marker):
    install_prompt(candidate)
    tree = ast.parse(candidate.read_bytes())
    part = ("dependencies" if marker in (
        "dependency_tokens", "parse_dependency_context", "dependency_edge_violation", "validate_slices",
    ) else "start-metrics" if marker in ("CODE_SHA256", "start_event") else "planner-prompt")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == marker:
            node.name = "removed_marker"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == marker:
                    target.id = "removed_marker"
    assert PARTS.classify(ast.unparse(tree).encode()) == {**INSTALLED, part: "conflict"}


@pytest.mark.parametrize("source", [
    b"class Other:\n    def dependency_tokens(self): pass\n",
    b"def other():\n    CODE_SHA256 = 'local'\n",
    b"obj.CODE_SHA256 = 'attribute'\n",
    b"CODE_SHA256: str\n",
])
def test_non_module_definitions_and_non_assignments_leave_parts_missing(source):
    assert PARTS.classify(source) == dict.fromkeys(INSTALLED, "missing")


@pytest.mark.parametrize("name", ["CODE_SHA256", "PLANNER_RULES_SHA256", "PLANNER_INDEPENDENCE"])
def test_string_mentions_do_not_replace_real_prompt_or_log_references(candidate, name):
    install_prompt(candidate)

    class StringReference(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id == name and isinstance(node.ctx, ast.Load):
                return ast.Constant(value=name)
            return node

    tree = StringReference().visit(ast.parse(candidate.read_bytes()))
    assert PARTS.classify(ast.unparse(tree).encode()) == {**INSTALLED, "planner-prompt": "conflict"}
