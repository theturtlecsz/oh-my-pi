"""OMP-317-s03: R13 mechanism candidate lifecycle, development comparison, and fresh-cohort confirmation."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.research import search
from omp_work.research.mechanism_eval import (
    Candidate,
    ConfirmationCohort,
    advance,
    confirm,
    development_comparison,
)
from omp_work.v1 import canonical

_SOURCE = "def policy(steps, opened, *, seed=0, width=1): ..."


def _source_sha256(text: str = _SOURCE) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate(state: str = "proposed", *, contract_passed: bool = False, source: str = _SOURCE) -> Candidate:
    return Candidate(
        id="cand-1",
        parent=None,
        source_sha256=_source_sha256(source),
        interface_version=1,
        declared_capabilities=("search",),
        contract_passed=contract_passed,
        state=state,
    )


def _steps() -> dict[str, dict[str, object]]:
    return {
        "A1": {"parent_id": None, "metric": 0.9, "cost": 1, "children": ["A2"], "infra_error": None},
        "A2": {"parent_id": "A1", "metric": 0.1, "cost": 1, "children": [], "infra_error": None},
        "B1": {"parent_id": None, "metric": 0.8, "cost": 1, "children": ["B2"], "infra_error": None},
        "B2": {"parent_id": "B1", "metric": 0.95, "cost": 1, "children": [], "infra_error": None},
    }


def _task() -> dict[str, object]:
    return {"steps": _steps(), "budget": 4, "width": 1}


def _restart(steps, opened, *, seed=0, width=1):
    """Restart-style candidate: open every root before extending the best open branch."""
    roots = sorted(step_id for step_id, node in steps.items() if node.get("parent_id") is None)
    for root in roots:
        if root not in opened:
            return root, seed
    ranked = sorted(opened, key=lambda step_id: (-(steps[step_id].get("metric") or 0.0), step_id))
    for step_id in ranked:
        children = sorted(child for child in steps[step_id].get("children") or () if child not in opened)
        if children:
            return children[0], seed
    return None, seed


def test_legal_lifecycle_chain() -> None:
    candidate = _candidate()
    candidate = advance(candidate, "inspect")
    assert candidate.state == "inspected"
    candidate = advance(candidate, "contract_pass")
    assert candidate.state == "contract_tested"
    assert candidate.contract_passed is True
    candidate = advance(candidate, "development")
    assert candidate.state == "development_compared"
    candidate = advance(candidate, "confirm")
    assert candidate.state == "confirmed"


def test_illegal_transitions_raise() -> None:
    with pytest.raises(ValueError, match="illegal_transition"):
        advance(_candidate("proposed"), "confirm")
    with pytest.raises(ValueError, match="illegal_transition"):
        advance(_candidate("development_compared"), "inspect")


def test_contract_fail_rejects_without_passing() -> None:
    candidate = advance(advance(_candidate(), "inspect"), "contract_fail")
    assert candidate.state == "rejected"
    assert candidate.contract_passed is False


def test_development_comparison_scores_restart_over_greedy() -> None:
    result = development_comparison(_restart, search.greedy, {"dev": _task()}, research_cost=1.0, comparison_cost=1.0)
    assert result["wins"] == 1
    assert result["tasks"] == 1
    record = result["per_task"]["dev"]
    assert record["candidate_metric"] == 0.95
    assert record["baseline_metric"] == 0.9
    assert record["candidate_cost"] == 4.0
    assert record["baseline_cost"] == 2.0
    assert record["win"] is True
    cost = result["cost"]
    assert cost["research"] == 2.0
    assert cost["comparison"] == 1.0
    assert cost["candidate_trials"] == 4.0
    assert cost["baseline_trials"] == 2.0
    assert cost["total"] == 9.0


def _tie_steps() -> dict[str, dict[str, object]]:
    return {"R": {"parent_id": None, "metric": 0.5, "cost": 1, "children": [], "infra_error": None}}


def test_equal_metrics_are_not_a_win() -> None:
    tasks = {"dev": {"steps": _tie_steps(), "budget": 1, "width": 1}}
    result = development_comparison(search.greedy, search.greedy, tasks, research_cost=0.0, comparison_cost=0.0)
    assert result["wins"] == 0
    assert result["per_task"]["dev"]["win"] is False


def test_none_candidate_metric_is_not_a_win() -> None:
    def idle(steps, opened, *, seed=0, width=1):
        return None, seed

    tasks = {"dev": {"steps": _tie_steps(), "budget": 1, "width": 1}}
    result = development_comparison(idle, search.greedy, tasks, research_cost=0.0, comparison_cost=0.0)
    record = result["per_task"]["dev"]
    assert record["candidate_metric"] is None
    assert record["baseline_metric"] == 0.5
    assert record["win"] is False
    assert result["wins"] == 0


def test_cohort_overlapping_dev_ids_is_not_fresh() -> None:
    with pytest.raises(ValueError, match="cohort_not_fresh"):
        ConfirmationCohort(dev_task_ids=("dev",), task_ids=("dev", "fresh-1"), required_wins=1)


def test_cohort_bad_requirement() -> None:
    with pytest.raises(ValueError, match="bad_requirement"):
        ConfirmationCohort(dev_task_ids=("dev",), task_ids=("fresh-1",), required_wins=0)
    with pytest.raises(ValueError, match="bad_requirement"):
        ConfirmationCohort(dev_task_ids=("dev",), task_ids=("fresh-1",), required_wins=2)


def test_confirm_rejects_tampered_source() -> None:
    cohort = ConfirmationCohort(dev_task_ids=("dev",), task_ids=("fresh-1",), required_wins=1)
    with pytest.raises(ValueError, match="changed_candidate"):
        confirm(
            _candidate("development_compared", contract_passed=True),
            cohort,
            _restart,
            search.greedy,
            {"fresh-1": _task()},
            frozen_source="tampered source",
        )


def test_confirm_pins_policy_across_fresh_cohort() -> None:
    cohort = ConfirmationCohort(dev_task_ids=("dev",), task_ids=("fresh-1", "fresh-2"), required_wins=2)
    candidate = _candidate("development_compared", contract_passed=True)
    tasks = {"fresh-1": _task(), "fresh-2": _task()}
    result = confirm(
        candidate,
        cohort,
        _restart,
        search.greedy,
        tasks,
        frozen_source=_SOURCE,
        research_cost=1.0,
        comparison_cost=1.0,
    )
    assert result["status"] == "confirmed"
    assert result["wins"] == 2
    assert result["required_wins"] == 2
    assert result["candidate_sha256"] == candidate.source_sha256
    assert result["cohort_sha256"] == canonical.sha256(["fresh-1", "fresh-2"])
    comparison = result["comparison"]
    assert comparison["tasks"] == 2
    assert set(comparison["per_task"]) == {"fresh-1", "fresh-2"}
    cost = comparison["cost"]
    assert cost["research"] == 4.0
    assert cost["comparison"] == 2.0
    assert cost["candidate_trials"] == 8.0
    assert cost["baseline_trials"] == 4.0
    assert cost["total"] == 18.0


def test_confirm_rejects_when_wins_fall_short() -> None:
    cohort = ConfirmationCohort(dev_task_ids=("dev",), task_ids=("fresh-1",), required_wins=1)
    result = confirm(
        _candidate("development_compared", contract_passed=True),
        cohort,
        search.greedy,
        search.greedy,
        {"fresh-1": {"steps": _tie_steps(), "budget": 1, "width": 1}},
        frozen_source=_SOURCE,
    )
    assert result["status"] == "rejected"
    assert result["wins"] == 0
