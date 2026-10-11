"""Recursive method comparison and pure, unpooled transfer reports."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from omp_work.research.recursive import RecursiveStudy
from omp_work.research.recursive_method import MethodResult
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_execution import ArmExecution, TaskExecution, execute_task
from omp_work.research.recursive_transfer_execution_inputs import PreparedExecution
from omp_work.research.recursive_transfer_freshness import CohortTask, prepare_cohorts
from omp_work.research.recursive_transfer_methods import freeze_pair

Status = Literal["ineligible", "inconclusive", "compared"]


@dataclass(frozen=True)
class ComparisonResult:
    status: Status
    denominator: int
    deltas: tuple[float, ...]
    failures: tuple[str, ...]
    keep_both: bool


@dataclass(frozen=True)
class TaskComparison(ComparisonResult):
    content_sha256: str | None
    task_id: str | None
    executions: tuple[TaskExecution, ...]


@dataclass(frozen=True)
class DomainComparison(ComparisonResult):
    phase: Literal["development", "confirmation"]
    domain: str
    protocol_sha256: str
    metric: str
    direction: Literal["higher", "lower"]
    tasks: tuple[TaskComparison, ...]


@dataclass(frozen=True)
class ComparisonReport(ComparisonResult):
    domains: tuple[DomainComparison, ...]
    transfer: tuple[DomainComparison, ...]
    history: ConsumptionHistory | None = None


def _qualified(execution: TaskExecution) -> bool:
    return (
        execution.ok and not execution.failures
        and execution.delta is not None and math.isfinite(execution.delta)
    )


def _summary(executions: Sequence[TaskExecution]) -> ComparisonResult:
    deltas = tuple(row.delta for row in executions if _qualified(row))
    denominator = sum(row.denominator for row in executions)
    failures = tuple(dict.fromkeys(reason for row in executions for reason in row.failures))
    status: Status = "compared" if deltas else "inconclusive" if denominator > 0 or failures else "ineligible"
    return ComparisonResult(
        status, denominator, deltas, failures,
        not executions or len(deltas) != len(executions) or any(delta <= 0 for delta in deltas),
    )


def assemble_results(executions: Sequence[TaskExecution]) -> ComparisonReport:
    """Retain all evidence, grouping only compatible measurements and identities."""
    groups: dict[tuple[str, str, str, str, str], list[TaskExecution]] = defaultdict(list)
    development_domains = {row.domain for row in executions if row.phase == "development"}
    for row in executions:
        groups[(row.phase, row.domain, row.protocol_sha256, row.metric, row.direction)].append(row)
    domains: list[DomainComparison] = []
    for rows in groups.values():
        identities: dict[tuple[str | None, str | None], list[TaskExecution]] = defaultdict(list)
        for row in rows:
            identities[(row.content_sha256, row.task_id)].append(row)
        tasks = tuple(
            TaskComparison(**vars(_summary(task_rows)), content_sha256=content, task_id=task_id,
                           executions=tuple(task_rows))
            for (content, task_id), task_rows in identities.items()
        )
        first = rows[0]
        domains.append(DomainComparison(
            **vars(_summary(rows)), phase=first.phase, domain=first.domain,
            protocol_sha256=first.protocol_sha256, metric=first.metric,
            direction=first.direction, tasks=tasks,
        ))
    transfer = tuple(
        group for group in domains
        if group.phase == "confirmation" and group.domain not in development_domains
        and group.status == "compared" and not group.keep_both
    )
    return ComparisonReport(**vars(_summary(executions)), domains=tuple(domains), transfer=transfer)


def compare_methods(
    study: RecursiveStudy,
    baseline_method: MethodResult,
    candidate_method: MethodResult,
    cohorts: Sequence[CohortTask],
    native_evidence: NativeEvidence,
) -> ComparisonReport:
    """Execute eligible cohorts using the caller's existing consumption history."""
    baseline, candidate = freeze_pair(study, baseline_method, candidate_method)
    prepared = prepare_cohorts(cohorts, native_evidence, (baseline.study, candidate.study))
    executions: list[TaskExecution] = []
    for cohort, task in zip(cohorts, prepared):
        if task.eligible:
            executions.append(execute_task(
                baseline, candidate, PreparedExecution.from_task(task, cohort), native_evidence,
            ))
        else:
            arms = tuple(
                ArmExecution(None, 0, (), None, (), frozen.result.state,
                             frozen.result.source_sha256, frozen.result.artifact_sha256)
                for frozen in (baseline, candidate)
            )
            executions.append(TaskExecution(
                cohort.phase, cohort.domain, cohort.protocol_sha256, cohort.metric, cohort.direction,
                task.task_id, task.content_sha256, "ineligible", None,
                {"baseline": None, "candidate": None}, 0, task.reasons, *arms, False,
            ))
    return replace(assemble_results(executions), history=native_evidence.history)
