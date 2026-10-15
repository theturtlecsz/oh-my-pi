"""Frozen, test-only scored trees for OMP-606 mechanism-family selection (not a fixture of any run)."""

from __future__ import annotations

from typing import Any


def _node(
    parent: str | None = None,
    children: tuple[str, ...] = (),
    metric: float | None = None,
    infra_error: str | None = None,
) -> dict[str, Any]:
    return {
        "parent_id": parent,
        "children": list(children),
        "metric": metric,
        "infra_error": infra_error,
    }


# Each case: steps, opened, width. ``designed_to_differ`` marks root-vs-child traps where the
# two production rules must select differently at thresholds the open parent clears.
CASES: dict[str, dict[str, Any]] = {
    "root_vs_child_trap": {
        "steps": {
            "r1": _node(None, ("r1a", "r1b"), 0.9),
            "r1a": _node("r1"),
            "r1b": _node("r1"),
            "r2": _node(None),
        },
        "opened": ["r1"],
        "width": 1,
        "designed_to_differ": True,
    },
    "gradient_between_thresholds": {
        "steps": {
            "r1": _node(None, ("c1",), 0.6),
            "c1": _node("r1"),
            "r2": _node(None),
        },
        "opened": ["r1"],
        "width": 1,
        "designed_to_differ": True,
    },
    "tied_metrics": {
        "steps": {
            "ra": _node(None, ("ra1",), 0.8),
            "rb": _node(None, ("rb1",), 0.8),
            "ra1": _node("ra"),
            "rb1": _node("rb"),
        },
        "opened": ["ra", "rb"],
        "width": 1,
        "designed_to_differ": False,
    },
    "tied_unopened_roots": {
        "steps": {"r2": _node(None), "r1": _node(None), "r3": _node(None)},
        "opened": [],
        "width": 1,
        "designed_to_differ": False,
    },
    "missing_metric": {
        "steps": {
            "r1": _node(None, ("c1",), None),
            "c1": _node("r1"),
        },
        "opened": ["r1"],
        "width": 1,
        "designed_to_differ": False,
    },
    "infra_error_trap": {
        "steps": {
            "r1": _node(None, ("c1",), 0.95, infra_error="timeout"),
            "c1": _node("r1"),
            "r2": _node(None, ("c2",), 0.3),
            "c2": _node("r2"),
        },
        "opened": ["r1", "r2"],
        "width": 1,
        "designed_to_differ": False,
    },
    "width_two": {
        "steps": {
            "ra": _node(None, ("ra1",), 0.9),
            "rb": _node(None, ("rb1",), 0.8),
            "ra1": _node("ra", ("ra1x",), 0.1),
            "ra1x": _node("ra1"),
            "rb1": _node("rb"),
        },
        "opened": ["ra", "rb", "ra1"],
        "width": 2,
        "designed_to_differ": False,
    },
    "exhausted_tree": {
        "steps": {"r1": _node(None, ("c1",), 0.9), "c1": _node("r1", (), 0.9)},
        "opened": ["r1", "c1"],
        "width": 1,
        "designed_to_differ": False,
    },
}
