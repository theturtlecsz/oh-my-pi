"""Adaptive search policies over a research step tree (R10, OMP-316)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any

__all__ = [
    "admit",
    "alternatives",
    "ancestry",
    "beam",
    "best_of_n",
    "cost_aware",
    "fixed",
    "greedy",
    "next_step",
    "prune",
    "random",
    "replicate",
    "run_policy",
    "typed_params",
    "winner",
]

Step = MutableMapping[str, Any]
Steps = MutableMapping[str, Step]
PolicyFn = Callable[..., tuple[str | None, int]]


def _is_infra(node: Step) -> bool:
    return node.get("infra_error") is not None


def _unopened_roots(steps: Steps, opened: set[str]) -> list[str]:
    return sorted(step_id for step_id, node in steps.items() if node.get("parent_id") is None and step_id not in opened)


def _unopened_children(steps: Steps, step_id: str, opened: set[str]) -> list[str]:
    return sorted(child for child in steps[step_id].get("children") or () if child not in opened)


def _metric_rank(steps: Steps) -> Callable[[str], tuple[int, float, str]]:
    def key(step_id: str) -> tuple[int, float, str]:
        metric = steps[step_id].get("metric")
        if metric is None:
            return (1, 0.0, step_id)
        return (0, -metric, step_id)

    return key


def _ranked_open(steps: Steps, opened: set[str]) -> list[str]:
    return sorted((step_id for step_id in opened if not _is_infra(steps[step_id])), key=_metric_rank(steps))


def best_of_n(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Open the lowest-id unopened root while fewer than width roots are open; never a child."""
    open_roots = sum(1 for step_id in opened if steps[step_id].get("parent_id") is None)
    if open_roots >= width:
        return None, seed
    roots = _unopened_roots(steps, opened)
    return (roots[0] if roots else None), seed


def greedy(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Open a root first, then the unopened child of the highest-metric open non-infra node."""
    if not opened:
        roots = _unopened_roots(steps, opened)
        return (roots[0] if roots else None), seed
    ranked = _ranked_open(steps, opened)
    if ranked:
        children = _unopened_children(steps, ranked[0], opened)
        if children:
            return children[0], seed
    return None, seed


def beam(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Keep the width best open non-infra nodes and extend the best of those that has a child."""
    for step_id in _ranked_open(steps, opened)[:width]:
        children = _unopened_children(steps, step_id, opened)
        if children:
            return children[0], seed
    roots = _unopened_roots(steps, opened)
    return (roots[0] if roots else None), seed


def cost_aware(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Extend the open non-infra node with the highest metric/cost; else the cheapest root."""
    best_id: str | None = None
    best_ratio = 0.0
    for step_id in opened:
        node = steps[step_id]
        metric = node.get("metric")
        if _is_infra(node) or metric is None:
            continue
        ratio = metric / node["cost"]
        if best_id is None or ratio > best_ratio or (ratio == best_ratio and step_id < best_id):
            best_id, best_ratio = step_id, ratio
    if best_id is not None:
        children = _unopened_children(steps, best_id, opened)
        if children:
            return children[0], seed
    roots = _unopened_roots(steps, opened)
    if roots:
        return min(roots, key=lambda step_id: (steps[step_id]["cost"], step_id)), seed
    return None, seed


def random(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Pick seed % len over sorted legal ids, then advance the LCG seed."""
    legal = sorted(
        [*_unopened_roots(steps, opened), *(child for step_id in opened for child in _unopened_children(steps, step_id, opened))]
    )
    if not legal:
        return None, seed
    pick = legal[seed % len(legal)]
    return pick, (seed * 1103515245 + 12345) % 2**31


def fixed(steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1) -> tuple[str | None, int]:
    """Open the first unopened legal node in mapping insertion order; seed unchanged."""
    for step_id, node in steps.items():
        parent = node.get("parent_id")
        if step_id not in opened and (parent is None or parent in opened):
            return step_id, seed
    return None, seed


_POLICIES: dict[str, PolicyFn] = {
    "fixed": fixed,
    "best_of_n": best_of_n,
    "greedy": greedy,
    "beam": beam,
    "cost_aware": cost_aware,
    "random": random,
}


def next_step(
    policy: str | PolicyFn, steps: Steps, opened: set[str], *, seed: int = 0, width: int = 1
) -> tuple[str | None, int]:
    """Resolve policy and return its next legal choice (or None) with the advanced seed."""
    if isinstance(policy, str):
        if policy not in _POLICIES:
            raise ValueError(f"unknown policy {policy!r}; expected one of {sorted(_POLICIES)}")
        fn = _POLICIES[policy]
    else:
        fn = policy
    step_id, seed = fn(steps, opened, seed=seed, width=width)
    if step_id is None:
        return None, seed
    parent = steps[step_id].get("parent_id")
    if step_id in opened or (parent is not None and parent not in opened):
        return None, seed
    return step_id, seed


def run_policy(policy: str | PolicyFn, steps: Steps, *, budget: float, seed: int = 0, width: int = 1) -> list[str]:
    """Open nodes one at a time until the policy stops or the next node's cost would exceed the budget."""
    opened: list[str] = []
    opened_set: set[str] = set()
    costs: list[float] = []
    while True:
        step_id, seed = next_step(policy, steps, opened_set, seed=seed, width=width)
        if step_id is None:
            break
        cost = steps[step_id]["cost"]
        if math.fsum([*costs, cost]) > budget:
            break
        opened.append(step_id)
        opened_set.add(step_id)
        costs.append(cost)
    return opened


def admit(open_cost: float, proposals: Sequence[Step], budget: float) -> dict[str, Any]:
    """Admit every proposal, or none with status resource_exceeded when the batch overflows budget."""
    if open_cost + sum(proposal["cost"] for proposal in proposals) > budget:
        return {"status": "resource_exceeded", "admitted": []}
    return {"status": "ok", "admitted": list(proposals)}


def winner(opened: Mapping[str, Step]) -> str | None:
    """Highest-metric opened node that has a metric and no infra_error (tie: lowest id)."""
    best_id: str | None = None
    best_metric = 0.0
    for step_id, node in opened.items():
        metric = node.get("metric")
        if metric is None or _is_infra(node):
            continue
        if best_id is None or metric > best_metric or (metric == best_metric and step_id < best_id):
            best_id, best_metric = step_id, metric
    return best_id


def ancestry(steps: Steps, step_id: str) -> list[str]:
    """List step_id and its parents up to the root."""
    chain: list[str] = []
    current: str | None = step_id
    while current is not None:
        chain.append(current)
        current = steps[current].get("parent_id")
    return chain


def alternatives(steps: Steps, parent_id: str) -> list[str]:
    """List the children of parent_id."""
    return list(steps[parent_id].get("children") or ())


def prune(steps: Steps, step_id: str) -> Steps:
    """Flag a node as pruned without removing it from the tree."""
    steps[step_id]["pruned"] = True
    return steps


def replicate(steps: Steps, step_id: str) -> str:
    """Add a {step_id}-rep sibling with the same metric, cost 1, and the same parent."""
    node = steps[step_id]
    replica_id = f"{step_id}-rep"
    steps[replica_id] = {
        "parent_id": node.get("parent_id"),
        "metric": node.get("metric"),
        "cost": 1,
        "children": [],
        "infra_error": None,
    }
    parent = node.get("parent_id")
    if parent is not None:
        steps[parent].setdefault("children", []).append(replica_id)
    return replica_id


def typed_params(space: Mapping[str, Sequence[Any]], step: int) -> dict[str, Any]:
    """Mixed-radix parameter grid: sorted keys, last key fastest, index wrapped with modulo."""
    keys = sorted(space)
    total = 1
    for key in keys:
        total *= len(space[key])
    index = step % total
    params: dict[str, Any] = {}
    for key in reversed(keys):
        values = space[key]
        params[key] = values[index % len(values)]
        index //= len(values)
    return {"optimizer": "optuna-bridge.v1", "space": space, "index": step, "params": params}
