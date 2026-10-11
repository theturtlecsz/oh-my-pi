"""Immutable recursion bindings, history isolation, and R13 proposal identity."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from omp_work.research.accounting import ResourceLimits
from omp_work.research.recursive import (
    CallerGrant,
    RecursionState,
    RecursiveProposal,
    RecursiveStudy,
    RecursiveUsage,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
LIMITS = ResourceLimits(100, 1000, Decimal("10"))


def study_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "study_id": "root", "parent_id": None, "depth": 0,
        "method_version": "r13.v1", "method_sha256": "a" * 64,
        "lower_researcher_sha256": "b" * 64, "objective": "Improve search",
        "evaluator_component_sha256": "c" * 64,
        "evaluator_artifact_sha256": "d" * 64, "protocol_sha256": "e" * 64,
        "dev": [{"task_id": "dev-1", "content_sha256": "1" * 64}],
        "confirmation": [{"task_id": "confirm-1", "content_sha256": "2" * 64}],
        "upstream_assets": [{"identity": "strategy.v1", "content_sha256": "f" * 64}],
        "window_start": START, "window_end": END,
        "max_depth": 2, "max_descendants": 2, "limits": LIMITS,
    }
    payload.update(overrides)
    return payload


def grant(**overrides: object) -> CallerGrant:
    payload: dict[str, object] = {
        "caller_id": "owner", "policy_id": "spend-policy", "decision_id": "decision-1",
        "signature_sha256": "3" * 64, "window_start": START, "window_end": END,
        "max_depth": 2, "max_descendants": 2, "limits": LIMITS,
    }
    payload.update(overrides)
    return CallerGrant.model_validate(payload)


def child(**overrides: object) -> RecursiveStudy:
    return RecursiveStudy.model_validate(study_payload(**{
        "study_id": "child", "parent_id": "root", "depth": 1,
        "dev": [{"task_id": "dev-2", "content_sha256": "4" * 64}],
        "confirmation": [{"task_id": "confirm-2", "content_sha256": "5" * 64}],
        **overrides,
    }))


def proposal(metric: float = 0.5, **overrides: object) -> RecursiveProposal:
    return RecursiveProposal.model_validate({
        "proposal_id": "proposal-1", "study_id": "root",
        "recipe": {"name": "first", "min_metric": metric},
        "spec": {"name": "first", "state_fields": ["score"], "nested": {"name": "semantic"}},
        **overrides,
    })


def test_mutation_cannot_change_sealed_history_or_nested_inputs() -> None:
    payload = study_payload()
    root = RecursiveStudy.model_validate(payload)
    spec = {"state_fields": ["score"]}
    sealed = proposal(spec=spec)
    state = RecursionState(studies=(root,), grant=grant(), proposal_history=(sealed,))
    before = state.model_dump_json()
    payload["dev"][0]["task_id"] = "changed"
    spec["state_fields"].append("changed")
    assert state.model_dump_json() == before
    for record, field, value in (
        (root, "depth", 1), (root.dev[0], "task_id", "changed"),
        (root.upstream_assets[0], "identity", "changed"),
        (sealed, "spec", b'{}'), (state, "terminal_reason", "changed"),
        (state.grant, "decision_id", "changed"),
    ):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(record, field, value)
    with pytest.raises(FrozenInstanceError):
        root.limits.max_model_tokens = 999
    restored = RecursionState.model_validate_json(before)
    assert restored == state
    with pytest.raises(ValidationError, match="frozen"):
        restored.studies[0].dev[0].task_id = "changed"


@pytest.mark.parametrize("field", [
    "method_sha256", "lower_researcher_sha256", "evaluator_component_sha256",
    "evaluator_artifact_sha256", "protocol_sha256",
])
def test_malformed_binding_digest_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        RecursiveStudy.model_validate(study_payload(**{field: "A" * 64}))


@pytest.mark.parametrize("overrides", [
    {"window_start": START.replace(tzinfo=None)},
    {"window_end": END.astimezone(timezone(timedelta(hours=1)))},
    {"window_end": START}, {"window_start": END},
    {"depth": -1}, {"parent_id": "parent"}, {"depth": 1},
    {"max_depth": -1}, {"max_descendants": -1}, {"max_depth": True},
    {"objective": " "}, {"dev": []}, {"confirmation": []}, {"upstream_assets": []},
    {"dev": [{"task_id": "", "content_sha256": "1" * 64}]},
    {"confirmation": [{"task_id": "confirm", "content_sha256": "bad"}]},
    {"upstream_assets": [{"identity": "asset", "content_sha256": "bad"}]},
    {"confirmation": [{"task_id": "dev-1", "content_sha256": "9" * 64}]},
    {"confirmation": [{"task_id": "other", "content_sha256": "1" * 64}]},
])
def test_invalid_study_bounds_and_empty_or_reused_evidence_refused(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        RecursiveStudy.model_validate(study_payload(**overrides))


@pytest.mark.parametrize("limits", [
    ResourceLimits(0, 1000, Decimal(10)), ResourceLimits(float("inf"), 1000, Decimal(10)),
    ResourceLimits(100, 0, Decimal(10)), ResourceLimits(100, True, Decimal(10)),
    ResourceLimits(100, 1000, Decimal(-1)), ResourceLimits(100, 1000, Decimal("NaN")),
])
def test_invalid_resource_caps_refused_for_study_and_grant(limits: ResourceLimits) -> None:
    with pytest.raises(ValidationError):
        RecursiveStudy.model_validate(study_payload(limits=limits))
    with pytest.raises(ValidationError):
        grant(limits=limits)


def test_grant_pins_signed_payload_and_refuses_unbound_authority() -> None:
    signed = grant()
    assert json.loads(signed.signing_bytes())["limits"]["max_usd"] == "10"
    assert "signature_sha256" not in json.loads(signed.signing_bytes())
    assert grant(decision_id="decision-2").signing_bytes() != signed.signing_bytes()
    for field in ("caller_id", "policy_id", "decision_id", "signature_sha256"):
        with pytest.raises(ValidationError):
            grant(**{field: ""})
    with pytest.raises(ValidationError, match="exceeds caller grant"):
        RecursionState(studies=(RecursiveStudy.model_validate(study_payload()),),
                       grant=grant(limits=ResourceLimits(99, 1000, Decimal(10))))


def test_renaming_equivalence_and_distinct_thresholds_survive_json_roundtrip() -> None:
    first = proposal()
    renamed = proposal(
        proposal_id="renamed", recipe={"id": "new", "name": "new", "label": "new", "min_metric": 0.5},
        spec={"id": "new", "label": "new", "state_fields": ["score"], "nested": {"name": "semantic"}},
    )
    distinct = proposal(0.6, proposal_id="distinct")
    nested_change = proposal(spec={"state_fields": ["score"], "nested": {"name": "different"}})
    state = RecursionState(studies=(RecursiveStudy.model_validate(study_payload()),), grant=grant(),
                           proposal_history=(first, renamed, distinct))
    restored = RecursionState.model_validate_json(state.model_dump_json())
    a, b, c = restored.proposal_history
    assert a.equivalence_sha256 == b.equivalence_sha256
    assert a.equivalence_sha256 != c.equivalence_sha256
    assert a.equivalence_sha256 != nested_change.equivalence_sha256
    assert a.recipe == b'{"min_metric":0.5,"name":"first"}'
    assert restored.studies[0].method_sha256 == state.studies[0].method_sha256


@pytest.mark.parametrize("recipe", [
    {}, {"width": 2}, {"min_metric": 0.5, "width": 2}, {"min_metric": True},
    {"min_metric": "0.5"}, {"min_metric": float("nan")}, b'{"min_metric":Infinity}',
])
def test_non_r13_or_nonfinite_recipe_refused(recipe: object) -> None:
    with pytest.raises(ValidationError):
        proposal(recipe=recipe)


@pytest.mark.parametrize("descendant", [
    {"parent_id": "missing"}, {"depth": 2},
    {"dev": [{"task_id": "dev-1", "content_sha256": "7" * 64}]},
    {"dev": [{"task_id": "fresh", "content_sha256": "2" * 64}]},
    {"window_end": END + timedelta(seconds=1)},
])
def test_history_refuses_broken_tree_and_cross_history_cohort_reuse(descendant: dict) -> None:
    with pytest.raises(ValidationError):
        RecursionState(studies=(RecursiveStudy.model_validate(study_payload()), child(**descendant)),
                       grant=grant())


@pytest.mark.parametrize("usage", [
    {"compute_seconds": 100, "tokens": 0, "usd": 0},
    {"compute_seconds": 0, "tokens": 1000, "usd": 0},
    {"compute_seconds": 0, "tokens": 0, "usd": 10},
])
def test_admission_checks_tree_cumulative_usage_and_exhausts_at_equality(usage: dict) -> None:
    root = RecursiveStudy.model_validate(study_payload())
    events = tuple(RecursiveUsage(study_id=study_id, **{
        axis: value / 2 if axis != "tokens" else value // 2 for axis, value in usage.items()
    }) for study_id in ("root", "child"))
    state = RecursionState(studies=(root, child()), grant=grant(), usage=events)
    assert state.refuse_reason(START) == "quota_exhausted"
    restored = RecursionState.model_validate_json(state.model_dump_json())
    assert restored.refuse_reason(START) == "quota_exhausted"


def test_admission_window_is_half_open_and_terminal_reason_survives_resume() -> None:
    root = RecursiveStudy.model_validate(study_payload())
    state = RecursionState(studies=(root,), grant=grant())
    assert state.refuse_reason(START - timedelta(microseconds=1)) == "outside_window"
    assert state.refuse_reason(START) is None
    assert state.refuse_reason(END - timedelta(microseconds=1)) is None
    assert state.refuse_reason(END) == "outside_window"
    stopped = RecursionState(studies=(root,), grant=grant(), terminal_reason="cancelled")
    assert RecursionState.model_validate_json(stopped.model_dump_json()).refuse_reason(START) == "cancelled"
    with pytest.raises(ValidationError):
        RecursionState(studies=(root,), grant=grant(), usage=(RecursiveUsage(
            study_id="missing", compute_seconds=0, tokens=0, usd=0),))


@pytest.mark.parametrize("spec", [b'[]', b'{}', b'{broken', {"value": object()}])
def test_invalid_spec_cannot_be_sealed(spec: object) -> None:
    with pytest.raises(ValidationError):
        proposal(spec=spec)


@pytest.mark.parametrize("usage", [
    {"compute_seconds": -1, "tokens": 0, "usd": 0},
    {"compute_seconds": float("inf"), "tokens": 0, "usd": 0},
    {"compute_seconds": 0, "tokens": -1, "usd": 0},
    {"compute_seconds": 0, "tokens": 0, "usd": "NaN"},
])
def test_invalid_usage_cannot_reduce_or_poison_tree_totals(usage: dict) -> None:
    with pytest.raises(ValidationError):
        RecursiveUsage(study_id="root", **usage)


def test_root_and_descendant_caps_refuse_more_children() -> None:
    root = RecursiveStudy.model_validate(study_payload(max_depth=0))
    assert RecursionState(studies=(root,), grant=grant()).refuse_reason(START) == "max_depth"
    with pytest.raises(ValidationError, match="root bounds"):
        RecursionState(studies=(root, child()), grant=grant())
    root = RecursiveStudy.model_validate(study_payload(max_descendants=1))
    state = RecursionState(studies=(root, child()), grant=grant())
    assert state.refuse_reason(START) == "max_descendants"
    with pytest.raises(ValidationError, match="one root"):
        RecursionState(studies=(root, RecursiveStudy.model_validate(study_payload(study_id="root-2"))),
                       grant=grant())
    with pytest.raises(ValidationError, match="duplicate study ID"):
        RecursionState(studies=(root, root), grant=grant())
