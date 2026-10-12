"""Maintenance asks for owner decisions without reusing confirmation or activating upstream."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from omp_work.research.accounting import ResourceLimits
from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_benefit import (
    AxisBenefit, BenefitAxes, BenefitReport, BenefitSummary, BoundObservation,
    CostBounds, SeriesBenefit,
)
from omp_work.research.recursive_maintenance import (
    ConsumedPath, FreshResult, MaintenancePolicy, MaintenanceRelease, maintenance_plan,
)
from omp_work.v1.canonical import sha256

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def inputs(primary=100):
    study = RecursiveStudy(
        study_id="study", parent_id=None, depth=0, recipe={"min_metric": 0},
        spec={"score": "higher"}, method_version="method-v2", method_sha256="a" * 64,
        lower_researcher_sha256="b" * 64, objective="search",
        evaluator_component_sha256="c" * 64, evaluator_artifact_sha256="d" * 64,
        protocol_sha256="e" * 64,
        dev=({"task_id": "dev", "content_sha256": "1" * 64},),
        confirmation=({"task_id": "old-confirm", "content_sha256": "2" * 64},),
        dev_sha256=sha256(["1" * 64]), confirmation_sha256=sha256(["2" * 64]),
        upstream_assets=({"identity": "method", "content_sha256": "f" * 64},),
        window_start=START, window_end=END, max_depth=2, max_descendants=3,
        limits=ResourceLimits(60, 1000, Decimal("5")),
    )
    release = MaintenanceRelease(
        release_id="release-v2", bundle_sha256="3" * 64, method_sha256=study.method_sha256,
        evaluator_component_sha256=study.evaluator_component_sha256,
        evaluator_artifact_sha256=study.evaluator_artifact_sha256, schema_read=(1, 2),
    )
    target = release.model_copy(update={"release_id": "stable", "bundle_sha256": "4" * 64, "method_sha256": "5" * 64})
    policy = MaintenancePolicy(
        frozen_at=START - timedelta(minutes=1), window_start=START, window_end=END,
        authorization_decision_id="s01-owner-decision", drift_tolerance=0.1,
        max_regression=20, cost_bounds=CostBounds(Decimal(0), Decimal(0), 0, Decimal(0)), reference_primary=100,
        direction="higher", tested_artifact_sha256=release.bundle_sha256,
        rollback_target=target, persisted_schema_versions=(1, 2),
        base_commit="6" * 40, candidate_commit="7" * 40,
        consumed_paths=(ConsumedPath(identity="method", asset_id="method", path="skills/search/SKILL.md", content_sha256="f" * 64),),
    )
    result = FreshResult(
        observed_at=START + timedelta(minutes=5), release_id=release.release_id,
        artifact_sha256=release.bundle_sha256, task_id="fresh", content_sha256="8" * 64,
        protocol_sha256=study.protocol_sha256,
        evaluator_component_sha256=study.evaluator_component_sha256,
        evaluator_artifact_sha256=study.evaluator_artifact_sha256,
        evaluator_version="eval-v1", primary=primary,
    )
    check = {
        "baseCommit": policy.base_commit, "candidateCommit": policy.candidate_commit,
        "files": [{"assetId": "method", "path": policy.consumed_paths[0].path,
                   "status": "unchanged", "fromSha256": "f" * 64, "toSha256": "f" * 64}],
        "newFiles": [], "overlayConflicts": [], "changed": False,
    }
    return study, release, (result,), None, check, policy


def benefit(args, net=1, *, complete=True, evaluator="eval-v1"):
    study, release, results, _, _, policy = args
    axis = AxisBenefit(Decimal(2), Decimal(1), 0, 0, 0, 0, Decimal(net) if complete else None, True)
    observations = tuple(BoundObservation(
        study.method_version if arm == "candidate" else "stable-method",
        release.release_id if arm == "candidate" else "stable", evaluator, "search",
        results[0].task_id, ("9" if arm == "candidate" else "0") * 64,
        results[0].observed_at, arm, (), True, False, (), results[0].content_sha256,
    ) for arm in ("baseline", "candidate"))
    summary = BenefitSummary((1.0,), (1.0,), BenefitAxes(axis, axis, axis, axis), 0, (), 1, complete, net >= 0, False, observations)
    return BenefitReport((SeriesBenefit(evaluator, study.protocol_sha256, (), summary),), 0, policy.window_start, policy.window_end)


def test_tested_release_drift_requests_fresh_study_with_identical_question_ids():
    args = inputs(115)
    args = (*args[:3], benefit(args), *args[4:])
    before = args[0].model_dump()
    plan = maintenance_plan(*args)
    assert plan.proposal == "reevaluate"
    assert "fresh_downstream_confirmation_required" in plan.reasons
    assert plan.confirmation_credit == 0
    assert plan.rollback is None
    assert "s01-owner-decision" in plan.question.scope and END.isoformat() in plan.question.scope
    assert plan.question.question_id == maintenance_plan(*args).question.question_id
    assert args[0].model_dump() == before
    assert dict(plan.evidence_digests)["observations"]


def test_stale_evaluator_blocks_reuse_and_retirement_credit():
    args = inputs(50)
    release = args[1].model_copy(update={"evaluator_artifact_sha256": "0" * 64})
    plan = maintenance_plan(args[0], release, args[2], benefit(args, -1), args[4], args[5])
    assert plan.proposal == "reevaluate"
    assert "changed_evaluator" in plan.reasons
    assert not plan.qualification_valid and plan.confirmation_credit == 0
    assert plan.rollback is None


def test_declining_benefit_proposes_compatible_stable_method_rollback():
    args = inputs()
    plan = maintenance_plan(*args[:3], benefit(args, -1), *args[4:])
    assert plan.proposal == "retire" and plan.status == "proposed"
    assert plan.reasons == ("lost_benefit",)
    assert plan.rollback["to"] == "stable"
    assert plan.rollback["steps"] == ["stop_admissions", "restore_bundle", "verify_readers"]


def test_incompatible_rollback_refused_instead_of_disabling_method():
    args = inputs(50)
    target = args[5].rollback_target.model_copy(update={"schema_read": (1,)})
    policy = args[5].model_copy(update={"rollback_target": target})
    with pytest.raises(ValueError, match="rollback_incompatible:2"):
        maintenance_plan(*args[:5], policy)


@pytest.mark.parametrize("status,to_hash,reason", [
    ("changed", "0" * 64, "consumed_method_changed"),
    ("missing", None, "consumed_method_missing"),
])
def test_consumed_upstream_change_invalidates_qualification(status, to_hash, reason):
    args = inputs()
    args[4]["files"][0].update(status=status, toSha256=to_hash)
    args[4]["overlayConflicts"] = ["method"]
    plan = maintenance_plan(*args[:3], benefit(args), *args[4:])
    assert plan.proposal == "reevaluate" and not plan.qualification_valid
    assert reason in plan.reasons and "overlay_conflict" in plan.reasons
    assert plan.rollback is None


def test_unrelated_upstream_change_never_activates_candidate_or_invalidates_method():
    args = inputs()
    args[4]["newFiles"] = ["skills/new/SKILL.md"]
    args[4]["files"].append({"assetId": "unrelated", "path": "other", "status": "changed"})
    args[4]["changed"] = True
    plan = maintenance_plan(*args[:3], benefit(args), *args[4:])
    assert plan.proposal == "retain" and plan.qualification_valid
    assert plan.rollback is None and plan.confirmation_credit == 0
    assert "candidate-upstream-inactive" in plan.question.tags


@pytest.mark.parametrize("field,value", [("baseCommit", "0" * 40), ("candidateCommit", "0" * 40)])
def test_upstream_commit_pins_cannot_be_substituted(field, value):
    args = inputs()
    args[4][field] = value
    plan = maintenance_plan(*args)
    assert plan.proposal == "reevaluate" and not plan.qualification_valid
    assert "upstream_commit_mismatch" in plan.reasons


def test_consumed_hash_mismatch_or_missing_row_requests_review():
    args = inputs()
    args[4]["files"][0]["fromSha256"] = "0" * 64
    assert "upstream_dependency_mismatch" in maintenance_plan(*args).reasons
    args[4]["files"] = []
    assert maintenance_plan(*args).proposal == "reevaluate"


def test_reused_task_content_and_out_of_window_observations_hold():
    args = inputs(50)
    result = args[2][0].model_copy(update={"content_sha256": "2" * 64, "observed_at": END})
    plan = maintenance_plan(args[0], args[1], (result,), None, *args[4:])
    assert plan.status == "hold" and plan.proposal == "retain"
    assert "confirmation_reused" in plan.reasons and "outside_observation_window" in plan.reasons
    assert plan.rollback is None


def test_unknown_benefit_and_stale_benefit_series_hold_instead_of_retiring():
    args = inputs()
    for report in (None, benefit(args, -1, complete=False), benefit(args, -1, evaluator="old-eval")):
        plan = maintenance_plan(*args[:3], report, *args[4:])
        assert plan.status == "hold" and plan.proposal == "retain"
        assert "unknown_benefit" in plan.reasons
        assert plan.rollback is None


def test_policy_must_be_frozen_before_observations_and_stay_in_authorized_window():
    args = inputs()
    with pytest.raises(ValidationError, match="policy_must_precede"):
        MaintenancePolicy.model_validate({**args[5].model_dump(), "frozen_at": END})
    policy = args[5].model_copy(update={"window_end": END + timedelta(hours=1)})
    with pytest.raises(ValueError, match="outside_authorized_s01_window"):
        maintenance_plan(*args[:5], policy)


def test_measured_regression_proposes_retirement_even_without_cost_claim():
    args = inputs(79)
    plan = maintenance_plan(*args)
    assert plan.proposal == "retire" and "measured_regression" in plan.reasons
    assert plan.rollback["to"] == "stable"
    assert not plan.qualification_valid


def test_lower_metric_direction_and_regression_bound_are_respected():
    args = inputs(120)
    policy = args[5].model_copy(update={"direction": "lower"})
    plan = maintenance_plan(*args[:5], policy)
    assert plan.proposal == "reevaluate"  # Equality does not exceed regression bound.
    result = args[2][0].model_copy(update={"primary": 121})
    plan = maintenance_plan(args[0], args[1], (result,), None, args[4], policy)
    assert plan.proposal == "retire" and plan.rollback["to"] == "stable"


def test_unknown_observation_or_upstream_holds_without_confirmation_credit():
    args = inputs()
    plan = maintenance_plan(*args[:4], None, args[5])
    assert plan.status == "hold" and not plan.qualification_valid
    assert plan.reasons == ("unknown_upstream",)
    result = args[2][0].model_copy(update={"primary": None})
    plan = maintenance_plan(args[0], args[1], (result,), None, *args[4:])
    assert plan.status == "hold" and plan.reasons == ("unknown_observations",)


def test_duplicate_fresh_confirmation_content_cannot_count_twice():
    args = inputs(50)
    duplicate = args[2][0].model_copy(update={"task_id": "different-label"})
    plan = maintenance_plan(args[0], args[1], (*args[2], duplicate), None, *args[4:])
    assert plan.status == "hold" and plan.rollback is None
    assert "confirmation_reused" in plan.reasons


def test_upstream_status_cannot_hide_changed_consumed_hash():
    args = inputs()
    args[4]["files"][0]["toSha256"] = "0" * 64
    plan = maintenance_plan(*args)
    assert plan.proposal == "reevaluate" and not plan.qualification_valid
    assert "invalid_upstream_check" in plan.reasons


def test_same_method_is_not_a_stable_method_rollback():
    args = inputs(50)
    target = args[5].rollback_target.model_copy(update={"method_sha256": args[1].method_sha256})
    policy = args[5].model_copy(update={"rollback_target": target})
    with pytest.raises(ValueError, match="rollback_requires_stable_method"):
        maintenance_plan(*args[:5], policy)


def test_equivalent_questions_share_id_while_preserving_distinct_evidence_digests():
    args = inputs(115)
    first = maintenance_plan(*args)
    result = args[2][0].model_copy(update={"task_id": "another-fresh", "content_sha256": "9" * 64})
    second = maintenance_plan(args[0], args[1], (result,), None, *args[4:])
    assert first.question.question_id == second.question.question_id
    assert dict(first.evidence_digests)["observations"] != dict(second.evidence_digests)["observations"]


def test_failed_confirmation_is_unknown_benefit_even_with_recorded_costs():
    args = inputs()
    report = benefit(args, -1)
    series = report.series[0]
    summary = replace(series.cumulative, observations=tuple(
        replace(observation, successful=False) for observation in series.cumulative.observations
    ))
    report = replace(report, series=(replace(series, cumulative=summary),))
    plan = maintenance_plan(*args[:3], report, *args[4:])
    assert plan.status == "hold" and plan.proposal == "retain"
    assert "unknown_benefit" in plan.reasons and plan.rollback is None
