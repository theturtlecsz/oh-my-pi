"""OMP-317-s01: R13 mechanism exploration, critique, specification, and generation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omp_work.research import mechanism
from omp_work.research.mechanism import (
    MechanismDraft,
    classify_trace,
    critique,
    explore,
    failure_pattern,
    generation_job,
    specify,
    trace_digest,
    trace_hypothesis,
)
from omp_work.research.search import run_policy, winner


def test_classify_trace_identifies_instrumentation_and_mechanism() -> None:
    infra_trace = {
        "step_id": "s1",
        "policy": "greedy",
        "action": "refine",
        "metric": None,
        "cost": 1.0,
        "infra_error": "timeout",
        "outcome": "failed",
    }
    clean_trace = {
        "step_id": "s2",
        "policy": "greedy",
        "action": "refine",
        "metric": 0.5,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "completed",
    }
    assert classify_trace(infra_trace) == "instrumentation"
    assert classify_trace(clean_trace) == "mechanism"


def test_repeated_failure_pattern_detected() -> None:
    t1 = {
        "step_id": "s1",
        "policy": "greedy",
        "action": "refine",
        "metric": 0.2,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "failed",
    }
    t2 = {
        "step_id": "s2",
        "policy": "greedy",
        "action": "refine",
        "metric": 0.25,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "failed",
    }
    assert failure_pattern([t1, t2]) == "repeated_failure:refine"


def test_plateau_pattern_and_infra_trace_exclusion() -> None:
    t1 = {
        "step_id": "s1",
        "policy": "greedy",
        "action": "step1",
        "metric": 0.5,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "completed",
    }
    t2 = {
        "step_id": "s2",
        "policy": "greedy",
        "action": "step2",
        "metric": 0.4,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "completed",
    }
    t3 = {
        "step_id": "s3",
        "policy": "greedy",
        "action": "step3",
        "metric": 0.45,
        "cost": 1.0,
        "infra_error": None,
        "outcome": "completed",
    }
    assert failure_pattern([t1, t2, t3]) == "plateau:greedy"

    lone_infra = {
        "step_id": "s0",
        "policy": "greedy",
        "action": "step0",
        "metric": 0.5,
        "cost": 1.0,
        "infra_error": "disk_full",
        "outcome": "failed",
    }
    assert failure_pattern([lone_infra]) is None
    # Lone infra trace does not start a pattern when prepended
    assert failure_pattern([lone_infra, t1, t2, t3]) == "plateau:greedy"


def test_trace_hypothesis_validates_pattern_and_binds_context() -> None:
    clean_traces = [
        {
            "step_id": "s1",
            "policy": "greedy",
            "action": "step1",
            "metric": 0.5,
            "cost": 1.0,
            "infra_error": None,
            "outcome": "completed",
        }
    ]
    with pytest.raises(ValueError, match="^no_pattern$"):
        trace_hypothesis(clean_traces, "Why did optimization stop?")

    plateau_traces = [
        {
            "step_id": "s1",
            "policy": "greedy",
            "action": "step1",
            "metric": 0.5,
            "cost": 1.0,
            "infra_error": None,
            "outcome": "completed",
        },
        {
            "step_id": "s2",
            "policy": "greedy",
            "action": "step2",
            "metric": 0.4,
            "cost": 1.0,
            "infra_error": None,
            "outcome": "completed",
        },
        {
            "step_id": "s3",
            "policy": "greedy",
            "action": "step3",
            "metric": 0.45,
            "cost": 1.0,
            "infra_error": None,
            "outcome": "completed",
        },
    ]
    hyp = trace_hypothesis(plateau_traces, "Why did optimization stop?")
    assert hyp.kind == "draft"
    assert hyp.context_sha == trace_digest(plateau_traces)
    assert "plateau:greedy" in hyp.text


def test_explore_numbers_proposals() -> None:
    proposals = [
        {"name": "proposal_a", "components": ["c1"]},
        {"name": "proposal_b", "components": ["c2"]},
    ]
    drafts = explore("repeated_failure:refine", proposals)
    assert len(drafts) == 2
    assert drafts[0].id == "mech-1"
    assert drafts[1].id == "mech-2"
    assert drafts[0].name == "proposal_a"
    assert drafts[1].name == "proposal_b"
    assert drafts[0].addresses == ("repeated_failure:refine",)
    assert drafts[1].addresses == ("repeated_failure:refine",)


def test_critique_evaluates_pattern_and_complexity_budget() -> None:
    unaddressed_draft = MechanismDraft(
        id="mech-1",
        name="unaddressed",
        kind="code",
        parent=None,
        addresses=("other_pattern",),
        components=("c1", "c2"),
        description="unaddressed draft",
    )
    result_unaddressed = critique(unaddressed_draft, "repeated_failure:refine", complexity_budget=2)
    assert result_unaddressed["status"] == "rejected"
    assert "unaddressed_pattern" in result_unaddressed["reasons"]

    complex_draft = MechanismDraft(
        id="mech-2",
        name="complex",
        kind="code",
        parent=None,
        addresses=("repeated_failure:refine",),
        components=("c1", "c2", "c3"),
        description="complex draft",
    )
    result_complex = critique(complex_draft, "repeated_failure:refine", complexity_budget=2)
    assert result_complex["status"] == "rejected"
    assert "complexity_exceeded" in result_complex["reasons"]

    accepted_draft = MechanismDraft(
        id="mech-3",
        name="accepted",
        kind="code",
        parent=None,
        addresses=("repeated_failure:refine",),
        components=("c1", "c2"),
        description="accepted draft",
    )
    result_accepted = critique(accepted_draft, "repeated_failure:refine", complexity_budget=2)
    assert result_accepted["status"] == "accepted"
    assert result_accepted["reasons"] == []


def test_specify_reports_interface_and_fields() -> None:
    draft = MechanismDraft(
        id="mech-1",
        name="spec_mech",
        kind="code",
        parent=None,
        addresses=("pattern",),
        components=("comp_a", "comp_b"),
        description="expected mechanism behavior",
    )
    spec = specify(draft)
    assert spec["name"] == "spec_mech"
    assert spec["interface_version"] == 1
    assert spec["input_keys"] == ["steps", "opened", "seed", "width"]
    assert spec["output"] == "step_id | None"
    assert spec["state_fields"] == ["comp_a", "comp_b"]
    assert spec["expected_behavior"] == "expected mechanism behavior"
    assert spec["candidate_kind"] == "code"


def test_generation_job_compiles_and_outperforms_greedy() -> None:
    draft = MechanismDraft(
        id="mech-1",
        name="threshold_policy",
        kind="code",
        parent=None,
        addresses=("plateau:greedy",),
        components=(),
        description="explore children when parent metric >= threshold",
    )
    spec = specify(draft)
    source = generation_job(spec, {"min_metric": 0.85})

    compiled = compile(source, "<strategy>", "exec")
    scope: dict[str, object] = {}
    exec(compiled, scope)
    propose_fn = scope["propose"]
    assert callable(propose_fn)

    tree: dict[str, dict[str, object]] = {
        "A1": {"parent_id": None, "metric": 0.9, "cost": 1, "children": ["A2"], "infra_error": None},
        "A2": {"parent_id": "A1", "metric": 0.1, "cost": 1, "children": [], "infra_error": None},
        "B1": {"parent_id": None, "metric": 0.8, "cost": 1, "children": ["B2"], "infra_error": None},
        "B2": {"parent_id": "B1", "metric": 0.95, "cost": 1, "children": [], "infra_error": None},
    }

    # Custom generated strategy
    opened = run_policy(propose_fn, dict(tree), budget=4, width=1)
    assert opened == ["A1", "A2", "B1", "B2"]
    win_node = winner({sid: tree[sid] for sid in opened})
    assert win_node == "B2"
    assert tree[win_node]["metric"] == 0.95

    # Standard greedy policy
    greedy_opened = run_policy("greedy", dict(tree), budget=4, width=1)
    assert greedy_opened == ["A1", "A2"]
    greedy_win_node = winner({sid: tree[sid] for sid in greedy_opened})
    assert greedy_win_node == "A1"
    assert tree[greedy_win_node]["metric"] == 0.9


def test_mechanism_module_and_generated_source_safety() -> None:
    source_path = Path(mechanism.__file__)
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    # The module imports hypothesis and canonical only
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)

    omp_imports = {m for m in imported_modules if "omp" in m}
    assert omp_imports == {"omp_work.research", "omp_work.v1"} or omp_imports <= {
        "omp_work.research.hypothesis",
        "omp_work.v1.canonical",
        "omp_work.research",
        "omp_work.v1",
    }

    # Verify generated source imports nothing and contains no forbidden s02 tokens
    spec = {"name": "test", "candidate_kind": "code"}
    gen_source = generation_job(spec, {"min_metric": 0.85})
    gen_tree = ast.parse(gen_source)

    for node in ast.walk(gen_tree):
        assert not isinstance(node, (ast.Import, ast.ImportFrom)), "generated source must not import"
        if isinstance(node, ast.Name):
            assert node.id not in {
                "open",
                "exec",
                "eval",
                "compile",
                "__import__",
                "input",
                "globals",
                "locals",
                "vars",
                "breakpoint",
                "exit",
                "quit",
                "getattr",
                "setattr",
                "delattr",
            }, f"forbidden name: {node.id}"
            lower_id = node.id.lower()
            for forbidden_sub in ("receipt", "evaluator", "verify", "qualifies"):
                assert forbidden_sub not in lower_id, f"evaluator tampering token: {node.id}"
        if isinstance(node, ast.Attribute):
            assert not (node.attr.startswith("__") and node.attr.endswith("__")), f"dunder attr: {node.attr}"
            lower_attr = node.attr.lower()
            for forbidden_sub in ("receipt", "evaluator", "verify", "qualifies"):
                assert forbidden_sub not in lower_attr, f"evaluator tampering attribute: {node.attr}"
