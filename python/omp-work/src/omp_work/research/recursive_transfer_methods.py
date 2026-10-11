"""Frozen method-pair admission without generation or live execution gates."""

from dataclasses import dataclass

from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_method import MethodResult, validate_frozen_method


@dataclass(frozen=True)
class FrozenMethod:
    result: MethodResult
    study: RecursiveStudy

    def __post_init__(self) -> None:
        validate_frozen_method(self.result, self.study)


def freeze_pair(
    study: RecursiveStudy, baseline_result: MethodResult, candidate_result: MethodResult,
) -> tuple[FrozenMethod, FrozenMethod]:
    """Bind baseline to its admitted recipe and candidate to the supplied study."""
    admitted = [s for s in baseline_result.state.studies if s.recipe == baseline_result.recipe_bytes]
    if len(admitted) != 1:
        raise ValueError("baseline_study_not_unique")
    baseline = FrozenMethod(baseline_result, admitted[0])
    candidate = FrozenMethod(candidate_result, study)
    for field, reason in (
        ("method_sha256", "method_changed"),
        ("lower_researcher_sha256", "lower_researcher_changed"),
        ("evaluator_component_sha256", "evaluator_component_changed"),
        ("evaluator_artifact_sha256", "evaluator_artifact_changed"),
        ("protocol_sha256", "protocol_changed"),
    ):
        if getattr(baseline.study, field) != getattr(candidate.study, field):
            raise ValueError(reason)
    return baseline, candidate
