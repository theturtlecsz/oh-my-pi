"""OMP-606-s01: two generator productions yield behaviourally distinct, protocol-valid policies."""

from __future__ import annotations

import copy
import hashlib
import math

import pytest

from omp_work.research import mechanism
from omp_work.research.mechanism_recipe import (
    ADAPTIVE_RESTART,
    FAMILIES,
    ROOT_FIRST,
    THRESHOLD_GRID,
    canonical_recipe,
)
from omp_work.research.strategy_protocol import run_candidate
from omp_work.v1 import canonical

from mechanism_cases_606 import CASES

SPEC = {"name": "s", "interface_version": 1}
CAPS = {"cpu_seconds": 120}


def _select(source: str, case: dict) -> str | None:
    steps = copy.deepcopy(case["steps"])
    result = run_candidate(
        source,
        steps,
        case["opened"],
        width=case["width"],
        allowed_actions=("open",),
        resource_caps=CAPS,
    )
    assert result["status"] == "ok", (result["status"], result["reasons"])
    assert steps == case["steps"]
    return result["proposal"]["step_id"]


def _selections(family: str) -> dict[float, dict[str, str | None]]:
    out: dict[float, dict[str, str | None]] = {}
    for policy in mechanism.generation_population(SPEC, family):
        out[policy["recipe"]["min_metric"]] = {
            name: _select(policy["source"], case) for name, case in CASES.items()
        }
    return out


def _fingerprint(selections: dict) -> str:
    return canonical.sha256(selections)


def test_designed_case_selects_root_versus_child() -> None:
    case = CASES["root_vs_child_trap"]
    adaptive = _select(mechanism.generation_job(SPEC, {"min_metric": 0.5}), case)
    root_first = _select(
        mechanism.generation_job(SPEC, {"family": ROOT_FIRST, "min_metric": 0.5}), case
    )
    assert adaptive == "r1a"
    assert root_first == "r2"


def test_populations_cover_grid_and_every_policy_passes_protocol() -> None:
    for family in FAMILIES:
        population = mechanism.generation_population(SPEC, family)
        assert [p["recipe"]["min_metric"] for p in population] == list(THRESHOLD_GRID)
        assert len({p["source_sha256"] for p in population}) == len(THRESHOLD_GRID)
        for policy in population:
            assert policy["source_sha256"] == hashlib.sha256(policy["source"].encode()).hexdigest()
            for name in CASES:
                _select(policy["source"], CASES[name])  # asserts protocol-valid
    impl = {mechanism.generation_population(SPEC, f)[0]["implementation_sha256"] for f in FAMILIES}
    assert len(impl) == 2


def test_families_differ_only_where_designed_and_tie_exhaustion_stay_valid() -> None:
    adaptive = _selections(ADAPTIVE_RESTART)
    root_first = _selections(ROOT_FIRST)
    for threshold in THRESHOLD_GRID:
        differing = {
            name for name in CASES if adaptive[threshold][name] != root_first[threshold][name]
        }
        designed = {name for name, case in CASES.items() if case["designed_to_differ"]}
        assert differing <= designed
    assert any(adaptive[t]["root_vs_child_trap"] != root_first[t]["root_vs_child_trap"] for t in THRESHOLD_GRID)
    for selections in (adaptive, root_first):
        for per_case in selections.values():
            assert per_case["tied_unopened_roots"] == "r1"
            assert per_case["tied_metrics"] == "ra1"
            assert per_case["exhausted_tree"] is None
            assert per_case["infra_error_trap"] == "c2"
            assert per_case["missing_metric"] == "c1"


def test_selection_fingerprints_count_identical_sets_as_one_family() -> None:
    groups: dict[str, list[str]] = {}
    for family in FAMILIES:
        for threshold, per_case in _selections(family).items():
            groups.setdefault(_fingerprint(per_case), []).append(f"{family}@{threshold}")
    # Whole-set identity collapses policies; two production rules must still survive as >=2 families.
    assert len(groups) >= 2
    adaptive_prints = {_fingerprint(v) for v in _selections(ADAPTIVE_RESTART).values()}
    root_prints = {_fingerprint(v) for v in _selections(ROOT_FIRST).values()}
    assert adaptive_prints != root_prints


def test_gradient_case_separates_thresholds_within_adaptive_restart() -> None:
    selections = _selections(ADAPTIVE_RESTART)
    assert selections[0.5]["gradient_between_thresholds"] == "c1"
    assert selections[0.75]["gradient_between_thresholds"] == "r2"


def test_legacy_min_metric_recipe_keeps_adaptive_restart_bytes() -> None:
    legacy = mechanism.generation_job(SPEC, {"min_metric": 0.5})
    assert legacy == mechanism.generation_job(SPEC, {"family": ADAPTIVE_RESTART, "min_metric": 0.5})
    # Downstream packaging hashes and ships these bytes; the frozen legacy digest
    # is the observable contract for byte-stable adaptive-restart.v1 output.
    assert hashlib.sha256(legacy.encode()).hexdigest() == (
        "d2f2b48d3e4b6d743bd502d0cb7fb27ff66ff4fbd4be1fbec6d198c10827d41c"
    )
    assert canonical_recipe({"min_metric": 1}) == {"family": ADAPTIVE_RESTART, "min_metric": 1.0}


def test_r13_bookkeeping_keys_do_not_change_generation() -> None:
    # recursive_method.run_method hands the raw R13 recipe to generation_job; the
    # schema (recursive._R13Inputs) sanctions id/name/label, so they must be inert.
    bare = mechanism.generation_job(SPEC, {"min_metric": 0.5})
    named = mechanism.generation_job(SPEC, {"id": "x", "name": "first", "label": "renamed", "min_metric": 0.5})
    assert named == bare
    assert canonical_recipe({"name": "first", "min_metric": 0.5}) == {"family": ADAPTIVE_RESTART, "min_metric": 0.5}


@pytest.mark.parametrize(
    "recipe",
    [
        {"min_metric": 0.5, "extra": 1},
        {"family": "beam.v1", "min_metric": 0.5},
        {"family": ["x"], "min_metric": 0.5},
        {"family": ROOT_FIRST},
        {"min_metric": True},
        {"min_metric": "0.5"},
        {"min_metric": math.nan},
        {"min_metric": math.inf},
        {"min_metric": 10**400},
    ],
)
def test_invalid_recipes_are_rejected_by_generation(recipe: dict) -> None:
    with pytest.raises(ValueError):
        mechanism.generation_job(SPEC, recipe)


def test_unknown_family_population_rejected() -> None:
    with pytest.raises(ValueError):
        mechanism.generation_population(SPEC, "nope.v1")
