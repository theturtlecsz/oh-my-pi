"""Mechanism candidate lifecycle, development comparison, and fresh-cohort confirmation (R13, OMP-317)."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from omp_work.research import search
from omp_work.v1 import canonical

__all__ = [
    "Candidate",
    "ConfirmationCohort",
    "advance",
    "confirm",
    "development_comparison",
]

Task = Mapping[str, Any]
Tasks = Mapping[str, Task]

_TRANSITIONS: dict[tuple[str, str], str] = {
    ("proposed", "inspect"): "inspected",
    ("inspected", "contract_pass"): "contract_tested",
    ("inspected", "contract_fail"): "rejected",
    ("contract_tested", "development"): "development_compared",
    ("development_compared", "confirm"): "confirmed",
    ("development_compared", "reject"): "rejected",
}


@dataclass(frozen=True)
class Candidate:
    id: str
    parent: str | None
    source_sha256: str
    interface_version: int
    declared_capabilities: tuple[str, ...]
    contract_passed: bool
    state: str


def advance(candidate: Candidate, event: str) -> Candidate:
    """Move a candidate along the lifecycle; anything off the chain raises illegal_transition."""
    state = _TRANSITIONS.get((candidate.state, event))
    if state is None:
        raise ValueError("illegal_transition")
    return replace(
        candidate,
        state=state,
        contract_passed=candidate.contract_passed or event == "contract_pass",
    )


@dataclass(frozen=True)
class ConfirmationCohort:
    dev_task_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    required_wins: int

    def __post_init__(self) -> None:
        if set(self.dev_task_ids) & set(self.task_ids):
            raise ValueError("cohort_not_fresh")
        if not 1 <= self.required_wins <= len(self.task_ids):
            raise ValueError("bad_requirement")


def _opened_metric(steps: search.Steps, opened: list[str]) -> float | None:
    winner_id = search.winner({step_id: steps[step_id] for step_id in opened})
    if winner_id is None:
        return None
    return steps[winner_id]["metric"]


def _trials_cost(steps: search.Steps, opened: list[str]) -> float:
    return float(sum(steps[step_id]["cost"] for step_id in opened))


def development_comparison(
    candidate_fn: search.PolicyFn,
    baseline_fn: search.PolicyFn,
    tasks: Tasks,
    *,
    research_cost: float,
    comparison_cost: float,
) -> dict[str, Any]:
    """Run candidate and baseline policies over fresh copies of each task tree and score both.

    Research cost accrues per policy run (candidate and baseline), comparison cost per task;
    the total folds in both alongside the node-cost trials.
    """
    per_task: dict[str, dict[str, Any]] = {}
    wins = 0
    research = 0.0
    comparison = 0.0
    candidate_trials = 0.0
    baseline_trials = 0.0
    for task_id in sorted(tasks):
        task = tasks[task_id]
        candidate_steps = copy.deepcopy(task["steps"])
        baseline_steps = copy.deepcopy(task["steps"])
        candidate_opened = search.run_policy(candidate_fn, candidate_steps, budget=task["budget"], width=task["width"])
        baseline_opened = search.run_policy(baseline_fn, baseline_steps, budget=task["budget"], width=task["width"])
        candidate_metric = _opened_metric(candidate_steps, candidate_opened)
        baseline_metric = _opened_metric(baseline_steps, baseline_opened)
        candidate_cost = _trials_cost(candidate_steps, candidate_opened)
        baseline_cost = _trials_cost(baseline_steps, baseline_opened)
        win = candidate_metric is not None and (baseline_metric is None or candidate_metric > baseline_metric)
        wins += int(win)
        research += 2 * research_cost
        comparison += comparison_cost
        candidate_trials += candidate_cost
        baseline_trials += baseline_cost
        per_task[task_id] = {
            "candidate_metric": candidate_metric,
            "baseline_metric": baseline_metric,
            "candidate_cost": candidate_cost,
            "baseline_cost": baseline_cost,
            "win": win,
        }
    return {
        "wins": wins,
        "tasks": len(per_task),
        "per_task": per_task,
        "cost": {
            "research": research,
            "comparison": comparison,
            "candidate_trials": candidate_trials,
            "baseline_trials": baseline_trials,
            "total": research + comparison + candidate_trials + baseline_trials,
        },
    }


def confirm(
    candidate: Candidate,
    cohort: ConfirmationCohort,
    candidate_fn: search.PolicyFn,
    baseline_fn: search.PolicyFn,
    tasks: Tasks,
    *,
    frozen_source: str,
    research_cost: float = 0.0,
    comparison_cost: float = 0.0,
) -> dict[str, Any]:
    """Confirm a candidate on a fresh cohort with the candidate's source and policy pinned."""
    if hashlib.sha256(frozen_source.encode("utf-8")).hexdigest() != candidate.source_sha256:
        raise ValueError("changed_candidate")
    cohort_tasks = {task_id: tasks[task_id] for task_id in cohort.task_ids}
    comparison = development_comparison(
        candidate_fn,
        baseline_fn,
        cohort_tasks,
        research_cost=research_cost,
        comparison_cost=comparison_cost,
    )
    wins = comparison["wins"]
    return {
        "status": "confirmed" if wins >= cohort.required_wins else "rejected",
        "wins": wins,
        "required_wins": cohort.required_wins,
        "candidate_sha256": candidate.source_sha256,
        "cohort_sha256": canonical.sha256(sorted(cohort.task_ids)),
        "comparison": comparison,
    }
