"""OMP-316-s03: R10 adaptive search policies."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.research.search import (
    admit,
    alternatives,
    ancestry,
    greedy,
    prune,
    replicate,
    run_policy,
    typed_params,
    winner,
)


def _steps() -> dict[str, dict[str, object]]:
    return {
        "A1": {"parent_id": None, "metric": 0.4, "cost": 1, "children": ["A2"], "infra_error": None},
        "A2": {"parent_id": "A1", "metric": 0.9, "cost": 1, "children": [], "infra_error": None},
        "B1": {"parent_id": None, "metric": 0.7, "cost": 1, "children": [], "infra_error": None},
        "INF": {"parent_id": None, "metric": 1.0, "cost": 1, "children": ["INF2"], "infra_error": "disk"},
        "INF2": {"parent_id": "INF", "metric": 0.1, "cost": 1, "children": [], "infra_error": None},
    }


def _opened(steps: dict[str, dict[str, object]], ids: list[str]) -> dict[str, dict[str, object]]:
    return {step_id: steps[step_id] for step_id in ids}


def test_best_of_n_opens_width_roots() -> None:
    steps = _steps()
    opened = run_policy("best_of_n", steps, budget=3, width=2)
    assert opened == ["A1", "B1"]
    assert steps[winner(_opened(steps, opened))]["metric"] == 0.7


def test_greedy_extends_best_node_and_skips_infra() -> None:
    steps = _steps()
    opened = run_policy("greedy", steps, budget=3)
    assert opened == ["A1", "A2"]
    assert "INF2" not in opened
    assert steps[winner(_opened(steps, opened))]["metric"] == 0.9


def test_beam_width_two_keeps_best_branches() -> None:
    steps = _steps()
    opened = run_policy("beam", steps, budget=3, width=2)
    assert "A2" in opened
    assert opened == ["A1", "A2", "B1"]
    assert steps[winner(_opened(steps, opened))]["metric"] == 0.9


def test_cost_aware_opens_cheapest_root_first() -> None:
    steps = {
        "C": {"parent_id": None, "metric": 0.9, "cost": 2, "children": [], "infra_error": None},
        "D": {"parent_id": None, "metric": 0.5, "cost": 1, "children": [], "infra_error": None},
    }
    opened = run_policy("cost_aware", steps, budget=3)
    assert opened[0] == "D"
    assert opened == ["D", "C"]


def test_admit_resource_exceeded_admits_nothing() -> None:
    proposals = [{"cost": 1}, {"cost": 1}]
    result = admit(open_cost=2, proposals=proposals, budget=3)
    assert result["status"] == "resource_exceeded"
    assert result["admitted"] == []


def test_admit_within_budget_admits_all() -> None:
    proposals = [{"cost": 1}, {"cost": 1}]
    result = admit(open_cost=1, proposals=proposals, budget=3)
    assert result["status"] == "ok"
    assert result["admitted"] == proposals


def test_zero_budget_opens_nothing() -> None:
    for policy in ("best_of_n", "greedy", "beam", "cost_aware", "random"):
        assert run_policy(policy, _steps(), budget=0, width=2) == []


def test_winner_excludes_infra_nodes() -> None:
    steps = _steps()
    assert winner({"A2": steps["A2"], "INF": steps["INF"]}) == "A2"


def test_winner_requires_metric() -> None:
    steps = _steps()
    opened = {"X": {"parent_id": None, "metric": None, "cost": 1, "children": [], "infra_error": None}, "A2": steps["A2"]}
    assert winner(opened) == "A2"
    assert winner({"X": opened["X"]}) is None
    assert winner({"INF": steps["INF"]}) is None


def test_prune_keeps_node_in_ancestry_and_alternatives() -> None:
    steps = _steps()
    prune(steps, "A2")
    assert steps["A2"]["pruned"] is True
    assert ancestry(steps, "A2") == ["A2", "A1"]
    assert "A2" in ancestry(steps, "A2")
    assert "A2" in alternatives(steps, "A1")


def test_replicate_adds_replica_sibling() -> None:
    steps = _steps()
    replica_id = replicate(steps, "A2")
    assert replica_id == "A2-rep"
    replica = steps["A2-rep"]
    assert replica["metric"] == 0.9
    assert replica["cost"] == 1
    assert replica["parent_id"] == "A1"
    assert "A2-rep" in alternatives(steps, "A1")


def test_random_seed_zero_is_deterministic() -> None:
    first = run_policy("random", _steps(), budget=3, seed=0)
    second = run_policy("random", _steps(), budget=3, seed=0)
    assert first == second
    assert len(first) > 0


def test_typed_params_grid() -> None:
    space = {"a": [0, 1], "b": [2]}
    first = typed_params(space, 0)
    assert first["params"] == {"a": 0, "b": 2}
    assert typed_params(space, 1)["params"] == {"a": 1, "b": 2}
    wrapped = typed_params(space, 2)
    assert wrapped["params"] == first["params"]
    assert wrapped["index"] == 2
    assert first["optimizer"] == "optuna-bridge.v1"
    assert first["space"] == space
    assert first["index"] == 0


def test_replay_same_arguments_returns_same_ids() -> None:
    steps = _steps()
    opened = run_policy("greedy", steps, budget=3)
    assert run_policy("greedy", steps, budget=3) == opened


def test_run_policy_accepts_policy_callable() -> None:
    assert run_policy(greedy, _steps(), budget=3) == run_policy("greedy", _steps(), budget=3)
