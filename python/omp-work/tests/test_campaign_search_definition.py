"""Search definitions resolve defaults and reject invalid forests at load."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    Criterion,
    PreparedTrial,
    SearchConfig,
    load_definition,
    score_campaign,
    score_metrics,
)
from omp_work.v1.models import ResearchDomain


def _load(tmp_path: Path, **overrides: object) -> CampaignDefinition:
    document = {
        "objective": "improve score",
        "domain": "engineering",
        "trials": [{"harness": "h.sh"}],
        "criteria": [
            {"metric": "score", "op": "gte", "threshold": 1},
            {"metric": "loss", "op": "lt", "threshold": 0},
        ],
        **overrides,
    }
    (tmp_path / "h.sh").write_bytes(b"cached harness\n")
    path = tmp_path / "campaign.json"
    # Exponent overflow exercises numeric validation, not JSON constant rejection.
    path.write_text(json.dumps(document).replace("Infinity", "1e309"))
    return load_definition(path)


def test_search_defaults_resolve_forest_and_finite_fractional_budget(tmp_path: Path) -> None:
    costs = [0.1, 0.2, 0.3]
    loaded = _load(
        tmp_path,
        search={},
        trials=[
            {"harness": "h.sh", "cost": costs[0]},
            {"harness": "h.sh", "parent": "t000", "cost": costs[1]},
            {"harness": "h.sh", "parent": None, "cost": costs[2]},
        ],
    )
    assert loaded.search == SearchConfig("fixed", math.fsum(costs), 1, 0, "score")
    assert loaded.search.budget == 0.6  # Ordinary sum would be 0.6000000000000001.
    assert [(trial.id, trial.parent, trial.cost) for trial in loaded.trials] == [
        ("t000", None, 0.1), ("t001", "t000", 0.2), ("t002", None, 0.3)
    ]


def test_explicit_search_and_forest_preserve_cached_bytes_and_scoring(tmp_path: Path) -> None:
    root = "A" * 64
    loaded = _load(
        tmp_path,
        search={"policy": "greedy", "budget": 0.5, "width": 2, "seed": 7, "metric": "loss"},
        trials=[
            {"harness": "h.sh", "id": root},
            {"harness": "h.sh", "id": "a.Z_9-", "parent": root},
            {"harness": "h.sh", "parent": "a.Z_9-"},
            {"harness": "h.sh", "id": "other"},
        ],
    )
    assert loaded.search == SearchConfig("greedy", 0.5, 2, 7, "loss")
    assert [(trial.id, trial.parent) for trial in loaded.trials] == [
        (root, None), ("a.Z_9-", root), ("t002", "a.Z_9-"), ("other", None)
    ]
    (tmp_path / "h.sh").write_bytes(b"changed")
    for trial in loaded.trials:
        assert trial.harness_bytes == b"cached harness\n"
        assert trial.candidate_digest == hashlib.sha256(trial.harness_bytes).hexdigest()
    verdict = score_metrics({"score": 1, "loss": -1}, loaded.criteria, "completed")
    assert score_campaign([verdict] * 4) == ("supported", "passed 4 trials")


def test_absent_search_and_old_positional_constructors_remain_usable(tmp_path: Path) -> None:
    loaded = _load(tmp_path)
    assert loaded.search is None
    assert (loaded.trials[0].id, loaded.trials[0].cost) == ("t000", 1)
    trial = PreparedTrial(Path("h.sh"), b"h", hashlib.sha256(b"h").hexdigest())
    definition = CampaignDefinition(
        "objective", ResearchDomain.ENGINEERING, 60, (Criterion("score", "gte", 1),), (trial,)
    )
    assert definition.search is None
    assert (trial.id, trial.parent, trial.cost) == (None, None, 1)
    assert score_metrics({"score": 1}, definition.criteria, "completed")[0] == "supported"


@pytest.mark.parametrize("field", ["id", "parent"])
@pytest.mark.parametrize("value", ["", "a" * 65, "é", "a b", "a/b", "a\n", 1, True])
def test_invalid_trial_identifiers_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValueError, match=rf"trials\[0\].{field} must match"):
        _load(tmp_path, trials=[{"harness": "h.sh", field: value}])


def test_explicit_null_id_is_not_a_generated_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="id must match"):
        _load(tmp_path, trials=[{"harness": "h.sh", "id": None}])


@pytest.mark.parametrize(
    "trials",
    [
        [{"id": "same"}, {"id": "same"}],
        [{}, {"id": "t000"}],
        [{"id": "t001"}, {}],
    ],
    ids=["explicit-duplicate", "explicit-collides-with-generated", "generated-collides-with-explicit"],
)
def test_duplicate_trial_ids_are_rejected(tmp_path: Path, trials: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError, match="duplicate id"):
        _load(tmp_path, trials=[{"harness": "h.sh", **trial} for trial in trials])


@pytest.mark.parametrize("parent", ["t000", "t001", "unknown"], ids=["self", "forward", "unknown"])
def test_parent_must_already_exist(tmp_path: Path, parent: str) -> None:
    with pytest.raises(ValueError, match="parent must name an earlier trial id"):
        _load(tmp_path, trials=[{"harness": "h.sh", "parent": parent}, {"harness": "h.sh"}])


@pytest.mark.parametrize("value", [0, -1, True, "1", None, float("inf"), 10**400])
@pytest.mark.parametrize("field", ["cost", "budget"])
def test_cost_and_budget_require_finite_positive_numbers(tmp_path: Path, field: str, value: object) -> None:
    overrides = (
        {"trials": [{"harness": "h.sh", "cost": value}]}
        if field == "cost"
        else {"search": {"budget": value}}
    )
    with pytest.raises(ValueError, match=rf"{field} must be a finite"):
        _load(tmp_path, **overrides)


@pytest.mark.parametrize("search", [None, [], True])
def test_explicit_search_requires_an_object(tmp_path: Path, search: object) -> None:
    with pytest.raises(TypeError, match="search must be an object"):
        _load(tmp_path, search=search)


@pytest.mark.parametrize(
    ("search", "match"),
    [
        ({"extra": 1}, "search has unknown key extra"),
        ({"policy": "random"}, "policy must be fixed or greedy"),
        ({"policy": None}, "policy must be fixed or greedy"),
        ({"width": 0}, "width must be a positive integer"),
        ({"width": -1}, "width must be a positive integer"),
        ({"width": True}, "width must be a positive integer"),
        ({"width": 1.5}, "width must be a positive integer"),
        ({"seed": -1}, "seed must be a nonnegative integer"),
        ({"seed": True}, "seed must be a nonnegative integer"),
        ({"seed": 1.5}, "seed must be a nonnegative integer"),
        ({"metric": "undeclared"}, "metric must name a declared criterion"),
        ({"metric": ""}, "metric must be a nonempty string"),
        ({"metric": None}, "metric must be a nonempty string"),
    ],
)
def test_invalid_search_options_fail_at_load(tmp_path: Path, search: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _load(tmp_path, search=search)


def test_omitted_budget_overflow_raises_value_error(tmp_path: Path) -> None:
    trials = [{"harness": "h.sh", "cost": 1e308}] * 2
    with pytest.raises(ValueError, match="budget total must be finite"):
        _load(tmp_path, trials=trials, search={})
    # Without search, no aggregate budget is needed for legacy scoring.
    assert _load(tmp_path, trials=trials).search is None


def test_unit_cost_budget_resolves_to_trial_count(tmp_path: Path) -> None:
    loaded = _load(tmp_path, search={}, trials=[{"harness": "h.sh"}] * 2)
    assert loaded.search == SearchConfig("fixed", 2.0, 1, 0, "score")
