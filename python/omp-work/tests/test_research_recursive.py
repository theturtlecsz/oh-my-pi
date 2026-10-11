"""Immutable recursion bindings, history isolation, and R13 proposal identity."""

from dataclasses import FrozenInstanceError, replace
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
    admit_study,
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
        "study_id": "grandchild", "parent_id": "child", "depth": 2,
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
