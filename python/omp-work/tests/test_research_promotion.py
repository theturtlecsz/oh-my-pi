"""R14 canary, rollback, and evaluator-change decisions (OMP-317).

These tests defend the observable contract a promotion consumer gates on: an
observation only counts as a regression in the declared direction, an empty
canary holds, a rollback is refused when the target cannot read a persisted
schema, and a mixed harness-plus-evaluator change is refused while an
evaluator-only change invalidates earlier confirmations. Every test names the
failure mode a consumer would see.
"""

from __future__ import annotations

import pytest

from omp_work.research.promotion import (
    CanaryObservation,
    EvaluatorChange,
    canary_verdict,
    check_promotion,
    classify_change,
    confirmation_valid,
    plan_rollback,
    requires_fresh_confirmation,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64


def manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "release_id": "release-1",
        "bundle_sha256": A,
        "evaluator": {"component_sha256": B, "artifact_sha256": C},
        "schema_read": [1, 2],
        "schema_write": [1, 2],
    }
    payload.update(overrides)
    return payload


def observation(**overrides: object) -> CanaryObservation:
    fields: dict[str, object] = {
        "release_id": "release-2",
        "task_id": "task-1",
        "metric": 0.9,
        "baseline_metric": 0.8,
        "direction": "higher",
    }
    fields.update(overrides)
    return CanaryObservation(**fields)  # type: ignore[arg-type]


def change(**overrides: object) -> EvaluatorChange:
    fields: dict[str, object] = {
        "version": 2,
        "old_component_sha256": B,
        "old_artifact_sha256": C,
        "new_component_sha256": D,
        "new_artifact_sha256": E,
        "reason": "tighten the evaluator sandbox",
    }
    fields.update(overrides)
    return EvaluatorChange(**fields)  # type: ignore[arg-type]


def confirmation(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "evaluator_component_sha256": D,
        "evaluator_artifact_sha256": E,
    }
    payload.update(overrides)
    return payload


def test_no_observations_hold() -> None:
    assert canary_verdict([]) == "hold"


def test_higher_metric_regression_rolls_back_at_zero_cap() -> None:
    regressed = observation(metric=0.7, baseline_metric=0.8, direction="higher")
    assert canary_verdict([regressed], max_regressions=0) == "rollback"


def test_lower_metric_regression_rolls_back_inverted() -> None:
    regressed = observation(metric=0.9, baseline_metric=0.8, direction="lower")
    assert canary_verdict([regressed], max_regressions=0) == "rollback"


def test_metric_improvement_in_declared_direction_promotes() -> None:
    higher = observation(metric=0.9, baseline_metric=0.8, direction="higher")
    lower = observation(metric=0.7, baseline_metric=0.8, direction="lower")
    assert canary_verdict([higher, lower]) == "promote"


def test_regressions_at_the_cap_still_promote() -> None:
    first = observation(metric=0.7, baseline_metric=0.8, direction="higher")
    second = observation(metric=0.6, baseline_metric=0.8, direction="higher")
    assert canary_verdict([first, second], max_regressions=2) == "promote"
    assert canary_verdict([first, second], max_regressions=1) == "rollback"


def test_unknown_direction_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown_direction"):
        CanaryObservation("r", "t", 0.9, 0.8, "sideways")  # type: ignore[arg-type]


def test_plan_rollback_refuses_the_current_release() -> None:
    current = manifest(release_id="release-1")
    with pytest.raises(ValueError, match="same_release"):
        plan_rollback(current, manifest(release_id="release-1"), [])


def test_plan_rollback_names_the_lowest_unreadable_schema() -> None:
    current = manifest(release_id="release-2")
    target = manifest(release_id="release-1", schema_read=[1, 2])
    with pytest.raises(ValueError, match="rollback_incompatible:3"):
        plan_rollback(current, target, [1, 2, 3])


def test_plan_rollback_returns_the_target_bundle_and_steps() -> None:
    current = manifest(release_id="release-2", bundle_sha256=D)
    target = manifest(release_id="release-1", bundle_sha256=F, schema_read=[1, 2])
    plan = plan_rollback(current, target, [1, 2])
    assert plan["from"] == "release-2"
    assert plan["to"] == "release-1"
    assert plan["restores_bundle"] == F
    assert plan["steps"] == ["stop_admissions", "restore_bundle", "verify_readers"]
    assert plan["note"] == "executable repoint alone is not a database rollback"


def test_evaluator_change_refuses_unchanged_digests() -> None:
    with pytest.raises(ValueError, match="no_change"):
        change(new_component_sha256=B, new_artifact_sha256=C)


def test_evaluator_change_refuses_an_empty_reason() -> None:
    with pytest.raises(ValueError, match="reason_required"):
        change(reason="")


def test_a_changed_evaluator_requires_a_fresh_confirmation() -> None:
    prior = confirmation(evaluator_component_sha256=B, evaluator_artifact_sha256=C)
    assert requires_fresh_confirmation(change(), prior) is True
    assert requires_fresh_confirmation(change(), confirmation()) is False


def test_classify_change_covers_the_four_combinations() -> None:
    base = manifest()
    assert classify_change(base, manifest()) == "no_change"
    assert (
        classify_change(base, manifest(bundle_sha256=D, schema_write=[1, 2, 3])) == "harness"
    )
    assert (
        classify_change(
            base,
            manifest(evaluator={"component_sha256": D, "artifact_sha256": C}),
        )
        == "evaluator"
    )
    assert (
        classify_change(
            base,
            manifest(bundle_sha256=D, evaluator={"component_sha256": B, "artifact_sha256": E}),
        )
        == "mixed"
    )


def test_check_promotion_refuses_a_mixed_change() -> None:
    base = manifest()
    mixed = manifest(
        bundle_sha256=D, evaluator={"component_sha256": E, "artifact_sha256": F}
    )
    with pytest.raises(ValueError, match="mixed_change"):
        check_promotion(base, mixed)
    assert check_promotion(base, manifest(bundle_sha256=D)) == "harness"


def test_confirmation_valid_reports_changed_evaluator() -> None:
    base = manifest()
    stale = confirmation(evaluator_component_sha256=D, evaluator_artifact_sha256=E)
    assert confirmation_valid(stale, base) == ["changed_evaluator"]
    fresh = confirmation(evaluator_component_sha256=B, evaluator_artifact_sha256=C)
    assert confirmation_valid(fresh, base) == []
