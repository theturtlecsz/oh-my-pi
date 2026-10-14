"""Search definitions resolve defaults and reject invalid forests at load."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    Criterion,
    PreparedTrial,
    SearchConfig,
    definition_commitment,
    load_definition,
    score_campaign,
    score_metrics,
    search_steps,
)
from omp_work.jobs import campaign_setup
from omp_work.jobs.campaign_setup import prepare_campaign
from omp_work.research.search import next_step, run_policy
from omp_work.v1.canonical import sha256
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


def test_search_steps_orders_tree_with_fresh_unset_nodes(tmp_path: Path) -> None:
    loaded = _load(
        tmp_path,
        search={},
        trials=[
            {"harness": "h.sh", "cost": 2},
            {"harness": "h.sh", "parent": "t000", "cost": 3},
            {"harness": "h.sh", "id": "x"},
            {"harness": "h.sh", "parent": "t000"},
        ],
    )
    steps = search_steps(loaded)
    digest = hashlib.sha256(b"cached harness\n").hexdigest()
    assert list(steps) == ["t000", "t001", "x", "t003"]  # same digest, distinct nodes
    assert steps["t000"] == {
        "parent_id": None, "children": ["t001", "t003"], "cost": 2,
        "candidate_digest": digest, "metric": None, "infra_error": None,
    }
    assert steps["t001"]["parent_id"] == "t000" and steps["t001"]["children"] == []
    steps["t000"]["metric"] = 5
    steps["t000"]["children"].append("zzz")
    again = search_steps(loaded)
    assert again["t000"]["metric"] is None and again["t000"]["children"] == ["t001", "t003"]
    assert loaded.trials[0].harness_bytes == b"cached harness\n"


def test_search_steps_resolves_default_ids_for_old_constructors() -> None:
    digest = hashlib.sha256(b"h").hexdigest()
    trials = (
        PreparedTrial(Path("a"), b"h", digest),
        PreparedTrial(Path("b"), b"h", digest, parent="t000"),
    )
    definition = CampaignDefinition(
        "o", ResearchDomain.ENGINEERING, 60, (Criterion("score", "gte", 1),), trials
    )
    steps = search_steps(definition)
    assert list(steps) == ["t000", "t001"]
    assert steps["t000"]["children"] == ["t001"]


def test_commitment_requires_explicit_search(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="search"):
        definition_commitment(_load(tmp_path))


def _mutations(loaded: CampaignDefinition) -> dict[str, CampaignDefinition]:
    search = loaded.search
    assert search is not None
    first, second = loaded.trials

    def swap(**kw: object) -> CampaignDefinition:
        return replace(loaded, trials=(first, replace(second, **kw)))

    def crit(index: int, value: Criterion) -> tuple[Criterion, ...]:
        criteria = list(loaded.criteria)
        criteria[index] = value
        return tuple(criteria)

    return {
        "objective": replace(loaded, objective="other"),
        "domain": replace(loaded, domain=ResearchDomain.LITERATURE),
        "timeout": replace(loaded, timeout_seconds=61),
        "criterion-metric": replace(loaded, criteria=crit(0, Criterion("loss", "gte", 1))),
        "criterion-op": replace(loaded, criteria=crit(0, Criterion("score", "gt", 1))),
        "criterion-threshold": replace(loaded, criteria=crit(0, Criterion("score", "gte", 2))),
        "criteria-order": replace(loaded, criteria=loaded.criteria[::-1]),
        "policy": replace(loaded, search=replace(search, policy="fixed")),
        "budget": replace(loaded, search=replace(search, budget=search.budget + 1)),
        "width": replace(loaded, search=replace(search, width=search.width + 1)),
        "seed": replace(loaded, search=replace(search, seed=search.seed + 1)),
        "metric": replace(loaded, search=replace(search, metric="loss")),
        "trial-id": swap(id="renamed"),
        "trial-parent": swap(parent=None),
        "trial-cost": swap(cost=9),
        "unopened-digest": swap(candidate_digest="0" * 64),
        "trial-order": replace(loaded, trials=(second, first)),
    }


def test_commitment_binds_every_field_and_ignores_presentation(tmp_path: Path) -> None:
    search = {"policy": "greedy", "budget": 4, "width": 2, "seed": 3, "metric": "score"}
    trials = [{"harness": "h.sh", "id": "a"}, {"harness": "h.sh", "id": "b", "parent": "a"}]
    loaded = _load(tmp_path, search=search, trials=trials)
    base = definition_commitment(loaded)
    assert base == definition_commitment(loaded)
    for name, mutated in _mutations(loaded).items():
        assert definition_commitment(mutated) != base, name

    # Key order, formatting, and relocated paths with identical bytes keep the hash.
    moved = tmp_path / "moved"
    moved.mkdir()
    document = {
        "trials": trials,
        "search": dict(reversed(list(search.items()))),
        "criteria": [{"threshold": 1, "op": "gte", "metric": "score"},
                     {"threshold": 0, "op": "lt", "metric": "loss"}],
        "domain": "engineering",
        "timeout_seconds": 60,
        "objective": "improve score",
    }
    (moved / "h.sh").write_bytes(b"cached harness\n")
    (moved / "d.json").write_text(json.dumps(document, indent=7))
    assert definition_commitment(load_definition(moved / "d.json")) == base

    steps = search_steps(loaded)
    steps["a"]["metric"] = 1.5
    steps["a"]["infra_error"] = "boom"
    assert definition_commitment(loaded) == base


@pytest.fixture
def offline_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        campaign_setup, "_register",
        lambda client, workspace, kind, name, artifact, **kw: sha256([str(kind), name, artifact]),
    )
    monkeypatch.setattr(campaign_setup, "code_fingerprint", lambda: "f" * 64)


def _prepare(definition: CampaignDefinition, tmp_path: Path):
    return prepare_campaign(None, None, definition, harness_dir=tmp_path / "cache")  # type: ignore[arg-type]


_SHARED_HARNESS = b"cached harness\n"
_UNOPENED_HARNESS = b"unopened harness\n"


def _explicit_campaign() -> dict[str, Any]:
    """Explicit ids and budget so a cost or id edit does not move another field.

    ``root-a`` and ``root-b`` are independent roots. ``u.sh`` is a separate
    harness so one unopened digest can change without touching the shared bytes.
    Search keeps selecting ``score``.
    """
    return {
        "objective": "improve score",
        "domain": "engineering",
        "timeout_seconds": 60,
        "criteria": [
            {"metric": "score", "op": "gte", "threshold": 1},
            {"metric": "loss", "op": "lt", "threshold": 0},
        ],
        "search": {"policy": "greedy", "budget": 4, "width": 2, "seed": 3, "metric": "score"},
        "trials": [
            {"harness": "h.sh", "id": "root-a", "cost": 1},
            {"harness": "h.sh", "id": "root-b", "cost": 1},
            {"harness": "u.sh", "id": "leaf", "parent": "root-a", "cost": 1},
        ],
    }


def _load_explicit(
    directory: Path, document: dict[str, Any], *, unopened: bytes = _UNOPENED_HARNESS
) -> CampaignDefinition:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "h.sh").write_bytes(_SHARED_HARNESS)
    (directory / "u.sh").write_bytes(unopened)
    path = directory / "campaign.json"
    path.write_text(json.dumps(document))
    return load_definition(path)


def _assert_loader_invariants(definition: CampaignDefinition) -> None:
    search = definition.search
    assert search is not None
    assert search.metric in {criterion.metric for criterion in definition.criteria}
    seen: set[str] = set()
    for trial in definition.trials:
        assert trial.id is not None and trial.id not in seen
        if trial.parent is not None:
            assert trial.parent in seen
        assert trial.candidate_digest == hashlib.sha256(trial.harness_bytes).hexdigest()
        seen.add(trial.id)


def _valid_prepared_mutations() -> list[tuple[str, dict[str, Any], bytes]]:
    def doc() -> dict[str, Any]:
        return _explicit_campaign()

    objective = doc()
    objective["objective"] = "other objective"
    domain = doc()
    domain["domain"] = "literature"
    timeout = doc()
    timeout["timeout_seconds"] = 61
    # Rename the criterion search does not select, so ``score`` stays declared.
    criterion_metric = doc()
    criterion_metric["criteria"][1]["metric"] = "error"
    criterion_op = doc()
    criterion_op["criteria"][0]["op"] = "gt"
    criterion_threshold = doc()
    criterion_threshold["criteria"][0]["threshold"] = 2
    criteria_order = doc()
    criteria_order["criteria"].reverse()
    policy = doc()
    policy["search"]["policy"] = "fixed"
    budget = doc()
    budget["search"]["budget"] = 5
    width = doc()
    width["search"]["width"] = 3
    seed = doc()
    seed["search"]["seed"] = 4
    metric = doc()
    metric["search"]["metric"] = "loss"
    pool_size = doc()
    pool_size["trials"].append({"harness": "h.sh", "id": "root-c", "cost": 1})
    pool_order = doc()
    root_a, root_b, leaf = pool_order["trials"]
    pool_order["trials"] = [root_b, root_a, leaf]
    trial_id = doc()
    trial_id["trials"][1]["id"] = "root-b2"
    trial_parent = doc()
    trial_parent["trials"][2]["parent"] = "root-b"
    trial_cost = doc()
    trial_cost["trials"][2]["cost"] = 9
    same = doc()
    rewritten = b"rewritten unopened harness\n"
    return [
        ("objective", objective, _UNOPENED_HARNESS),
        ("domain", domain, _UNOPENED_HARNESS),
        ("timeout", timeout, _UNOPENED_HARNESS),
        ("criterion-metric", criterion_metric, _UNOPENED_HARNESS),
        ("criterion-op", criterion_op, _UNOPENED_HARNESS),
        ("criterion-threshold", criterion_threshold, _UNOPENED_HARNESS),
        ("criteria-order", criteria_order, _UNOPENED_HARNESS),
        ("policy", policy, _UNOPENED_HARNESS),
        ("budget", budget, _UNOPENED_HARNESS),
        ("width", width, _UNOPENED_HARNESS),
        ("seed", seed, _UNOPENED_HARNESS),
        ("metric", metric, _UNOPENED_HARNESS),
        ("pool-size", pool_size, _UNOPENED_HARNESS),
        ("pool-order", pool_order, _UNOPENED_HARNESS),
        ("trial-id", trial_id, _UNOPENED_HARNESS),
        ("trial-parent", trial_parent, _UNOPENED_HARNESS),
        ("trial-cost", trial_cost, _UNOPENED_HARNESS),
        ("unopened-digest", same, rewritten),
    ]


def test_prepared_spec_binds_commitment_and_every_mutation(offline_setup: None, tmp_path: Path) -> None:
    loaded = _load_explicit(tmp_path / "base", _explicit_campaign())
    _assert_loader_invariants(loaded)
    assert loaded.search == SearchConfig("greedy", 4, 2, 3, "score")
    assert [(trial.id, trial.parent, trial.cost) for trial in loaded.trials] == [
        ("root-a", None, 1), ("root-b", None, 1), ("leaf", "root-a", 1),
    ]
    base = _prepare(loaded, tmp_path)
    assert base.spec.evaluation_protocol_sha256 == definition_commitment(loaded)
    assert base.spec_sha256 == _prepare(loaded, tmp_path).spec_sha256
    for name, document, unopened in _valid_prepared_mutations():
        mutated = _load_explicit(tmp_path / name, document, unopened=unopened)
        _assert_loader_invariants(mutated)
        if name == "criterion-metric":
            assert mutated.search is not None and mutated.search.metric == "score"
            assert [criterion.metric for criterion in mutated.criteria] == ["score", "error"]
        if name == "pool-order":
            assert [(trial.id, trial.parent) for trial in mutated.trials] == [
                ("root-b", None), ("root-a", None), ("leaf", "root-a"),
            ]
        if name == "unopened-digest":
            assert mutated.trials[2].harness_bytes != loaded.trials[2].harness_bytes
            assert mutated.trials[2].candidate_digest != loaded.trials[2].candidate_digest
            assert mutated.trials[0].harness_bytes == loaded.trials[0].harness_bytes
            assert mutated.trials[0].candidate_digest == loaded.trials[0].candidate_digest
        changed = _prepare(mutated, tmp_path)
        assert changed.spec.evaluation_protocol_sha256 == definition_commitment(mutated), name
        assert changed.spec.evaluation_protocol_sha256 != base.spec.evaluation_protocol_sha256, name
        assert changed.spec_sha256 != base.spec_sha256, name

    # Key order, formatting, and a relocated path with the same cached bytes.
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "h.sh").write_bytes(_SHARED_HARNESS)
    (moved / "u.sh").write_bytes(_UNOPENED_HARNESS)
    (moved / "d.json").write_text(json.dumps({
        "trials": [
            {"cost": 1, "id": "root-a", "harness": "h.sh"},
            {"harness": "h.sh", "cost": 1, "id": "root-b"},
            {"parent": "root-a", "harness": "u.sh", "id": "leaf", "cost": 1},
        ],
        "search": {"metric": "score", "seed": 3, "width": 2, "budget": 4, "policy": "greedy"},
        "criteria": [
            {"threshold": 1, "op": "gte", "metric": "score"},
            {"threshold": 0, "metric": "loss", "op": "lt"},
        ],
        "domain": "engineering",
        "timeout_seconds": 60,
        "objective": "improve score",
    }, indent=7))
    relocated_definition = load_definition(moved / "d.json")
    _assert_loader_invariants(relocated_definition)
    relocated = _prepare(relocated_definition, moved)
    assert relocated.spec.evaluation_protocol_sha256 == base.spec.evaluation_protocol_sha256
    assert relocated.spec_sha256 == base.spec_sha256


def test_absent_search_keeps_criteria_only_evaluation_hash(offline_setup: None, tmp_path: Path) -> None:
    loaded = _load(tmp_path)
    setup = _prepare(loaded, tmp_path)
    expected = sha256([{"metric": "score", "op": "gte", "threshold": 1},
                       {"metric": "loss", "op": "lt", "threshold": 0}])
    assert setup.spec.evaluation_protocol_sha256 == expected
    assert score_metrics({"score": 1, "loss": -1}, loaded.criteria, "completed")[0] == "supported"


def test_loaded_default_budget_opens_fixed_trials_and_smaller_budget_stops(tmp_path: Path) -> None:
    two = _load(tmp_path, search={}, trials=[{"harness": "h.sh", "cost": 1}, {"harness": "h.sh", "cost": 2}])
    assert two.search.budget == 3
    assert run_policy(two.search.policy, search_steps(two), budget=two.search.budget) == ["t000", "t001"]
    assert run_policy("fixed", search_steps(two), budget=2.5) == ["t000"]
    assert run_policy("fixed", search_steps(two), budget=0.5) == []
    frac = _load(tmp_path, search={}, trials=[{"harness": "h.sh", "cost": c} for c in (0.1, 0.2, 0.1)])
    assert run_policy("fixed", search_steps(frac), budget=frac.search.budget) == ["t000", "t001", "t002"]


def test_fixed_follows_definition_order_not_lexical(tmp_path: Path) -> None:
    loaded = _load(tmp_path, search={}, trials=[
        {"harness": "h.sh", "id": "z"}, {"harness": "h.sh", "id": "a"}, {"harness": "h.sh", "id": "m", "parent": "z"},
    ])
    assert run_policy("fixed", search_steps(loaded), budget=loaded.search.budget) == ["z", "a", "m"]


def test_greedy_child_follows_swapped_parent_metrics(tmp_path: Path) -> None:
    loaded = _load(tmp_path, search={"policy": "greedy"}, trials=[
        {"harness": "h.sh", "id": "p1"}, {"harness": "h.sh", "id": "p2"},
        {"harness": "h.sh", "id": "c1", "parent": "p1"}, {"harness": "h.sh", "id": "c2", "parent": "p2"},
    ])
    picks = []
    for first, second in ((1, 2), (2, 1)):
        steps = search_steps(loaded)
        steps["p1"]["metric"], steps["p2"]["metric"] = first, second
        picks.append(next_step(loaded.search.policy, steps, {"p1", "p2"})[0])
    assert picks == ["c2", "c1"]
