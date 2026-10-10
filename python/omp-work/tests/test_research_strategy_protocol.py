"""R13 contract tests for the isolated strategy protocol."""

from __future__ import annotations

import json

import pytest

from omp_work.research.search import beam
from omp_work.research.strategy_protocol import (
    MAX_SOURCE_BYTES,
    config_candidate,
    load_strategy,
    run_candidate,
    run_with_fallback,
    sanitized_snapshot,
    validate_proposal,
)

_ALLOWED = {"open"}
_CAPS = {"cpu_seconds": 120}

_BEAM_SOURCE = """
def propose(steps, opened, *, seed=0, width=1):
    def rank(step_id):
        metric = steps[step_id].get("metric")
        if metric is None:
            return (1, 0.0, step_id)
        return (0, -metric, step_id)

    ranked = sorted(
        [step_id for step_id in opened if steps[step_id].get("infra_error") is None],
        key=rank,
    )
    for step_id in ranked[:width]:
        children = sorted(
            child for child in (steps[step_id].get("children") or []) if child not in opened
        )
        if children:
            return children[0], seed
    roots = sorted(
        step_id
        for step_id, node in steps.items()
        if node.get("parent_id") is None and step_id not in opened
    )
    if roots:
        return roots[0], seed
    return None, seed
"""

_RESOURCES_SOURCE = (
    'RESOURCES = {"cpu_seconds": 1000}\n' + _BEAM_SOURCE
)

_CRASH_SOURCE = """
def propose(steps, opened, *, seed=0, width=1):
    steps["A1"]["metric"] = -1
    steps["A1"]["children"].append("mutated")
    return 1 / 0
"""

_MUTATE_SOURCE = """
def propose(steps, opened, *, seed=0, width=1):
    steps["A1"]["metric"] = -1
    steps["A1"]["children"].append("mutated")
    return "A2", seed
"""


def _steps() -> dict[str, dict[str, object]]:
    return {
        "A1": {"parent_id": None, "metric": 0.4, "cost": 1, "children": ["A2"], "infra_error": None},
        "A2": {"parent_id": "A1", "metric": 0.9, "cost": 1, "children": [], "infra_error": None},
        "B1": {"parent_id": None, "metric": 0.7, "cost": 1, "children": [], "infra_error": None},
    }


def _copy(steps: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return json.loads(json.dumps(steps))


def _run(source: str, steps: dict[str, dict[str, object]], opened: set[str], **kwargs: object) -> dict[str, object]:
    return run_candidate(
        source,
        steps,
        opened,
        allowed_actions=kwargs.get("allowed_actions", _ALLOWED),
        resource_caps=kwargs.get("resource_caps", _CAPS),
    )


def test_beam_source_loads_and_returns_a_legal_child() -> None:
    steps = _steps()
    opened = {"A1"}
    before = _copy(steps)
    fn = load_strategy(_BEAM_SOURCE)
    assert callable(fn)
    assert set(fn.__globals__["__builtins__"]) == {
        "sorted",
        "min",
        "max",
        "len",
        "sum",
        "abs",
        "round",
        "range",
        "enumerate",
        "zip",
        "float",
        "int",
        "str",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "frozenset",
        "any",
        "all",
        "reversed",
    }

    result = _run(_BEAM_SOURCE, steps, opened)
    pick, pick_seed = beam(steps, opened, seed=0, width=1)
    assert result["status"] == "ok"
    assert result["reasons"] == []
    assert result["proposal"]["step_id"] == pick == "A2"
    assert result["proposal"]["step_id"] in steps["A1"]["children"]
    assert result["proposal"]["action"] == "open"
    assert result["proposal"]["resources"] == {}
    assert result["seed"] == pick_seed
    assert steps == before
    assert opened == {"A1"}


def test_load_rejects_broken_forbidden_and_oversized_source() -> None:
    steps = _steps()
    opened: set[str] = set()
    cases = {
        "def propose(:\n": "broken_code",
        "import os\ndef propose(steps, opened, *, seed=0, width=1):\n    return None, seed\n": "forbidden_import",
        "def propose(steps, opened, *, seed=0, width=1):\n    open('x')\n    return None, seed\n": "forbidden_name",
        "def propose(steps, opened, *, seed=0, width=1):\n    verify_receipt()\n    return None, seed\n": "evaluator_tampering",
        "def propose(steps, opened, *, seed, width):\n    return None, seed\n": "wrong_interface",
        "x" * 8193: "too_large",
    }
    assert len(("x" * 8193).encode("utf-8")) == 8193
    for source, reason in cases.items():
        with pytest.raises(ValueError, match=f"^{reason}$"):
            load_strategy(source)
        failed = _run(source, steps, opened)
        assert failed["status"] == "failed"
        assert failed["reason"] == reason
        assert failed["proposal"] is None

    body = "def propose(steps, opened, *, seed=0, width=1):\n    return None, seed\n"
    capped = body + ("#" * (MAX_SOURCE_BYTES - len(body.encode("utf-8"))))
    assert len(capped.encode("utf-8")) == MAX_SOURCE_BYTES
    assert callable(load_strategy(capped))


def test_over_cap_resources_are_rejected() -> None:
    steps = _steps()
    opened = {"A1"}
    result = _run(_RESOURCES_SOURCE, steps, opened)
    assert result["status"] == "rejected"
    assert result["reasons"] == ["resource_exceeded"]
    assert result["proposal"]["resources"] == {"cpu_seconds": 1000}
    assert result["proposal"]["action"] == "open"
    assert result["proposal"]["step_id"] == "A2"


def test_validate_proposal_flags_node_parent_action_and_resources() -> None:
    steps = _steps()
    assert validate_proposal(
        {"step_id": "NOPE", "action": "open", "resources": {}},
        steps,
        {"A1"},
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["unknown_node"]
    assert validate_proposal(
        {"step_id": "A2", "action": "open", "resources": {}},
        steps,
        set(),
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["parent_not_open"]
    assert validate_proposal(
        {"step_id": "A2", "action": "delete", "resources": {}},
        steps,
        {"A1"},
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["action_not_allowed"]
    assert validate_proposal(
        {"step_id": "A2", "action": "open", "resources": {"cpu_seconds": 1000}},
        steps,
        {"A1"},
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["resource_exceeded"]
    assert validate_proposal(
        {
            "step_id": "A2",
            "action": "delete",
            "resources": {"cpu_seconds": 1000, "gpu": 1},
        },
        steps,
        set(),
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["parent_not_open", "action_not_allowed", "resource_exceeded"]
    assert validate_proposal(
        {"step_id": "A2", "action": "open", "resources": {"cpu_seconds": 120}},
        steps,
        {"A1"},
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == []
    assert validate_proposal(
        {"step_id": "B1", "action": "open", "resources": {}},
        steps,
        set(),
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == []
    huge = {"step_id": "x" * 5000, "action": "open", "resources": {}}
    assert validate_proposal(
        huge,
        steps,
        set(),
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    ) == ["proposal_too_large", "unknown_node"]
    assert "bad_schema" in validate_proposal(
        {"step_id": "A2", "action": "open"},
        steps,
        {"A1"},
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    )


def test_crash_leaves_the_tree_and_falls_back_to_beam() -> None:
    steps = _steps()
    opened = {"A1"}
    before = _copy(steps)
    crashed = _run(_CRASH_SOURCE, steps, opened)
    assert crashed["status"] == "failed"
    assert crashed["reason"] == "crashed"
    assert steps == before
    assert opened == {"A1"}

    fallback = run_with_fallback(
        _CRASH_SOURCE,
        steps,
        opened,
        fallback_fn=beam,
        seed=0,
        width=1,
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    )
    assert steps == before
    assert opened == {"A1"}
    assert fallback["status"] == "fallback"
    assert fallback["proposal"] == beam(steps, opened, seed=0, width=1)
    assert fallback["proposal"][0] == "A2"


def test_snapshot_writes_do_not_touch_the_caller() -> None:
    steps = _steps()
    opened = {"A1"}
    before = _copy(steps)
    copied, frozen = sanitized_snapshot(steps, opened)
    assert copied == steps
    assert copied is not steps
    assert copied["A1"] is not steps["A1"]
    assert isinstance(frozen, frozenset)
    assert frozen == frozenset(opened)
    copied["A1"]["metric"] = -1
    copied["A1"]["children"].append("ZZ")
    assert steps == before

    result = _run(_MUTATE_SOURCE, steps, opened)
    assert result["status"] == "ok"
    assert result["proposal"]["step_id"] == "A2"
    assert steps == before
    assert opened == {"A1"}


def test_ok_candidate_is_not_replaced_by_fallback() -> None:
    steps = _steps()
    opened = {"A1"}
    result = run_with_fallback(
        _BEAM_SOURCE,
        steps,
        opened,
        fallback_fn=beam,
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    )
    assert result["status"] == "ok"
    assert result["proposal"]["step_id"] == "A2"


def test_config_candidate_digest_changes_with_width() -> None:
    parent = "search.beam"
    one = config_candidate({"width": 1}, parent)
    two = config_candidate({"width": 2}, parent)
    again = config_candidate({"width": 1}, parent)
    assert one["kind"] == "config"
    assert one["parent"] == parent
    assert one["parameters"] == {"width": 1}
    assert two["parameters"] == {"width": 2}
    assert one["sha256"] != two["sha256"]
    assert one["sha256"] == again["sha256"]
    assert len(one["sha256"]) == 64
