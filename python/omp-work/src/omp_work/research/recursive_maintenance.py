"""Bounded maintenance proposals, never owner decisions or release mutations.

Freeze MaintenancePolicy before collecting observations. Upstream input is the
ECC adapter's local-checkout JSON; its candidate is evidence, never activation.
A requested fresh study still needs s01 live admission authority and disjoint
cohorts. This module neither issues grants nor schedules work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from omp_work.research.accounting import run_canary
from omp_work.research.program import QuestionItem
from omp_work.research.promotion import confirmation_valid, plan_rollback
from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_benefit import BenefitReport, CostBounds
from omp_work.v1.canonical import sha256, validate_execution_path
from omp_work.v1.models import StrictModel, hex64


class MaintenanceRelease(StrictModel):
    release_id: str = Field(min_length=1)
    bundle_sha256: hex64
    method_sha256: hex64
    evaluator_component_sha256: hex64
    evaluator_artifact_sha256: hex64
    schema_read: tuple[int, ...] = Field(min_length=1)

    def manifest(self) -> dict[str, object]:
        return {
            **self.model_dump(),
            "evaluator": {
                "component_sha256": self.evaluator_component_sha256,
                "artifact_sha256": self.evaluator_artifact_sha256,
            },
        }


class ConsumedPath(StrictModel):
    identity: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    path: str
    content_sha256: hex64

    @model_validator(mode="after")
    def canonical_path(self) -> ConsumedPath:
        validate_execution_path(self.path)
        return self


class MaintenancePolicy(StrictModel):
    """Preregistered limits; authorization is a reference, never a new grant."""

    frozen_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    authorization_decision_id: str = Field(min_length=1)
    drift_tolerance: float = Field(ge=0, le=1, allow_inf_nan=False)
    max_regression: float = Field(ge=0, allow_inf_nan=False)
    cost_bounds: CostBounds
    reference_primary: float = Field(allow_inf_nan=False)
    direction: Literal["higher", "lower"]
    tested_artifact_sha256: hex64
    rollback_target: MaintenanceRelease
    persisted_schema_versions: tuple[int, ...]
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    candidate_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    consumed_paths: tuple[ConsumedPath, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def preregistered(self) -> MaintenancePolicy:
        if not self.frozen_at <= self.window_start < self.window_end:
            raise ValueError("policy_must_precede_observation_window")
        keys = [(item.asset_id, item.path) for item in self.consumed_paths]
        if len(set(keys)) != len(keys) or len({item.identity for item in self.consumed_paths}) != len(keys):
            raise ValueError("duplicate_consumed_path")
        return self


class FreshResult(StrictModel):
    observed_at: AwareDatetime
    release_id: str = Field(min_length=1)
    artifact_sha256: hex64
    task_id: str = Field(min_length=1)
    content_sha256: hex64
    protocol_sha256: hex64
    evaluator_component_sha256: hex64
    evaluator_artifact_sha256: hex64
    evaluator_version: str = Field(min_length=1)
    primary: float | None = Field(default=None, allow_inf_nan=False)


@dataclass(frozen=True)
class MaintenancePlan:
    proposal: Literal["retain", "reevaluate", "retire"]
    status: Literal["hold", "proposed"]
    reasons: tuple[str, ...]
    evidence_digests: tuple[tuple[str, str], ...]
    question: QuestionItem
    rollback: Mapping[str, object] | None
    qualification_valid: bool
    # Old confirmations cannot satisfy the requested downstream study.
    confirmation_credit: int = 0


def _upstream_reasons(
    check: Mapping[str, object] | None, policy: MaintenancePolicy,
) -> tuple[str, ...]:
    if check is None:
        return ("unknown_upstream",)
    if check.get("baseCommit") != policy.base_commit or check.get("candidateCommit") != policy.candidate_commit:
        return ("upstream_commit_mismatch",)
    files = check.get("files")
    conflicts = check.get("overlayConflicts")
    if not isinstance(files, list) or not isinstance(conflicts, list):
        return ("invalid_upstream_check",)
    reasons: list[str] = []
    for pin in policy.consumed_paths:
        rows = [row for row in files if isinstance(row, Mapping)
                and row.get("assetId") == pin.asset_id and row.get("path") == pin.path]
        if len(rows) != 1 or rows[0].get("fromSha256") != pin.content_sha256:
            reasons.append("upstream_dependency_mismatch")
            continue
        row = rows[0]
        status, digest = row.get("status"), row.get("toSha256")
        if status == "unchanged" and digest == pin.content_sha256:
            pass
        elif status == "missing" and digest is None:
            reasons.append("consumed_method_missing")
        elif status == "changed" and isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) and digest != pin.content_sha256:
            reasons.append("consumed_method_changed")
        else:
            reasons.append("invalid_upstream_check")
        if pin.asset_id in conflicts:
            reasons.append("overlay_conflict")
    # newFiles and unrelated assets cannot qualify or activate a method.
    return tuple(sorted(set(reasons)))


def maintenance_plan(
    study: RecursiveStudy,
    release: MaintenanceRelease,
    fresh_results: Sequence[FreshResult],
    benefit_report: BenefitReport | None,
    upstream_check: Mapping[str, object] | None,
    policy: MaintenancePolicy,
) -> MaintenancePlan:
    """Return Chris's bounded question and evidence; apply no choice.

    Retirement requires measured, fresh, release-bound evidence and a compatible
    stable-method rollback. An incompatible rollback is refused by the shared
    planner, never converted into disabling the current method.
    """
    if not study.window_start <= policy.window_start < policy.window_end <= study.window_end:
        raise ValueError("outside_authorized_s01_window")
    assets = {asset.identity: asset.content_sha256 for asset in study.upstream_assets}
    if {pin.identity for pin in policy.consumed_paths} != set(assets) or any(
        assets[pin.identity] != pin.content_sha256 for pin in policy.consumed_paths
    ):
        raise ValueError("unpinned_study_dependency")
    if release.method_sha256 != study.method_sha256:
        raise ValueError("release_method_mismatch")
    if policy.rollback_target.method_sha256 == release.method_sha256:
        raise ValueError("rollback_requires_stable_method")

    results = tuple(fresh_results)
    upstream_evidence = dict(upstream_check) if upstream_check is not None else None
    if upstream_evidence is not None:
        for key in ("files", "newFiles", "overlayConflicts"):
            if isinstance(upstream_evidence.get(key), list):
                upstream_evidence[key] = sorted(upstream_evidence[key], key=sha256)
    digests = tuple(sorted({
        "study": sha256(study.model_dump(mode="json")),
        "release": sha256(release.model_dump(mode="json")),
        "policy": sha256(policy.model_dump(mode="json")),
        "observations": sha256(sorted((item.model_dump(mode="json") for item in results), key=sha256)),
        "benefit": sha256(asdict(benefit_report)) if benefit_report else sha256(None),
        "upstream": sha256(upstream_evidence),
    }.items()))
    reasons = list(_upstream_reasons(upstream_check, policy))
    invalid_upstream = bool(reasons)
    old = study.dev + study.confirmation
    ids = {item.task_id for item in old}
    contents = {item.content_sha256 for item in old}
    for result in results:
        if not policy.window_start <= result.observed_at < policy.window_end:
            reasons.append("outside_observation_window")
        if result.task_id in ids or result.content_sha256 in contents:
            reasons.append("confirmation_reused")
        ids.add(result.task_id)
        contents.add(result.content_sha256)
        if result.release_id != release.release_id or result.artifact_sha256 != release.bundle_sha256:
            reasons.append("release_identity_mismatch")
        if result.protocol_sha256 != study.protocol_sha256:
            reasons.append("changed_protocol")
        reasons.extend(confirmation_valid(result.model_dump(), release.manifest()))
    reasons.extend(confirmation_valid(study.model_dump(), release.manifest()))
    if release.bundle_sha256 != policy.tested_artifact_sha256:
        reasons.append("release_identity_mismatch")

    invalid_evidence = bool(reasons)
    proposal: Literal["retain", "reevaluate", "retire"] = "retain"
    status: Literal["hold", "proposed"] = "hold"
    rollback = None
    if reasons:
        if (invalid_upstream and reasons != ["unknown_upstream"]) or "changed_evaluator" in reasons:
            proposal = "reevaluate"
    elif not results or any(item.primary is None for item in results):
        reasons.append("unknown_observations")
    else:
        drift = False
        regression = False
        for result in results:
            if result.primary is None:
                raise AssertionError
            drift |= run_canary(policy.reference_primary, result.primary, policy.drift_tolerance,
                                policy.tested_artifact_sha256, release.bundle_sha256)["status"] == "drift_detected"
            loss = policy.reference_primary - result.primary
            if policy.direction == "lower":
                loss = -loss
            regression |= loss > policy.max_regression
        summary = benefit_report.cumulative if benefit_report else None
        benefit_known = bool(
            summary and summary.complete and not summary.self_scored and summary.observations
            and benefit_report and benefit_report.window_start == policy.window_start
            and benefit_report.window_end == policy.window_end
            and len(benefit_report.series) == 1
            and benefit_report.series[0].protocol_sha256 == study.protocol_sha256
            and all(
                observation.successful and not observation.self_scored
                and observation.timestamp >= policy.window_start and observation.timestamp < policy.window_end
                and observation.evaluator_version == benefit_report.series[0].evaluator_version
                and any(item.evaluator_version == observation.evaluator_version
                        and item.task_id == observation.task_id
                        and item.content_sha256 == observation.content_sha256 for item in results)
                and (observation.arm != "candidate" or (
                    observation.release_version == release.release_id
                    and observation.method_version == study.method_version
                ))
                for observation in summary.observations
            )
            and all(summary.axes.get(axis).net is not None for axis in ("usd", "compute_seconds", "tokens", "owner_minutes"))
        )
        lost = bool(benefit_known and summary and any(
            summary.axes.get(axis).net < policy.cost_bounds.get(axis)
            for axis in ("usd", "compute_seconds", "tokens", "owner_minutes")
        ))
        if regression or lost:
            proposal, status = "retire", "proposed"
            reasons.extend(reason for reason, measured in (("measured_regression", regression), ("lost_benefit", lost)) if measured)
            rollback = plan_rollback(release.manifest(), policy.rollback_target.manifest(), policy.persisted_schema_versions)
        elif drift:
            proposal, status = "reevaluate", "proposed"
            reasons.append("fresh_downstream_confirmation_required")
        elif not benefit_known:
            reasons.append("unknown_benefit")
        else:
            status = "proposed"
    reasons_tuple = tuple(sorted(set(reasons)))
    if proposal == "reevaluate":
        status = "proposed"
    scope = (
        f"{study.study_id}; [{policy.window_start.isoformat()}, {policy.window_end.isoformat()}); "
        f"decision {policy.authorization_decision_id}; caps {study.limits}; "
        f"max_depth {study.max_depth}; max_descendants {study.max_descendants}"
    )
    question = QuestionItem(
        sha256({
            "proposal": proposal, "method": study.method_sha256,
            "study": study.equivalence_sha256, "release": dict(digests)["release"],
            "policy": dict(digests)["policy"], "reasons": reasons_tuple,
        }),
        f"{proposal.capitalize()} method {study.method_version}? Fresh downstream confirmation uses disjoint tasks; Chris decides.",
        "recursive-maintenance", 1, scope, policy.window_start.isoformat(),
        ("proposal-only", "fresh-confirmation", "candidate-upstream-inactive"),
    )
    return MaintenancePlan(proposal, status, reasons_tuple, digests, question, rollback,
                           not invalid_evidence and status == "proposed" and proposal == "retain")
