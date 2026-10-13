from __future__ import annotations

import math

import pytest

from omp_work.research.search import fixed, greedy, next_step, run_policy


def _node(parent=None, metric=None, cost=1, children=(), infra=None):
    return {"parent_id": parent, "metric": metric, "cost": cost, "children": list(children), "infra_error": infra}


def test_fixed_follows_insertion_order_and_parent_legality():
    steps = {
        "z": _node(),
        "b": _node(parent="z"),
        "a": _node(parent="q"),
        "q": _node(),
    }
    assert fixed(steps, set(), seed=7) == ("z", 7)
    assert fixed(steps, {"z"}) == ("b", 0)
    assert fixed(steps, {"z", "b"}) == ("q", 0)
    assert fixed(steps, {"z", "b", "q"}) == ("a", 0)
    assert fixed(steps, set(steps)) == (None, 0)
    assert run_policy("fixed", steps, budget=10) == ["z", "b", "q", "a"]


def test_next_step_resolves_names_and_callables():
    steps = {"r": _node(children=["c"]), "c": _node(parent="r")}
    assert next_step("fixed", steps, set(), seed=3) == ("r", 3)
    assert next_step(fixed, steps, {"r"}) == ("c", 0)
    assert next_step("random", steps, set(), seed=0)[1] != 0
    assert next_step(lambda s, o, *, seed, width: ("r", seed + 1), steps, set(), seed=1) == ("r", 2)


def test_next_step_rejects_duplicate_and_illegal_callable_choices():
    steps = {"r": _node(children=["c"]), "c": _node(parent="r")}
    assert next_step(lambda s, o, *, seed, width: ("r", seed), steps, {"r"}) == (None, 0)
    assert next_step(lambda s, o, *, seed, width: ("c", seed), steps, set()) == (None, 0)
    assert run_policy(lambda s, o, *, seed, width: ("c", seed), steps, budget=10) == []


def test_unknown_policy_raises():
    with pytest.raises(ValueError, match="unknown policy 'nope'"):
        next_step("nope", {}, set())
    with pytest.raises(ValueError, match="unknown policy"):
        run_policy("nope", {}, budget=1)


def _two_parents(ma, mb, infra_a=None):
    return {
        "A": _node(metric=ma, children=["a1"], infra=infra_a),
        "B": _node(metric=mb, children=["b1"]),
        "a1": _node(parent="A"),
        "b1": _node(parent="B"),
    }


def test_greedy_child_switches_with_parent_ranking():
    opened = {"A", "B"}
    assert greedy(_two_parents(0.9, 0.1), opened)[0] == "a1"
    assert greedy(_two_parents(0.1, 0.9), opened)[0] == "b1"
    assert next_step("greedy", _two_parents(0.1, 0.9), opened)[0] == "b1"


def test_greedy_excludes_infra_parents():
    assert greedy(_two_parents(0.9, 0.1, infra_a="boom"), {"A", "B"})[0] == "b1"


def _chain(costs):
    return {f"s{i}": _node(cost=c) for i, c in enumerate(costs)}


def test_fractional_budget_uses_fsum():
    costs = [0.1, 0.2, 0.1]
    total = math.fsum(costs)
    steps = _chain(costs)
    assert run_policy("fixed", steps, budget=total) == ["s0", "s1", "s2"]
    assert run_policy("fixed", steps, budget=math.nextafter(total, 0)) == ["s0", "s1"]


def test_zero_budget_and_prospective_overflow():
    steps = _chain([1, 1])
    assert run_policy("fixed", steps, budget=0) == []
    # second trial would overflow; cheaper later node is not substituted
    steps = {"a": _node(cost=3), "b": _node(cost=3), "c": _node(cost=1)}
    assert run_policy("fixed", steps, budget=5) == ["a"]
