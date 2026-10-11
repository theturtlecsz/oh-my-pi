"""Preparation and trust-boundary input validation for recursive transfer execution.

OMP-319-s03-s05-s01:
- PreparedExecution subclasses PreparedTask, retaining CohortTask controller settings
  (phase, domain, metric, direction, protocol_sha256, seed, width, budget) without defaults.
- prepare_execution_cohorts wraps prepare_cohorts, returning PreparedExecution instances.
- load_payload validates custody datasets at the trust boundary, checking task_id and
  canonical content hash identities, validating the step tree and opened references before
  set conversion. Payload-level settings and tree metric fields carry no authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_transfer_evidence import NativeEvidence
from omp_work.research.recursive_transfer_freshness import (
    CohortTask,
    PreparedTask,
    _dataset,
    prepare_cohorts,
)
from omp_work.v1.canonical import canonical_json, sha256

__all__ = [
    "ExecutionPayload",
    "PayloadValidationError",
    "PreparedExecution",
    "load_payload",
    "prepare_execution_cohorts",
]


class PayloadValidationError(ValueError):
    """Raised when an execution payload fails validation at the trust boundary."""


@dataclass(frozen=True, eq=False)
class ExecutionPayload(Mapping):
    """Validated execution payload with step tree and opened step IDs.

    Payload-level settings and tree metric fields carry no authority;
    only the validated tree structure and opened references are exposed.
    """

    steps: dict[str, Any]
    opened: frozenset[str]

    def __getitem__(self, key: str) -> Any:
        if key == "steps":
            return self.steps
        if key == "opened":
            return self.opened
        raise KeyError(key)

    def __iter__(self):
        yield "steps"
        yield "opened"

    def __len__(self) -> int:
        return 2

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ExecutionPayload):
            return self.steps == other.steps and self.opened == other.opened
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False


@dataclass(frozen=True)
class PreparedExecution(PreparedTask):
    """Execution-ready prepared task retaining controller/cohort settings.

    Subclasses PreparedTask for full reserve_task and evidence compatibility,
    while retaining phase, domain, metric, direction, protocol_sha256, seed,
    width, and budget without defaults.
    """

    phase: Literal["development", "confirmation"]
    domain: str
    metric: str
    direction: Literal["higher", "lower"]
    protocol_sha256: str
    seed: int
    width: int
    budget: int

    @classmethod
    def from_task(cls, prep: PreparedTask, cohort: CohortTask) -> PreparedExecution:
        return cls(
            eligible=prep.eligible,
            reasons=prep.reasons,
            dataset_id=prep.dataset_id,
            task_id=prep.task_id,
            content_sha256=prep.content_sha256,
            steps=prep.steps,
            phase=cohort.phase,
            domain=cohort.domain,
            metric=cohort.metric,
            direction=cohort.direction,
            protocol_sha256=cohort.protocol_sha256,
            seed=cohort.seed,
            width=cohort.width,
            budget=cohort.budget,
        )


def prepare_execution_cohorts(
    cohorts: Sequence[CohortTask],
    native: NativeEvidence,
    studies: Sequence[RecursiveStudy] = (),
) -> tuple[PreparedExecution, ...]:
    """Wrap prepare_cohorts, retaining CohortTask settings on PreparedExecution.

    Retains phase, domain, metric, direction, protocol_sha256, seed, width,
    and budget without defaults.
    """
    prepared_tasks = prepare_cohorts(cohorts, native, studies)
    return tuple(
        PreparedExecution.from_task(prep, cohort)
        for cohort, prep in zip(cohorts, prepared_tasks)
    )


def load_payload(
    prepared: PreparedTask,
    native: NativeEvidence,
) -> ExecutionPayload:
    """Load and validate execution payload from custody at the trust boundary.

    Reuses freshness._dataset, checks task/content identity, and validates the
    tree plus opened step references.

    Requires string opened IDs and valid tree references before set conversion.
    Payload-level settings and tree metric fields carry no authority.
    """
    if not isinstance(prepared, PreparedTask):
        raise PayloadValidationError(f"expected PreparedTask, got {type(prepared).__name__}")

    try:
        dataset = _dataset(native, prepared.dataset_id)
    except PayloadValidationError:
        raise
    except (ValueError, OSError, RuntimeError) as error:
        raise PayloadValidationError(f"failed to read dataset from custody: {error}") from error

    if prepared.task_id is None:
        raise PayloadValidationError("prepared task_id is None")
    if dataset.task_id != prepared.task_id:
        raise PayloadValidationError(
            f"task_id mismatch: expected {prepared.task_id!r}, got {dataset.task_id!r}"
        )

    if prepared.content_sha256 is None:
        raise PayloadValidationError("prepared content_sha256 is None")
    computed_content_sha256 = sha256(dataset.payload)
    if computed_content_sha256 != prepared.content_sha256:
        raise PayloadValidationError(
            f"content_sha256 mismatch: expected {prepared.content_sha256!r}, got {computed_content_sha256!r}"
        )

    raw_payload = dataset.payload
    if not isinstance(raw_payload, Mapping):
        raise PayloadValidationError(
            f"payload must be a JSON object (mapping), got {type(raw_payload).__name__}"
        )

    if "steps" not in raw_payload:
        raise PayloadValidationError("payload missing required 'steps' field")

    steps_raw = raw_payload["steps"]
    if not isinstance(steps_raw, Mapping):
        raise PayloadValidationError(
            f"steps must be a mapping, got {type(steps_raw).__name__}"
        )

    if not steps_raw:
        raise PayloadValidationError("steps mapping cannot be empty")

    for step_id, node in steps_raw.items():
        if not isinstance(step_id, str) or not step_id:
            raise PayloadValidationError(
                f"step_id must be a non-empty string, got {step_id!r}"
            )
        if not isinstance(node, Mapping):
            raise PayloadValidationError(
                f"step node {step_id!r} must be a mapping, got {type(node).__name__}"
            )

        parent_id = node.get("parent_id")
        if parent_id is not None:
            if not isinstance(parent_id, str):
                raise PayloadValidationError(
                    f"step {step_id!r} parent_id must be string or None, got {type(parent_id).__name__}"
                )
            if parent_id == step_id:
                raise PayloadValidationError(
                    f"step {step_id!r} cannot have itself as parent_id"
                )
            if parent_id not in steps_raw:
                raise PayloadValidationError(
                    f"step {step_id!r} parent_id {parent_id!r} not found in steps tree"
                )

        children = node.get("children")
        if children is not None:
            if isinstance(children, (str, bytes)) or not isinstance(children, (list, tuple, set, frozenset)):
                raise PayloadValidationError(
                    f"step {step_id!r} children must be a collection of string IDs, got {type(children).__name__}"
                )
            for child_idx, child in enumerate(children):
                if not isinstance(child, str):
                    raise PayloadValidationError(
                        f"step {step_id!r} child at index {child_idx} must be a string, got {type(child).__name__}"
                    )
                if not child:
                    raise PayloadValidationError(
                        f"step {step_id!r} child at index {child_idx} cannot be empty string"
                    )
                if child not in steps_raw:
                    raise PayloadValidationError(
                        f"step {step_id!r} child {child!r} not found in steps tree"
                    )

    opened_raw = raw_payload.get("opened", ())
    if opened_raw is None:
        opened_raw = ()
    elif isinstance(opened_raw, (str, bytes)) or not isinstance(opened_raw, (list, tuple, set, frozenset)):
        raise PayloadValidationError(
            f"opened must be a collection of step IDs, got {type(opened_raw).__name__}"
        )

    opened_ids: list[str] = []
    for idx, opened_id in enumerate(opened_raw):
        if not isinstance(opened_id, str):
            raise PayloadValidationError(
                f"opened ID at index {idx} must be a string, got {type(opened_id).__name__}"
            )
        if not opened_id:
            raise PayloadValidationError(
                f"opened ID at index {idx} cannot be empty string"
            )
        if opened_id not in steps_raw:
            raise PayloadValidationError(
                f"opened step_id {opened_id!r} not found in steps tree"
            )
        opened_ids.append(opened_id)

    # Set conversion strictly after validation
    opened_set = frozenset(opened_ids)

    clean_steps: dict[str, Any] = json.loads(canonical_json(dict(steps_raw)))

    return ExecutionPayload(
        steps=clean_steps,
        opened=opened_set,
    )
