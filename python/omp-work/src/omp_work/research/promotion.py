"""R14 canary observation, schema-aware rollback, and evaluator-change workflow.

Pure, database-free decisions for promoting an evaluated harness or evaluator
release. A canary verdict compares fresh observations against their baselines
by the metric's declared direction. A rollback plan is refused when the target
cannot read a persisted schema, because repointing an executable directory is
not a database rollback. A change is classified as harness-only, evaluator-only,
or mixed, and a mixed change is refused: a bundled harness-plus-evaluator change
must be evaluated as the declared interaction, so it is never promoted here.

The module holds no I/O and imports nothing from the release or receipt layers.
Manifests and confirmations are plain mappings, so the caller owns their
persistence and trust.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "CanaryObservation",
    "ChangeKind",
    "EvaluatorChange",
    "Verdict",
    "canary_verdict",
    "check_promotion",
    "classify_change",
    "confirmation_valid",
    "plan_rollback",
    "requires_fresh_confirmation",
]

Direction = Literal["higher", "lower"]
Verdict = Literal["hold", "rollback", "promote"]
ChangeKind = Literal["no_change", "harness", "evaluator", "mixed"]

_ROLLBACK_STEPS = ("stop_admissions", "restore_bundle", "verify_readers")
_ROLLBACK_NOTE = "executable repoint alone is not a database rollback"


@dataclass(frozen=True)
class CanaryObservation:
    """One fresh canary reading against the baseline it replaces.

    ``direction`` is the metric's declared direction: a ``higher`` metric
    regresses when it falls below its baseline, a ``lower`` metric regresses
    when it rises above it.
    """

    release_id: str
    task_id: str
    metric: float
    baseline_metric: float
    direction: Direction

    def __post_init__(self) -> None:
        if self.direction not in ("higher", "lower"):
            raise ValueError(f"unknown_direction:{self.direction}")

    def regressed(self) -> bool:
        if self.direction == "higher":
            return self.metric < self.baseline_metric
        return self.metric > self.baseline_metric


def canary_verdict(
    observations: Sequence[CanaryObservation], *, max_regressions: int = 0
) -> Verdict:
    """``hold`` with no observations, ``rollback`` past the regression cap, else ``promote``."""
    if not observations:
        return "hold"
    regressions = sum(1 for observation in observations if observation.regressed())
    if regressions > max_regressions:
        return "rollback"
    return "promote"


def plan_rollback(
    current: Mapping[str, Any],
    target: Mapping[str, Any],
    persisted_schema_versions: Sequence[int],
) -> dict[str, Any]:
    """Plan a rollback to ``target``, refusing an incompatible target schema.

    A target that is the current release raises ``same_release``. A persisted
    schema version the target cannot read raises
    ``rollback_incompatible:<version>`` naming the lowest such version. The
    accepted plan restores the target bundle; the caller must still migrate the
    database, because the plan alone does not do that.
    """
    if str(target["release_id"]) == str(current["release_id"]):
        raise ValueError("same_release")
    readable = {int(version) for version in target["schema_read"]}
    missing = sorted(
        int(version) for version in persisted_schema_versions if int(version) not in readable
    )
    if missing:
        raise ValueError(f"rollback_incompatible:{missing[0]}")
    return {
        "from": current["release_id"],
        "to": target["release_id"],
        "restores_bundle": target["bundle_sha256"],
        "steps": list(_ROLLBACK_STEPS),
        "note": _ROLLBACK_NOTE,
    }


@dataclass(frozen=True)
class EvaluatorChange:
    """A separately versioned evaluator change, distinct from a harness release."""

    version: int
    old_component_sha256: str
    old_artifact_sha256: str
    new_component_sha256: str
    new_artifact_sha256: str
    reason: str

    def __post_init__(self) -> None:
        if (
            self.old_component_sha256 == self.new_component_sha256
            and self.old_artifact_sha256 == self.new_artifact_sha256
        ):
            raise ValueError("no_change")
        if not self.reason:
            raise ValueError("reason_required")


def requires_fresh_confirmation(
    change: EvaluatorChange, confirmation: Mapping[str, Any]
) -> bool:
    """True when ``confirmation`` was issued before ``change``.

    A confirmation binds the evaluator digests it was issued for, so any change
    that moves either digest invalidates it.
    """
    return (
        str(confirmation["evaluator_component_sha256"]) != change.new_component_sha256
        or str(confirmation["evaluator_artifact_sha256"]) != change.new_artifact_sha256
    )


def _evaluator_digests(manifest: Mapping[str, Any]) -> tuple[str, str]:
    evaluator = manifest["evaluator"]
    return str(evaluator["component_sha256"]), str(evaluator["artifact_sha256"])


def classify_change(
    old_manifest: Mapping[str, Any], new_manifest: Mapping[str, Any]
) -> ChangeKind:
    """Classify the divergence between two release manifests.

    ``no_change`` when the bundle and both evaluator digests match, ``harness``
    when only the bundle differs, ``evaluator`` when only an evaluator digest
    differs, and ``mixed`` when both differ.
    """
    bundle_changed = str(old_manifest["bundle_sha256"]) != str(new_manifest["bundle_sha256"])
    evaluator_changed = _evaluator_digests(old_manifest) != _evaluator_digests(new_manifest)
    if bundle_changed and evaluator_changed:
        return "mixed"
    if bundle_changed:
        return "harness"
    if evaluator_changed:
        return "evaluator"
    return "no_change"


def check_promotion(
    old_manifest: Mapping[str, Any], new_manifest: Mapping[str, Any]
) -> ChangeKind:
    """Return the change kind, refusing a mixed harness-plus-evaluator change."""
    kind = classify_change(old_manifest, new_manifest)
    if kind == "mixed":
        raise ValueError("mixed_change")
    return kind


def confirmation_valid(
    confirmation: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    """Reasons a confirmation no longer matches ``manifest`` (R06 vocabulary)."""
    if (
        str(confirmation["evaluator_component_sha256"]) != _evaluator_digests(manifest)[0]
        or str(confirmation["evaluator_artifact_sha256"]) != _evaluator_digests(manifest)[1]
    ):
        return ["changed_evaluator"]
    return []
