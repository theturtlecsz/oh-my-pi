"""Immutable recursion bindings, history isolation, and R13 proposal identity."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omp_work.research.accounting import ResourceLimits
from omp_work.research.recursive import (
    CallerGrant,
    RecursionState,
    RecursiveProposal,
    RecursiveStudy,
    RecursiveUsage,
    admit_proposal,
    admit_study,
    record_usage,
    terminate,
)
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1 import canonical

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
LIMITS = ResourceLimits(100, 1000, Decimal("10"))


def study_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "study_id": "root", "parent_id": None, "depth": 0,
        "recipe": {"min_metric": 0.0},
        "spec": {"state_fields": ["score"], "nested": {"name": "semantic"}},
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
    for field in ("dev", "confirmation"):
        payload.setdefault(f"{field}_sha256", canonical.sha256(sorted(
            item["content_sha256"] for item in payload[field]
        )))
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
        "study_id": "child", "parent_id": "root", "depth": 1, "recipe": {"min_metric": 0.1},
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
    spec = {"state_fields": ["score"], "nested": {"name": "semantic"}}
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
    with pytest.raises(ValidationError, match="grant_bounds"):
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
                           proposal_history=(first, distinct))
    restored = RecursionState.model_validate_json(state.model_dump_json())
    a, c = restored.proposal_history
    assert a.equivalence_sha256 == renamed.equivalence_sha256
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
    with pytest.raises(ValidationError, match="max_depth"):
        RecursionState(studies=(root, child()), grant=grant())
    root = RecursiveStudy.model_validate(study_payload(max_descendants=1))
    state = RecursionState(studies=(root, child(max_descendants=1)), grant=grant())
    assert state.refuse_reason(START) == "max_descendants"
    with pytest.raises(ValidationError, match="second_root"):
        RecursionState(studies=(root, RecursiveStudy.model_validate(study_payload(study_id="root-2"))),
                       grant=grant())
    with pytest.raises(ValidationError, match="reused_study_id"):
        RecursionState(studies=(root, root), grant=grant())


# All admission paths use the same live context; snapshots contain none of it.
def live_context() -> dict[str, object]:
    return {
        "policy": StandingPolicy(
            policy_id="spend-policy", action_class="spend_beyond_threshold",
            money_limit_usd=Decimal(10), expires_at=END, decision_id="decision-1",
        ),
        "authority": ChangeAuthority("owner", "decision-1", "owner"),
        "action": ActionRequest("spend_beyond_threshold", amount_usd=Decimal(10)),
        "repositories": {"repo": RepositoryRecord("repo", "main")},
        "now": START,
    }


def admit(state: RecursionState, candidate: RecursiveStudy, **context: object) -> RecursionState:
    return admit_study(state, candidate, state.grant, **{**live_context(), **context})


def admitted_root(**overrides: object) -> RecursionState:
    return admit(RecursionState(grant=grant()), RecursiveStudy.model_validate(study_payload(**overrides)))


def assert_refusal(before: RecursionState, after: RecursionState, reason: str) -> None:
    assert after.terminal_reason == reason
    assert after.studies == before.studies
    assert after.proposal_history == before.proposal_history
    assert after.usage == before.usage
    restored = RecursionState.model_validate_json(after.model_dump_json())
    assert restored.terminal_reason == reason
    assert admit(restored, child(study_id="fallback")) == restored


def test_admit_root_child_and_restored_child_with_live_authority() -> None:
    state = admitted_root()
    assert state.terminal_reason is None
    assert [item.study_id for item in state.studies] == ["root"]
    restored = RecursionState.model_validate_json(state.model_dump_json())
    result = admit(restored, child())
    assert result.terminal_reason is None
    assert [(item.study_id, item.parent_id, item.depth) for item in result.studies] == [
        ("root", None, 0), ("child", "root", 1),
    ]
    assert state.studies == restored.studies


@pytest.mark.parametrize("field", ["grant", "policy", "authority", "action", "repositories", "now"])
@pytest.mark.parametrize("has_root", [False, True])
def test_every_admission_requires_live_context(field: str, has_root: bool) -> None:
    state = admitted_root() if has_root else RecursionState(grant=grant())
    state = RecursionState.model_validate_json(state.model_dump_json())
    context = {**live_context(), "grant": state.grant, field: None}
    result = admit_study(state, child() if has_root else RecursiveStudy.model_validate(study_payload()), **context)
    assert_refusal(state, result, "utc_now_required" if field == "now" else "authority_context_required")


@pytest.mark.parametrize("authority,reason", [
    (ChangeAuthority("task-agent", "decision-1", "owner"), "worker_not_permitted"),
    (ChangeAuthority("automation", "decision-1", "owner"), "worker_not_permitted"),
    (ChangeAuthority("unknown", "decision-1", "owner"), "unknown_actor"),
    (ChangeAuthority("owner", None, "owner"), "decision_required"),
    (ChangeAuthority("owner", "decision-1", "client"), "owner_signature_required"),
    (ChangeAuthority("owner", "other", "owner"), "authority_binding_mismatch"),
])
def test_original_grant_requires_owner_authority(authority: ChangeAuthority, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(), authority=authority), reason)


@pytest.mark.parametrize("changes,reason", [
    ({"decision_id": None}, "missing_decision"),
    ({"decision_id": "other"}, "authority_binding_mismatch"),
    ({"policy_id": "other"}, "authority_binding_mismatch"),
    ({"action_class": "not-tier2"}, "not_tier2"),
    ({"money_limit_usd": None}, "missing_bound"),
    ({"money_limit_usd": Decimal(0)}, "money_limit"),
    ({"money_limit_usd": Decimal(9)}, "policy_grant_bounds"),
    ({"money_limit_usd": Decimal("Infinity")}, "policy_grant_bounds"),
    ({"money_limit_usd": Decimal("NaN")}, "invalid_authority_context"),
    ({"expires_at": None}, "policy_grant_bounds"),
    ({"expires_at": END - timedelta(seconds=1)}, "policy_grant_bounds"),
    ({"expires_at": END.replace(tzinfo=None)}, "policy_grant_bounds"),
    ({"repositories": ("missing",)}, "unknown_repository"),
])
def test_live_policy_cannot_be_replaced_or_exceeded(changes: dict, reason: str) -> None:
    state = admitted_root()
    policy = replace(live_context()["policy"], **changes)
    assert_refusal(state, admit(state, child(), policy=policy), reason)


@pytest.mark.parametrize("action,reason", [
    (ActionRequest("network_access", destination="example.org"), "invalid_action"),
    (ActionRequest("network_access", amount_usd=Decimal(1)), "policy_not_covered"),
    (ActionRequest("spend_beyond_threshold", amount_usd=Decimal(11)), "policy_not_covered"),
    (ActionRequest("spend_beyond_threshold", amount_usd=Decimal(-1)), "policy_not_covered"),
    (ActionRequest("spend_beyond_threshold", amount_usd=Decimal("NaN")), "invalid_action"),
])
def test_action_must_be_covered(action: ActionRequest, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(), action=action), reason)


@pytest.mark.parametrize("now,reason", [
    (START.replace(tzinfo=None), "utc_now_required"),
    (START.astimezone(timezone(timedelta(hours=1))), "utc_now_required"),
    (START - timedelta(microseconds=1), "outside_window"),
    (END, "policy_not_covered"),
])
def test_admission_checks_utc_and_half_open_window(now: datetime, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(), now=now), reason)


def test_window_end_refused_even_when_policy_outlives_grant() -> None:
    state = admitted_root()
    policy = replace(live_context()["policy"], expires_at=END + timedelta(hours=1))
    assert_refusal(state, admit(state, child(), policy=policy, now=END), "outside_window")
    assert admit(state, child(), now=END - timedelta(microseconds=1)).terminal_reason is None
    assert_refusal(state, admit(state, child(window_start=START + timedelta(seconds=1))), "outside_window")


@pytest.mark.parametrize("changes", [
    {"caller_id": "other"}, {"signature_sha256": "9" * 64},
    {"policy_id": "other"}, {"decision_id": "other"},
    {"max_depth": 3}, {"max_descendants": 3},
    {"limits": ResourceLimits(101, 1000, Decimal(10))},
    {"limits": ResourceLimits(100, 1001, Decimal(10))},
    {"limits": ResourceLimits(100, 1000, Decimal(11))},
    {"window_start": START - timedelta(seconds=1)},
    {"window_end": END + timedelta(seconds=1)},
])
def test_grant_identity_and_signed_bounds_cannot_change(changes: dict) -> None:
    state = admitted_root()
    result = admit_study(state, child(), grant(**changes), **live_context())
    assert_refusal(state, result, "grant_changed")


@pytest.mark.parametrize("changes", [
    {"max_depth": 3}, {"max_descendants": 3},
    {"limits": ResourceLimits(101, 1000, Decimal(10))},
    {"limits": ResourceLimits(100, 1001, Decimal(10))},
    {"limits": ResourceLimits(100, 1000, Decimal(11))},
    {"window_start": START - timedelta(seconds=1)},
    {"window_end": END + timedelta(seconds=1)},
])
def test_study_cannot_enlarge_grant_bounds(changes: dict) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(**changes)), "grant_bounds")


@pytest.mark.parametrize("field,value", [
    ("objective", "Different objective"), ("method_version", "r13.v2"),
    ("method_sha256", "9" * 64), ("lower_researcher_sha256", "9" * 64),
    ("evaluator_component_sha256", "9" * 64), ("evaluator_artifact_sha256", "9" * 64),
    ("protocol_sha256", "9" * 64),
    ("upstream_assets", [{"identity": "strategy.v2", "content_sha256": "f" * 64}]),
    ("upstream_assets", [{"identity": "strategy.v1", "content_sha256": "9" * 64}]),
])
def test_child_cannot_replace_pinned_bindings(field: str, value: object) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(**{field: value})), "binding_changed")


@pytest.mark.parametrize("changes,reason", [
    ({"study_id": "root"}, "reused_study_id"),
    ({"parent_id": "unadmitted"}, "unadmitted_parent"),
    ({"parent_id": "child"}, "ancestry_cycle"),
    ({"depth": 2}, "parent_depth"),
])
def test_child_requires_admitted_parent_and_exact_depth(changes: dict, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(**changes)), reason)


def test_standalone_child_has_no_parent_or_inherited_authority() -> None:
    state = RecursionState(grant=grant())
    assert_refusal(state, admit(state, child()), "unadmitted_parent")
    assert_refusal(state, admit_study(state, child(), state.grant), "utc_now_required")


def test_second_root_cannot_bypass_single_descendant_cap() -> None:
    state = admitted_root(max_descendants=1)
    state = admit(state, child(max_descendants=1))
    assert state.terminal_reason is None
    second = RecursiveStudy.model_validate(study_payload(study_id="second-root", max_descendants=1))
    assert_refusal(state, admit(state, second), "second_root")


def test_siblings_and_renamed_ids_count_all_admitted_history() -> None:
    state = admit(admitted_root(max_descendants=1), child(max_descendants=1))
    # Completion has no removal transition: usage and proposals remain in history.
    state = RecursionState.model_validate({
        **state.model_dump(), "proposal_history": (proposal(study_id="child"),),
        "usage": (RecursiveUsage(study_id="child", compute_seconds=1, tokens=1, usd=1),),
    })
    sibling = child(study_id="renamed", max_descendants=1,
                    dev=[{"task_id": "fresh-dev", "content_sha256": "6" * 64}],
                    confirmation=[{"task_id": "fresh-confirm", "content_sha256": "7" * 64}])
    assert_refusal(state, admit(state, sibling), "max_descendants")


@pytest.mark.parametrize("changes", [
    {"max_descendants": 2}, {"max_depth": 2},
    {"window_start": START}, {"window_end": END},
    {"limits": LIMITS},
])
def test_child_cannot_enlarge_narrowed_ancestor(changes: dict) -> None:
    state = admit(RecursionState(grant=grant()), RecursiveStudy.model_validate(study_payload(
        max_depth=1, max_descendants=1, window_start=START + timedelta(seconds=1),
        window_end=END - timedelta(seconds=1), limits=ResourceLimits(50, 500, Decimal(5)),
    )), now=START + timedelta(seconds=1))
    candidate = child(**{
        "max_depth": 1, "max_descendants": 1,
        "window_start": START + timedelta(seconds=1), "window_end": END - timedelta(seconds=1),
        "limits": ResourceLimits(50, 500, Decimal(5)), **changes,
    })
    assert_refusal(state, admit(state, candidate, now=START + timedelta(seconds=1)), "ancestor_bounds")


def test_parent_descendant_cap_counts_grandchildren_without_using_sibling_budget() -> None:
    state = admit(admitted_root(), child(max_descendants=0))
    grandchild = child(study_id="grandchild", parent_id="child", depth=2, max_descendants=0,
                       dev=[{"task_id": "fresh-dev", "content_sha256": "6" * 64}],
                       confirmation=[{"task_id": "fresh-confirm", "content_sha256": "7" * 64}])
    assert_refusal(state, admit(state, grandchild), "max_descendants")


@pytest.mark.parametrize("corruption,reason", [
    ({"parent_id": "missing"}, "unadmitted_parent"),
    ({"parent_id": "child"}, "ancestry_cycle"),
    ({"depth": 2}, "parent_depth"),
])
def test_corrupt_restored_ancestry_cannot_authorize_child(corruption: dict, reason: str) -> None:
    state = admitted_root().model_copy(update={"studies": (
        RecursiveStudy.model_validate(study_payload()), child().model_copy(update=corruption),
    )})
    result = admit(state, child(study_id="new", parent_id="child", depth=2))
    assert result.terminal_reason == reason
    assert result.studies == state.studies


def test_invalid_explicit_root_depth_refused_without_admitting_history() -> None:
    state = RecursionState(grant=grant())
    invalid = RecursiveStudy.model_validate(study_payload()).model_copy(update={"depth": 1})
    assert_refusal(state, admit(state, invalid), "invalid_admission")


def grandchild(**overrides: object) -> RecursiveStudy:
    return child(**{
        "study_id": "grandchild", "parent_id": "child", "depth": 2, "recipe": {"min_metric": 0.2},
        "dev": [{"task_id": "fresh-dev", "content_sha256": "6" * 64}],
        "confirmation": [{"task_id": "fresh-confirm", "content_sha256": "7" * 64}],
        **overrides,
    })


def test_complete_ancestry_admits_grandchild_but_root_counts_whole_tree() -> None:
    state = admit(admit(admitted_root(), child()), grandchild())
    assert state.terminal_reason is None
    assert [item.depth for item in state.studies] == [0, 1, 2]
    sibling = child(study_id="sibling",
                    dev=[{"task_id": "sibling-dev", "content_sha256": "8" * 64}],
                    confirmation=[{"task_id": "sibling-confirm", "content_sha256": "9" * 64}])
    assert_refusal(state, admit(state, sibling), "max_descendants")


def test_depth_must_obey_parent_cap_even_when_grant_allows_more() -> None:
    state = admit(admitted_root(), child(max_depth=1))
    assert_refusal(state, admit(state, grandchild()), "max_depth")


@pytest.mark.parametrize("usage", [
    {"compute_seconds": 50, "tokens": 0, "usd": 0},
    {"compute_seconds": 0, "tokens": 500, "usd": 0},
    {"compute_seconds": 0, "tokens": 0, "usd": 5},
])
def test_narrowed_parent_quota_terminates_admission_at_equality(usage: dict) -> None:
    limits = ResourceLimits(50, 500, Decimal(5))
    state = admit(admitted_root(), child(limits=limits))
    state = RecursionState.model_validate({
        **state.model_dump(), "usage": (RecursiveUsage(study_id="child", **usage),),
    })
    assert_refusal(state, admit(state, grandchild(limits=limits)), "quota_exhausted")


def test_completed_child_window_does_not_block_sibling_but_still_counts() -> None:
    state = admit(admitted_root(), child(window_end=START + timedelta(seconds=1)))
    sibling = child(study_id="sibling",
                    dev=[{"task_id": "sibling-dev", "content_sha256": "6" * 64}],
                    confirmation=[{"task_id": "sibling-confirm", "content_sha256": "7" * 64}])
    result = admit(state, sibling, now=START + timedelta(seconds=2))
    assert result.terminal_reason is None
    assert [item.study_id for item in result.studies] == ["root", "child", "sibling"]
    assert_refusal(state, admit(state, grandchild(window_end=START + timedelta(seconds=1)),
                                now=START + timedelta(seconds=2)), "outside_window")


@pytest.mark.parametrize("context,reason", [
    ({"repositories": "repo"}, "authority_context_required"),
    ({"repositories": {"repo": "not a repository"}}, "invalid_repositories"),
    ({"policy": {"policy_id": "spend-policy"}}, "authority_context_required"),
    ({"authority": {"answered_by_kind": "owner"}}, "authority_context_required"),
    ({"action": {"amount_usd": 10}}, "authority_context_required"),
])
def test_invalid_context_cannot_be_authorized_by_artifact_shape(context: dict, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit(state, child(), **context), reason)


def test_reused_cohort_refusal_retains_proposals_and_usage() -> None:
    state = RecursionState.model_validate({
        **admitted_root().model_dump(), "proposal_history": (proposal(),),
        "usage": (RecursiveUsage(study_id="root", compute_seconds=1, tokens=1, usd=1),),
    })
    reused = child(dev=[{"task_id": "dev-1", "content_sha256": "4" * 64}])
    assert_refusal(state, admit(state, reused), "cohort_reused")


@pytest.mark.parametrize("field", ["dev", "confirmation", "dev_sha256", "confirmation_sha256"])
@pytest.mark.parametrize("has_root", [False, True])
def test_missing_cohort_evidence_or_digest_cannot_admit(field: str, has_root: bool) -> None:
    state = admitted_root() if has_root else RecursionState(grant=grant())
    candidate = child() if has_root else RecursiveStudy.model_validate(study_payload())
    payload = dict(candidate)
    del payload[field]
    invalid = RecursiveStudy.model_construct(**payload)
    assert_refusal(state, admit(state, invalid), "invalid_admission")


@pytest.mark.parametrize("field", ["dev", "confirmation"])
@pytest.mark.parametrize("has_root", [False, True])
def test_empty_evidence_cannot_inherit_or_claim_a_cohort(field: str, has_root: bool) -> None:
    state = admitted_root() if has_root else RecursionState(grant=grant())
    candidate = child() if has_root else RecursiveStudy.model_validate(study_payload())
    digest = getattr(state.studies[0], f"{field}_sha256") if has_root else canonical.sha256([])
    invalid = candidate.model_copy(update={field: (), f"{field}_sha256": digest})
    assert_refusal(state, admit(state, invalid), "invalid_admission")


@pytest.mark.parametrize("field", ["dev_sha256", "confirmation_sha256"])
def test_contradictory_cohort_digest_cannot_claim_fresh_evidence(field: str) -> None:
    state = admitted_root()
    # In particular, a development digest cannot name the parent's confirmation
    # while its evidence claims a fresh development cohort.
    invalid = child().model_copy(update={field: state.studies[0].confirmation_sha256})
    assert_refusal(state, admit(state, invalid), "invalid_admission")


@pytest.mark.parametrize("source", ["dev", "confirmation"])
@pytest.mark.parametrize("destination", ["dev", "confirmation"])
@pytest.mark.parametrize("overlap", ["task_id", "renamed_content", "partial_content"])
def test_either_cohort_cannot_reuse_any_parent_cohort(
    source: str, destination: str, overlap: str,
) -> None:
    state = RecursionState.model_validate({
        **admitted_root().model_dump(), "proposal_history": (proposal(),),
        "usage": (RecursiveUsage(study_id="root", compute_seconds=1, tokens=1, usd=1),),
    })
    prior = getattr(state.studies[0], source)[0]
    evidence = [{
        "task_id": prior.task_id if overlap == "task_id" else "renamed",
        "content_sha256": "8" * 64 if overlap == "task_id" else prior.content_sha256,
    }]
    if overlap == "partial_content":
        evidence.append({"task_id": "extra", "content_sha256": "9" * 64})
    candidate = child(**{destination: evidence})
    if overlap == "renamed_content":
        assert getattr(candidate, f"{destination}_sha256") == getattr(
            state.studies[0], f"{source}_sha256"
        )
    elif overlap == "partial_content":
        assert getattr(candidate, f"{destination}_sha256") != getattr(
            state.studies[0], f"{source}_sha256"
        )
    assert_refusal(state, admit(state, candidate), "cohort_reused")


@pytest.mark.parametrize("source_study", ["root", "child"])
@pytest.mark.parametrize("source", ["dev", "confirmation"])
def test_grandchild_cannot_leak_ancestor_content(source_study: str, source: str) -> None:
    state = admit(admitted_root(), child())
    prior = next(item for item in state.studies if item.study_id == source_study)
    candidate = grandchild(dev=[{
        "task_id": "renamed-ancestor", "content_sha256": getattr(prior, source)[0].content_sha256,
    }])
    assert_refusal(state, admit(state, candidate), "cohort_reused")


@pytest.mark.parametrize("source", ["dev", "confirmation"])
def test_expired_sibling_content_remains_reserved(source: str) -> None:
    state = admit(admitted_root(), child(window_end=START + timedelta(seconds=1)))
    prior = getattr(state.studies[1], source)[0]
    sibling = grandchild(study_id="sibling", parent_id="root", depth=1,
                         confirmation=[{"task_id": "renamed-sibling", "content_sha256": prior.content_sha256}])
    assert_refusal(state, admit(state, sibling, now=START + timedelta(seconds=2)), "cohort_reused")


def test_terminal_study_history_cannot_be_discarded_by_retry() -> None:
    state = admit(admitted_root(), child())
    refused = admit(state, grandchild(dev=[{
        "task_id": "renamed", "content_sha256": state.studies[0].dev[0].content_sha256,
    }]))
    assert_refusal(state, refused, "cohort_reused")
    assert admit(refused, grandchild()) == refused


@pytest.mark.parametrize("corruption", ["empty", "contradictory", "reused"])
def test_restored_history_cannot_hide_invalid_cohort_evidence(corruption: str) -> None:
    state = admit(admitted_root(), child())
    admitted = state.studies[1]
    if corruption == "empty":
        admitted = admitted.model_copy(update={"confirmation": ()})
    elif corruption == "contradictory":
        admitted = admitted.model_copy(update={"dev_sha256": state.studies[0].confirmation_sha256})
    else:
        admitted = child(dev=[{"task_id": "renamed", "content_sha256": "1" * 64}])
    state = state.model_copy(update={"studies": (state.studies[0], admitted)})
    result = admit(state, grandchild())
    assert result.terminal_reason == ("cohort_reused" if corruption == "reused" else "invalid_history")
    assert result.studies == state.studies


def test_canonical_cohort_identity_ignores_order_and_task_labels() -> None:
    evidence = [
        {"task_id": "task-a", "content_sha256": "6" * 64},
        {"task_id": "task-b", "content_sha256": "7" * 64},
    ]
    state = admitted_root(dev=evidence)
    renamed = child(dev=[
        {"task_id": "renamed-b", "content_sha256": "7" * 64},
        {"task_id": "renamed-a", "content_sha256": "6" * 64},
    ])
    assert renamed.dev_sha256 == state.studies[0].dev_sha256
    assert_refusal(state, admit(state, renamed), "cohort_reused")
    contradictory = renamed.model_copy(update={"dev_sha256": canonical.sha256(evidence)})
    assert_refusal(state, admit(state, contradictory), "invalid_admission")


@pytest.mark.parametrize("overlap", ["task_id", "content_sha256"])
def test_admission_rejects_overlap_between_own_cohorts(overlap: str) -> None:
    state = admitted_root()
    candidate = child()
    evidence = candidate.confirmation[0].model_copy(update={
        overlap: getattr(candidate.dev[0], overlap),
    })
    invalid = candidate.model_copy(update={
        "confirmation": (evidence,),
        "confirmation_sha256": canonical.sha256([evidence.content_sha256]),
    })
    assert_refusal(state, admit(state, invalid), "invalid_admission")


def test_fresh_disjoint_multitask_cohorts_admit_with_canonical_content_binding() -> None:
    state = admitted_root()
    candidate = child(dev=[
        {"task_id": "new-b", "content_sha256": "7" * 64},
        {"task_id": "new-a", "content_sha256": "6" * 64},
    ], confirmation=[
        {"task_id": "new-c", "content_sha256": "8" * 64},
        {"task_id": "new-d", "content_sha256": "9" * 64},
    ], dev_sha256=canonical.sha256(["6" * 64, "7" * 64]),
       confirmation_sha256=canonical.sha256(["8" * 64, "9" * 64]))
    result = admit(RecursionState.model_validate_json(state.model_dump_json()), candidate)
    assert result.terminal_reason is None
    assert [item.study_id for item in result.studies] == ["root", "child"]
    assert RecursionState.model_validate_json(result.model_dump_json()) == result


def admit_prop(state: RecursionState, candidate: RecursiveProposal, **context: object) -> RecursionState:
    return admit_proposal(state, candidate, state.grant, **{**live_context(), **context})


def admitted_proposal(**overrides: object) -> RecursionState:
    return admit_prop(admitted_root(), proposal(**overrides))


def test_distinct_min_metric_proposals_admit_and_roundtrip(tmp_path: Path) -> None:
    state = admitted_proposal()
    assert state.terminal_reason is None
    distinct = proposal(0.6, proposal_id="proposal-2",
                        recipe={"label": "renamed", "min_metric": 0.6})
    assert distinct.equivalence_sha256 != state.proposal_history[0].equivalence_sha256
    result = admit_prop(state, distinct)
    assert result.terminal_reason is None
    assert [item.proposal_id for item in result.proposal_history] == ["proposal-1", "proposal-2"]
    snapshot = tmp_path / "state.json"
    snapshot.write_text(result.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == result
    assert [item.equivalence_sha256 for item in restored.proposal_history] == [
        item.equivalence_sha256 for item in result.proposal_history
    ]


def test_renamed_duplicate_of_ancestor_proposal_terminates() -> None:
    state = admit_prop(admit(admitted_root(), child()), proposal())
    renamed = proposal(proposal_id="renamed", study_id="child",
                       recipe={"name": "renamed", "min_metric": 0.5},
                       spec={"label": "renamed", "state_fields": ["score"],
                             "nested": {"name": "semantic"}})
    assert renamed.equivalence_sha256 == state.proposal_history[0].equivalence_sha256
    refused = admit_prop(state, renamed)
    assert_refusal(state, refused, "equivalent_proposal")
    assert admit_prop(refused, proposal(0.9, proposal_id="later")) == refused


def test_renamed_duplicate_of_completed_history_terminates() -> None:
    state = admit(admitted_root(), child(window_end=START + timedelta(seconds=1)))
    state = admit_prop(state, proposal(study_id="child"))
    assert state.terminal_reason is None
    renamed = proposal(proposal_id="renamed", study_id="root",
                       recipe={"id": "new", "min_metric": 0.5})
    refused = admit_prop(state, renamed, now=START + timedelta(seconds=2))
    assert_refusal(state, refused, "equivalent_proposal")


def test_renamed_duplicate_across_sibling_branches_terminates() -> None:
    state = admit(admitted_root(), child())
    state = admit_prop(state, proposal(study_id="child"))
    sibling = child(study_id="sibling",
                    dev=[{"task_id": "sibling-dev", "content_sha256": "6" * 64}],
                    confirmation=[{"task_id": "sibling-confirm", "content_sha256": "7" * 64}])
    state = admit(state, sibling)
    assert state.terminal_reason is None
    renamed = proposal(proposal_id="renamed", study_id="sibling",
                       recipe={"name": "other", "min_metric": 0.5})
    assert_refusal(state, admit_prop(state, renamed), "equivalent_proposal")


def test_reused_proposal_id_terminates_even_with_distinct_content() -> None:
    state = admitted_proposal()
    duplicate_id = proposal(0.9, proposal_id="proposal-1")
    assert duplicate_id.equivalence_sha256 != state.proposal_history[0].equivalence_sha256
    assert_refusal(state, admit_prop(state, duplicate_id), "reused_proposal_id")


def test_proposal_requires_admitted_study() -> None:
    empty = RecursionState(grant=grant())
    assert_refusal(empty, admit_prop(empty, proposal()), "unadmitted_study")
    state = admitted_root()
    assert_refusal(state, admit_prop(state, proposal(study_id="missing")), "unadmitted_study")


@pytest.mark.parametrize("field", ["grant", "policy", "authority", "action", "repositories", "now"])
def test_every_proposal_admission_requires_live_context(field: str) -> None:
    state = RecursionState.model_validate_json(admitted_root().model_dump_json())
    context = {**live_context(), "grant": state.grant, field: None}
    result = admit_proposal(state, proposal(), **context)
    assert_refusal(state, result, "utc_now_required" if field == "now" else "authority_context_required")


@pytest.mark.parametrize("authority,reason", [
    (ChangeAuthority("task-agent", "decision-1", "owner"), "worker_not_permitted"),
    (ChangeAuthority("automation", "decision-1", "owner"), "worker_not_permitted"),
    (ChangeAuthority("unknown", "decision-1", "owner"), "unknown_actor"),
    (ChangeAuthority("owner", None, "owner"), "decision_required"),
    (ChangeAuthority("owner", "decision-1", "client"), "owner_signature_required"),
    (ChangeAuthority("owner", "other", "owner"), "authority_binding_mismatch"),
])
def test_proposal_requires_owner_authority(authority: ChangeAuthority, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit_prop(state, proposal(), authority=authority), reason)


def test_proposal_admission_requires_the_original_grant() -> None:
    state = admitted_root()
    result = admit_proposal(state, proposal(), grant(decision_id="other"), **live_context())
    assert_refusal(state, result, "grant_changed")


@pytest.mark.parametrize("now,reason", [
    (START.replace(tzinfo=None), "utc_now_required"),
    (START.astimezone(timezone(timedelta(hours=1))), "utc_now_required"),
    (START - timedelta(microseconds=1), "outside_window"),
    (END, "policy_not_covered"),
])
def test_proposal_admission_checks_utc_and_half_open_window(now: datetime, reason: str) -> None:
    state = admitted_root()
    assert_refusal(state, admit_prop(state, proposal(), now=now), reason)


def test_proposal_under_expired_study_is_outside_window() -> None:
    state = admit(admitted_root(), child(window_end=START + timedelta(seconds=1)))
    refused = admit_prop(state, proposal(study_id="child"), now=START + timedelta(seconds=2))
    assert_refusal(state, refused, "outside_window")
    admitted = admit_prop(state, proposal(study_id="root"), now=START + timedelta(seconds=2))
    assert admitted.terminal_reason is None
    assert [item.study_id for item in admitted.proposal_history] == ["root"]


def test_proposal_admission_shares_tree_cumulative_quota() -> None:
    state = RecursionState.model_validate({
        **admitted_root().model_dump(),
        "usage": (RecursiveUsage(study_id="root", compute_seconds=100, tokens=0, usd=0),),
    })
    assert_refusal(state, admit_prop(state, proposal()), "quota_exhausted")


@pytest.mark.parametrize("key,reason", [
    ("objective", "binding_changed"),
    ("evaluator_component_sha256", "binding_changed"),
    ("evaluator_artifact_sha256", "binding_changed"),
    ("protocol_sha256", "binding_changed"),
    ("method_version", "binding_changed"),
    ("method_sha256", "binding_changed"),
    ("lower_researcher_sha256", "binding_changed"),
    ("upstream_assets", "binding_changed"),
    ("caller_id", "grant_changed"),
    ("policy_id", "grant_changed"),
    ("decision_id", "grant_changed"),
    ("signature_sha256", "grant_changed"),
    ("window_start", "grant_changed"),
    ("window_end", "grant_changed"),
    ("max_depth", "grant_changed"),
    ("max_descendants", "grant_changed"),
    ("limits", "grant_changed"),
    ("grant", "grant_changed"),
])
def test_proposal_cannot_replace_pinned_bindings_or_grant(key: str, reason: str) -> None:
    state = admitted_root()
    spec = {"state_fields": ["score"], key: "replacement"}
    assert_refusal(state, admit_prop(state, proposal(spec=spec)), reason)


def test_recipe_cannot_carry_replacement_keys() -> None:
    with pytest.raises(ValidationError):
        proposal(recipe={"min_metric": 0.5, "objective": "replace"})


def test_bookkeeping_labels_are_not_replacement_or_new_content() -> None:
    state = admitted_proposal()
    relabeled = proposal(proposal_id="relabeled",
                         recipe={"id": "x", "name": "x", "label": "x", "min_metric": 0.7},
                         spec={"id": "x", "name": "x", "label": "x", "state_fields": ["score"], "nested": {"name": "semantic"}})
    result = admit_prop(state, relabeled)
    assert result.terminal_reason is None
    first, second = result.proposal_history
    assert first.equivalence_sha256 != second.equivalence_sha256


def test_invalid_proposal_record_cannot_admit() -> None:
    state = admitted_root()
    invalid = RecursiveProposal.model_construct(
        proposal_id="raw", study_id="root", recipe=b'{"min_metric":0.5}', spec=b"{}",
    )
    assert_refusal(state, admit_prop(state, invalid), "invalid_admission")


def test_refused_proposal_terminates_without_removing_prior_state() -> None:
    state = admit(admitted_root(), child())
    state = admit_prop(state, proposal(study_id="child"))
    state = RecursionState.model_validate({
        **state.model_dump(),
        "usage": (RecursiveUsage(study_id="root", compute_seconds=1, tokens=1, usd=1),),
    })
    renamed = proposal(proposal_id="renamed", study_id="root",
                       recipe={"name": "renamed", "min_metric": 0.5})
    refused = admit_prop(state, renamed)
    assert_refusal(state, refused, "equivalent_proposal")
    assert admit_prop(refused, proposal(0.9, proposal_id="later")) == refused
    assert admit(refused, grandchild()) == refused


def test_terminate_latches_reason_and_blocks_all_admission(tmp_path: Path) -> None:
    state = admit(admitted_root(), child())
    state = RecursionState.model_validate({
        **state.model_dump(), "proposal_history": (proposal(),),
        "usage": (RecursiveUsage(study_id="child", compute_seconds=1, tokens=1, usd=1),),
    })
    stopped = terminate(state, "operator_stop")
    assert stopped.terminal_reason == "operator_stop"
    assert stopped.studies == state.studies
    assert stopped.proposal_history == state.proposal_history
    assert stopped.usage == state.usage
    assert terminate(stopped, "second_reason").terminal_reason == "operator_stop"
    assert admit(stopped, grandchild()) == stopped
    assert admit_prop(stopped, proposal(0.9, proposal_id="later")) == stopped
    halted = terminate(RecursionState(grant=grant()), "halt")
    assert halted.refuse_reason(START) == "halt"
    assert admit(halted, RecursiveStudy.model_validate(study_payload())) == halted
    snapshot = tmp_path / "state.json"
    snapshot.write_text(stopped.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == stopped
    assert restored.refuse_reason(START) == "operator_stop"
    assert admit(restored, grandchild()) == restored
    assert admit_prop(restored, proposal(0.9, proposal_id="later")) == restored


@pytest.mark.parametrize("reason", ["", "   ", None, 1])
def test_terminate_requires_a_nonempty_reason(reason: object) -> None:
    with pytest.raises(ValueError):
        terminate(admitted_root(), reason)  # type: ignore[arg-type]


def _snapshot_payload() -> dict[str, object]:
    state = RecursionState.model_validate({
        **admit(admitted_root(), child()).model_dump(),
        "proposal_history": (proposal(),),
        "usage": (RecursiveUsage(study_id="child", compute_seconds=1, tokens=1, usd=1),),
    })
    return json.loads(state.model_dump_json())


def _break_ancestry(payload: dict) -> None:
    payload["studies"][1]["parent_id"] = "missing"


def _drop_root(payload: dict) -> None:
    del payload["studies"][0]


def _add_second_root(payload: dict) -> None:
    payload["studies"].append({**payload["studies"][0], "study_id": "root-2"})


def _exceed_descendants(payload: dict) -> None:
    payload["studies"][0]["max_descendants"] = 0


def _duplicate_study_id(payload: dict) -> None:
    payload["studies"][1]["study_id"] = "root"


def _reuse_cohort(payload: dict) -> None:
    payload["studies"][1]["dev"] = payload["studies"][0]["dev"]
    payload["studies"][1]["dev_sha256"] = payload["studies"][0]["dev_sha256"]


def _duplicate_proposal_id(payload: dict) -> None:
    payload["proposal_history"].append(dict(payload["proposal_history"][0]))


def _unknown_proposal_study(payload: dict) -> None:
    payload["proposal_history"][0]["study_id"] = "missing"


def _unknown_usage_study(payload: dict) -> None:
    payload["usage"][0]["study_id"] = "missing"


def _negative_usage_seconds(payload: dict) -> None:
    payload["usage"][0]["compute_seconds"] = -1


def _negative_usage_tokens(payload: dict) -> None:
    payload["usage"][0]["tokens"] = -1


def _negative_usage_usd(payload: dict) -> None:
    payload["usage"][0]["usd"] = "-1"


def _nonfinite_usage_usd(payload: dict) -> None:
    payload["usage"][0]["usd"] = "NaN"


def _shrink_grant_window(payload: dict) -> None:
    payload["grant"]["window_end"] = (START + timedelta(minutes=30)).isoformat()


def _empty_terminal_reason(payload: dict) -> None:
    payload["terminal_reason"] = ""


@pytest.mark.parametrize("tamper", [
    _break_ancestry, _drop_root, _add_second_root, _exceed_descendants,
    _duplicate_study_id, _reuse_cohort, _duplicate_proposal_id,
    _unknown_proposal_study, _unknown_usage_study, _negative_usage_seconds,
    _negative_usage_tokens, _negative_usage_usd, _nonfinite_usage_usd,
    _shrink_grant_window, _empty_terminal_reason,
], ids=lambda tamper: tamper.__name__)
def test_malformed_restoration_refused(tmp_path: Path, tamper: Callable[[dict], None]) -> None:
    payload = _snapshot_payload()
    tamper(payload)
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        RecursionState.model_validate_json(snapshot.read_text())


def test_restoration_roundtrip_preserves_terminal_behavior_and_identities(tmp_path: Path) -> None:
    state = admit(admitted_root(), child())
    state = admit_prop(state, proposal(study_id="child"))
    state = RecursionState.model_validate({
        **state.model_dump(),
        "usage": (RecursiveUsage(study_id="child", compute_seconds=1, tokens=1, usd=1),),
    })
    state = terminate(state, "operator_stop")
    snapshot = tmp_path / "state.json"
    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == state
    assert restored.terminal_reason == "operator_stop"
    assert restored.refuse_reason(START) == "operator_stop"
    assert restored.grant == state.grant
    assert restored.studies[0].method_sha256 == "a" * 64
    assert [item.equivalence_sha256 for item in restored.proposal_history] == [
        state.proposal_history[0].equivalence_sha256
    ]
    assert [item.study_id for item in restored.usage] == ["child"]


def test_reloaded_state_admits_only_with_live_authority(tmp_path: Path) -> None:
    state = admitted_proposal()
    snapshot = tmp_path / "state.json"
    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == state
    assert_refusal(restored, admit_study(restored, child(), restored.grant), "utc_now_required")
    assert admit(restored, child()).terminal_reason is None
    admitted = admit_prop(restored, proposal(0.6, proposal_id="proposal-2"))
    assert admitted.terminal_reason is None
    assert [item.proposal_id for item in admitted.proposal_history] == ["proposal-1", "proposal-2"]


@pytest.mark.parametrize("kind", ["study", "proposal"])
@pytest.mark.parametrize("spec", [
    {"state_fields": ["changed"], "nested": {"name": "semantic"}},
    {"state_fields": ["score"], "nested": {"name": "changed"}},
])
def test_only_threshold_may_vary_from_study_baseline(kind: str, spec: dict) -> None:
    state = admitted_proposal()
    if kind == "study":
        result = admit(state, child(spec=spec))
    else:
        result = admit_prop(state, proposal(0.5, proposal_id="changed", spec=spec))
    assert_refusal(state, result, "spec_changed")


@pytest.mark.parametrize("kind", ["study", "proposal"])
def test_renamed_study_baseline_terminates_recursion(kind: str) -> None:
    state = admitted_root()
    recipe = {"name": "renamed", "min_metric": 0}
    spec = {"label": "renamed", "state_fields": ["score"], "nested": {"name": "semantic"}}
    if kind == "study":
        result = admit(state, child(recipe=recipe, spec=spec))
    else:
        result = admit_prop(state, proposal(recipe=recipe, spec=spec))
    assert_refusal(state, result, "equivalent_proposal")


def test_study_recipe_checks_all_prior_proposals_after_reload(tmp_path: Path) -> None:
    state = admitted_proposal()
    snapshot = tmp_path / "state.json"
    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    renamed = child(recipe={"label": "renamed", "min_metric": 0.5})
    assert_refusal(restored, admit(restored, renamed), "equivalent_proposal")
    assert admit(restored, child(recipe={"min_metric": 0.6})).terminal_reason is None


@pytest.mark.parametrize("metrics", [(1, 1.0), (0.0, -0.0), (9007199254740992, 9007199254740993)])
def test_threshold_identity_matches_generated_strategy(metrics: tuple) -> None:
    first, equivalent = metrics
    state = admit_prop(admitted_root(), proposal(first))
    if first == 0:
        # Zero already equals root baseline.
        assert state.terminal_reason == "equivalent_proposal"
    else:
        assert state.terminal_reason is None
        assert_refusal(state, admit_prop(state, proposal(equivalent, proposal_id="renamed")),
                       "equivalent_proposal")


@pytest.mark.parametrize("field,value", [
    ("objective", "replacement"), ("method_sha256", "9" * 64),
    ("lower_researcher_sha256", "9" * 64), ("protocol_sha256", "9" * 64),
    ("evaluator_component_sha256", "9" * 64), ("evaluator_artifact_sha256", "9" * 64),
    ("upstream_assets", [{"identity": "changed", "content_sha256": "f" * 64}]),
    ("max_descendants", 0), ("max_depth", 1),
    ("limits", {"max_wall_clock_seconds": 1, "max_model_tokens": 1000, "max_usd": "10"}),
    ("recipe", '{"min_metric":0}'),
    ("spec", '{"state_fields":["changed"]}'),
])
def test_restore_rejects_child_binding_and_ancestor_cap_bypass(
    tmp_path: Path, field: str, value: object,
) -> None:
    state = admit(admit(admitted_root(), child()), grandchild())
    assert state.terminal_reason is None
    payload = json.loads(state.model_dump_json())
    payload["studies"][1][field] = value
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        RecursionState.model_validate_json(snapshot.read_text())


@pytest.mark.parametrize("corruption", ["replacement", "grant", "spec", "duplicate", "baseline"])
def test_restore_rejects_invalid_proposal_history(tmp_path: Path, corruption: str) -> None:
    payload = _snapshot_payload()
    retained = payload["proposal_history"][0]
    if corruption in {"replacement", "grant", "spec"}:
        key = {"replacement": "objective", "grant": "grant", "spec": "state_fields"}[corruption]
        retained["spec"] = json.dumps({key: "replacement"})
    elif corruption == "duplicate":
        payload["proposal_history"].append({**retained, "proposal_id": "renamed"})
    else:
        retained["recipe"] = '{"min_metric":0}'
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        RecursionState.model_validate_json(snapshot.read_text())


def test_rejected_duplicate_reload_keeps_identity_and_terminal_reason(tmp_path: Path) -> None:
    prior = admitted_proposal()
    rejected = admit_prop(prior, proposal(proposal_id="renamed"))
    assert rejected.terminal_reason == "equivalent_proposal"
    snapshot = tmp_path / "state.json"
    snapshot.write_text(rejected.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == rejected
    assert restored.proposal_history[0].equivalence_sha256 == prior.proposal_history[0].equivalence_sha256
    assert admit_prop(restored, proposal(0.9, proposal_id="distinct")) == restored
    assert admit(restored, child()) == restored


def test_restore_counts_descendants_under_zero_cap_even_when_child_bounds_fit(tmp_path: Path) -> None:
    state = admit(admit(admitted_root(), child()), grandchild(max_descendants=0))
    assert state.terminal_reason is None
    payload = json.loads(state.model_dump_json())
    payload["studies"][1]["max_descendants"] = 0
    snapshot = tmp_path / "state.json"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match="max_descendants"):
        RecursionState.model_validate_json(snapshot.read_text())


@pytest.mark.parametrize("kind", ["study", "proposal"])
def test_baseline_recipe_and_spec_must_be_explicit(kind: str) -> None:
    payload = study_payload() if kind == "study" else proposal().model_dump()
    model = RecursiveStudy if kind == "study" else RecursiveProposal
    for field in ("recipe", "spec"):
        with pytest.raises(ValidationError, match=field):
            model.model_validate({key: value for key, value in payload.items() if key != field})


# ==============================================================================
# OMP-319-s01-s05: Resource transitions, record_usage, and shared admission gates
# ==============================================================================

@pytest.mark.parametrize("axis,equality_delta,exceeded_delta", [
    ("compute_seconds", {"compute_seconds": 100.0}, {"compute_seconds": 100.1}),
    ("tokens", {"tokens": 1000}, {"tokens": 1001}),
    ("usd", {"usd": Decimal("10.0")}, {"usd": Decimal("10.01")}),
])
def test_record_usage_independent_axis_exhaustion_and_equality(
    axis: str, equality_delta: dict, exceeded_delta: dict,
) -> None:
    # Root has LIMITS: compute=100, tokens=1000, usd=10
    state = admitted_root()
    # Below cap: state remains unterminated
    below = {k: v / 2 if k != "tokens" else v // 2 for k, v in equality_delta.items()}
    sub_state = record_usage(state, "root", **below)
    assert sub_state.terminal_reason is None
    assert len(sub_state.usage) == 1

    # Exact equality: terminates with quota_exhausted and retains crossing event
    eq_state = record_usage(state, "root", **equality_delta)
    assert eq_state.terminal_reason == "quota_exhausted"
    assert len(eq_state.usage) == 1
    assert eq_state.refuse_reason(START) == "quota_exhausted"

    # Strictly exceeded: terminates with quota_exhausted and retains crossing event
    exc_state = record_usage(state, "root", **exceeded_delta)
    assert exc_state.terminal_reason == "quota_exhausted"
    assert len(exc_state.usage) == 1
    assert exc_state.refuse_reason(START) == "quota_exhausted"


def test_record_usage_crossing_usage_retained_across_transitions() -> None:
    state = admitted_root()
    # Spend $6 of $10 cap on root
    state = record_usage(state, "root", usd=Decimal("6.00"))
    assert state.terminal_reason is None
    assert len(state.usage) == 1
    assert state.usage[0].usd == Decimal("6.00")

    # Spend another $5: total becomes $11 >= $10 root cap -> quota_exhausted
    state = record_usage(state, "root", usd=Decimal("5.00"))
    assert state.terminal_reason == "quota_exhausted"
    assert len(state.usage) == 2
    assert state.usage[0].usd == Decimal("6.00")
    assert state.usage[1].usd == Decimal("5.00")
    assert sum(item.usd for item in state.usage) == Decimal("11.00")


@pytest.mark.parametrize("cap,deltas,exhausted", [
    ("1.00000000000000000000000000001", ("1.00000000000000000000000000001",), True),
    ("1.00000000000000000000000000001", ("1", "0.00000000000000000000000000001"), True),
    ("1", ("0.99999999999999999999999999999", "0.00000000000000000000000000002"), True),
    ("1.00000000000000000000000000002", ("1.00000000000000000000000000001",), False),
])
@pytest.mark.parametrize("precision", [2, 28])
def test_decimal_usage_transition_and_reloaded_gates_are_exact(
    tmp_path: Path, cap: str, deltas: tuple[str, ...], exhausted: bool, precision: int,
) -> None:
    limits = ResourceLimits(100, 1000, Decimal(cap))
    state = admitted_root(limits=limits)
    events = tuple(RecursiveUsage(
        study_id="root", compute_seconds=0, tokens=0, usd=Decimal(delta),
    ) for delta in deltas)
    with localcontext() as context:
        context.prec = precision
        recorded = state
        for event in events:
            recorded = record_usage(recorded, event)
        assert recorded.usage == events
        assert recorded.terminal_reason == ("quota_exhausted" if exhausted else None)

        # Restored usage need not already carry a terminal latch: gates still
        # compare exact totals, using fresh authority supplied by admit helpers.
        snapshot = tmp_path / "state.json"
        snapshot.write_text(RecursionState(
            studies=state.studies, grant=state.grant, usage=events,
        ).model_dump_json())
        restored = RecursionState.model_validate_json(snapshot.read_text())
        assert restored.usage == events
        assert restored.refuse_reason(START) == ("quota_exhausted" if exhausted else None)
        study_result = admit(restored, child(limits=limits))
        proposal_result = admit_prop(restored, proposal())
        if exhausted:
            assert_refusal(restored, study_result, "quota_exhausted")
            assert_refusal(restored, proposal_result, "quota_exhausted")
        else:
            assert study_result.studies == (*restored.studies, child(limits=limits))
            assert proposal_result.proposal_history == (proposal(),)
            assert study_result.terminal_reason is proposal_result.terminal_reason is None


def test_reloaded_narrowed_decimal_envelopes_refuse_study_and_proposal(tmp_path: Path) -> None:
    cap = Decimal("1.00000000000000000000000000001")
    limits = ResourceLimits(100, 1000, cap)
    state = admit(admitted_root(), child(limits=limits))
    # Spend recorded on root exhausts child's envelope, without exhausting root.
    state = record_usage(state, "root", usd=cap)
    assert state.terminal_reason is None
    snapshot = tmp_path / "state.json"
    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored.usage[0].usd == cap
    assert_refusal(restored, admit(restored, grandchild(limits=limits)), "quota_exhausted")
    assert_refusal(restored, admit_prop(restored, proposal(
        study_id="child", recipe={"min_metric": 0.6},
    )), "quota_exhausted")


@pytest.mark.parametrize("invalid_kwargs", [
    {"compute_seconds": -1},
    {"compute_seconds": float("inf")},
    {"compute_seconds": float("nan")},
    {"compute_seconds": True},
    {"tokens": -1},
    {"tokens": True},
    {"tokens": 1.5},
    {"usd": -1},
    {"usd": Decimal("-0.01")},
    {"usd": Decimal("NaN")},
    {"usd": Decimal("Inf")},
    {"usd": 0.05},  # float rejected: Decimal USD, no float conversion
    {"usd": 1.0},   # float rejected: Decimal USD, no float conversion
    {"usd": True},
    {"study_id": "missing_study"},
    {"study_id": ""},
    {"study_id": "   "},
])
def test_record_usage_negative_nonfinite_and_float_usd_refused(invalid_kwargs: dict) -> None:
    state = admitted_root()
    base = {"study_id": "root", "compute_seconds": 0.0, "tokens": 0, "usd": Decimal("0")}
    kwargs = {**base, **invalid_kwargs}
    with pytest.raises(ValueError):
        record_usage(state, **kwargs)
    # State usage remains completely unmodified
    assert state.usage == ()


def test_root_spent_budget_refuses_narrowed_child_and_its_proposal() -> None:
    # Root has cap $10. Spend $6 on root.
    state = admitted_root()
    state = record_usage(state, "root", usd=Decimal("6.00"))
    assert state.terminal_reason is None

    # Scenario 1: Attempting to admit a $1-cap child is refused with quota_exhausted
    narrowed_limits = ResourceLimits(100, 1000, Decimal("1.00"))
    child_1 = child(study_id="child-1", limits=narrowed_limits)
    refused_child_state = admit(state, child_1)
    assert_refusal(state, refused_child_state, "quota_exhausted")

    # Scenario 2: If child with $1 cap was admitted BEFORE the spend:
    pre_state = admit(admitted_root(), child(study_id="child-1", limits=narrowed_limits))
    assert pre_state.terminal_reason is None
    pre_state = record_usage(pre_state, "root", usd=Decimal("6.00"))
    # Proposal for that child is refused with quota_exhausted
    prop = proposal(study_id="child-1", recipe={"min_metric": 0.6})
    refused_prop_state = admit_prop(pre_state, prop)
    assert_refusal(pre_state, refused_prop_state, "quota_exhausted")

    # Root can still admit a study with cap $8 (or $10) and root's own proposals
    child_8 = child(study_id="child-8", limits=ResourceLimits(100, 1000, Decimal("8.00")))
    admitted_child_state = admit(state, child_8)
    assert admitted_child_state.terminal_reason is None
    assert "child-8" in {s.study_id for s in admitted_child_state.studies}

    root_prop = proposal(study_id="root", recipe={"min_metric": 0.6})
    admitted_root_prop = admit_prop(state, root_prop)
    assert admitted_root_prop.terminal_reason is None


def test_exhausted_ancestor_refuses_child() -> None:
    # Root ($10 cap) -> Parent ($5 cap).
    parent_limits = ResourceLimits(100, 1000, Decimal("5.00"))
    state = admit(admitted_root(), child(study_id="parent", limits=parent_limits))
    assert state.terminal_reason is None

    # Record $5 spend: parent envelope is exhausted
    state = record_usage(state, "parent", usd=Decimal("5.00"))
    assert state.terminal_reason == "quota_exhausted"

    # Parent attempting to admit a grandchild (even with $1 cap) is refused
    grandchild_limits = ResourceLimits(100, 1000, Decimal("1.00"))
    gc = child(
        study_id="grandchild", parent_id="parent", depth=2,
        limits=grandchild_limits,
        dev=[{"task_id": "gc-dev", "content_sha256": "8" * 64}],
        confirmation=[{"task_id": "gc-confirm", "content_sha256": "9" * 64}],
    )
    refused = admit(state, gc)
    assert refused.terminal_reason == "quota_exhausted"


def test_second_root_and_renamed_ids_cannot_reset_usage() -> None:
    state = admitted_root()
    state = record_usage(state, "root", usd=Decimal("6.00"))
    assert state.terminal_reason is None

    # Second root is refused
    second = RecursiveStudy.model_validate(study_payload(study_id="root-2"))
    assert_refusal(state, admit(state, second), "second_root")

    # Recording usage for fake / renamed IDs cannot reset or evade admitted tracking
    with pytest.raises(ValueError, match="not an admitted study"):
        record_usage(state, "fake-root-id", usd=Decimal("1.00"))

    # Legitimate child with $8 cap still inherits tree-cumulative spend ($6)
    child_8 = child(study_id="child-8", limits=ResourceLimits(100, 1000, Decimal("8.00")))
    state = admit(state, child_8)
    assert state.terminal_reason is None

    # Recording $2 more on child-8 hits the $8 cap (6 + 2 = 8 >= 8) -> quota_exhausted
    state = record_usage(state, "child-8", usd=Decimal("2.00"))
    assert state.terminal_reason == "quota_exhausted"


def test_tmp_path_reload_retains_exhaustion_descendants_duplicates_and_isolation(tmp_path: Path) -> None:
    state = admit(admitted_root(), child())
    state = admit_prop(state, proposal(study_id="root", recipe={"min_metric": 0.6}))
    snapshot = tmp_path / "state.json"
    snapshot.write_text(state.model_dump_json())
    live_restored = RecursionState.model_validate_json(snapshot.read_text())
    assert live_restored.terminal_reason is None
    assert_refusal(live_restored, admit_prop(live_restored, proposal(
        proposal_id="renamed", recipe={"min_metric": 0.6},
    )), "equivalent_proposal")
    reused = child(
        study_id="child-reused", recipe={"min_metric": 0.7},
        dev=[{"task_id": "root-dev-reuse", "content_sha256": "1" * 64}],
        confirmation=[{"task_id": "root-confirm-reuse", "content_sha256": "7" * 64}],
    )
    assert_refusal(live_restored, admit(live_restored, reused), "cohort_reused")

    state = record_usage(state, "root", compute_seconds=100.0)  # exact compute limit equality
    assert state.terminal_reason == "quota_exhausted"
    assert len(state.studies) == 2
    assert len(state.proposal_history) == 1
    assert len(state.usage) == 1

    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())

    # Retains exhaustion
    assert restored == state
    assert restored.terminal_reason == "quota_exhausted"
    assert restored.refuse_reason(START) == "quota_exhausted"

    # Retains descendants
    assert len(restored.studies) == 2
    assert [s.study_id for s in restored.studies] == ["root", "child"]

    assert admit_prop(restored, proposal(0.9, proposal_id="fresh")) == restored


def test_integrated_r13_min_metric_admission_to_reload_scenario(tmp_path: Path) -> None:
    # 1. Admit root with R13 min_metric = 0.5
    root_study = RecursiveStudy.model_validate(study_payload(recipe={"min_metric": 0.5}))
    state = admit(RecursionState(grant=grant()), root_study)
    assert state.terminal_reason is None
    assert len(state.studies) == 1

    # 2. Admit proposal for root with min_metric = 0.6
    prop1 = proposal(study_id="root", proposal_id="prop-root-1", recipe={"min_metric": 0.6})
    state = admit_prop(state, prop1)
    assert state.terminal_reason is None
    assert len(state.proposal_history) == 1

    # 3. Admit child study with distinct min_metric = 0.7 and narrowed limits
    child_limits = ResourceLimits(50, 500, Decimal("5.00"))
    child_study = child(
        study_id="child-1", parent_id="root", depth=1,
        recipe={"min_metric": 0.7}, limits=child_limits,
    )
    state = admit(state, child_study)
    assert state.terminal_reason is None
    assert len(state.studies) == 2

    # 4. Admit proposal for child with distinct min_metric = 0.8
    prop2 = proposal(study_id="child-1", proposal_id="prop-child-1", recipe={"min_metric": 0.8})
    state = admit_prop(state, prop2)
    assert state.terminal_reason is None
    assert len(state.proposal_history) == 2

    # 5. Record usage below caps
    state = record_usage(state, "root", compute_seconds=10.0, tokens=100, usd=Decimal("1.00"))
    state = record_usage(state, "child-1", compute_seconds=15.0, tokens=150, usd=Decimal("1.50"))
    assert state.terminal_reason is None
    assert len(state.usage) == 2

    # 6. Snapshot and reload via real tmp_path
    snapshot = tmp_path / "integrated_state.json"
    snapshot.write_text(state.model_dump_json())
    restored = RecursionState.model_validate_json(snapshot.read_text())
    assert restored == state
    assert restored.terminal_reason is None

    # 7. Fresh external authority check allows another distinct proposal (min_metric = 0.9)
    prop3 = proposal(study_id="child-1", proposal_id="prop-child-2", recipe={"min_metric": 0.9})
    restored = admit_prop(restored, prop3)
    assert restored.terminal_reason is None
    assert len(restored.proposal_history) == 3

    # 8. Record crossing usage on child: compute 25.0 brings total compute to 10 + 15 + 25 = 50 >= 50
    terminated = record_usage(restored, "child-1", compute_seconds=25.0)
    assert terminated.terminal_reason == "quota_exhausted"
    assert len(terminated.usage) == 3
    assert terminated.refuse_reason(START) == "quota_exhausted"

    # 9. All subsequent admissions blocked
    blocked_prop = proposal(study_id="child-1", proposal_id="prop-child-3", recipe={"min_metric": 1.0})
    assert admit_prop(terminated, blocked_prop) == terminated


def test_four_reported_bypasses_explicit_reproduction() -> None:
    # [P1] Standalone admission bypasses authority
    # Reproduced: admit_study without authority or known ancestry
    raw_state = RecursionState(grant=grant())
    assert_refusal(raw_state, admit_study(raw_state, child(), grant()), "utc_now_required")
    assert_refusal(raw_state, admit(raw_state, child(parent_id="unknown-parent")), "unadmitted_parent")

    # [P1] New root IDs bypass descendant cap
    # Reproduced: second root accepting children under max_descendants=1
    state1 = admitted_root(max_descendants=1)
    state1 = admit(state1, child(max_descendants=1))
    second = RecursiveStudy.model_validate(study_payload(study_id="second-root", max_descendants=1))
    assert_refusal(state1, admit(state1, second), "second_root")

    # [P1] Narrowed child budgets do not gate admission
    # Reproduced: $6 spent under $10 root cap, then admitted $1-cap child and its proposal
    state2 = admitted_root()
    state2 = record_usage(state2, "root", usd=Decimal("6.00"))
    child_1usd = child(study_id="narrow-1", limits=ResourceLimits(100, 1000, Decimal("1.00")))
    assert_refusal(state2, admit(state2, child_1usd), "quota_exhausted")

    # [P1] Empty cohorts bypass confirmation isolation
    # Reproduced: child admission with dev digest equal to parent confirm digest
    state3 = admitted_root()
    reused_cohort_child = child(
        study_id="reused-cohort",
        dev=[{"task_id": "root-confirm-task", "content_sha256": "2" * 64}],
        confirmation=[{"task_id": "new-confirm-task", "content_sha256": "9" * 64}],
    )
    assert_refusal(state3, admit(state3, reused_cohort_child), "cohort_reused")
