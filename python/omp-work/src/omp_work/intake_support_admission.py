"""Grant check and O8 usage for the intake support judge (OMP-604-s05).

``resolve_admission`` answers, from the caller's own open transaction, whether
the project or mission holds budget for one research-route judge call, and
returns the routed model, the remaining headroom under the tightest present
budget, and a stable grant id binding the workspace, mission, route rule and
route model. Refusal is ``IntakeAdmissionError`` with code ``model_grant_required``
(no budget, unknown mission, or a routing refusal) or ``budget_exceeded``
(headroom spent out).

``settle_judge_usage`` records the judge's token usage under O8 accounting in a
transaction of its own, so the row survives a rollback of the caller's
transaction, and a tokenless reply is stored as unknown (null counts and
price), never as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg

from omp_work.jobs.usage import record_usage
from omp_work.routing.policy import RoutingRefused, load_policy
from omp_work.routing.router import route_stage
from omp_work.v1.canonical import sha256

if TYPE_CHECKING:
    from omp_work.jobs.store import NativeJobStore
    from omp_work.project_store import ProjectStoreMixin

__all__ = [
    "Admission",
    "IntakeAdmissionError",
    "resolve_admission",
    "settle_judge_usage",
]

MODEL_GRANT_REQUIRED = "model_grant_required"
BUDGET_EXCEEDED = "budget_exceeded"
JUDGE_ROLE = "intake_support_judge"
_MEASURED = "measured"
_UNKNOWN = "unknown"


class IntakeAdmissionError(Exception):
    """A refused intake admission, carrying a stable machine code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Admission:
    """One admitted judge call: the routed model, headroom, and grant identity."""

    model_id: str
    remaining_usd: Decimal
    grant_id: str


def resolve_admission(
    projects: ProjectStoreMixin,
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    mission_id: UUID,
) -> Admission:
    """Admit one research-route judge call against the mission's budgets.

    ``project_id`` comes from ``omp_work.project_missions``. The active project
    budget (``mission_id IS NULL``) and the mission's own budget are loaded and
    their spend summed exactly as model-call admission does; remaining headroom
    is ``min(ceiling - spent)`` over the present budgets. No budget, an unknown
    mission, or a refusal from ``route_stage(load_policy(), "research")`` is
    ``model_grant_required``; headroom at or below zero is ``budget_exceeded``.
    Reads only: this never writes and never settles usage.
    """
    cur.execute(
        "SELECT project_id FROM omp_work.project_missions"
        " WHERE workspace_id=%s AND mission_id=%s",
        (workspace_id, mission_id),
    )
    row = cur.fetchone()
    if row is None:
        raise IntakeAdmissionError(MODEL_GRANT_REQUIRED)
    project_id = _as_uuid(row["project_id"])
    project_b, _ = projects._load_budget(cur, workspace_id, project_id, None)
    mission_b, _ = projects._load_budget(cur, workspace_id, project_id, mission_id)
    budgets = [b for b in (project_b, mission_b) if b is not None]
    if not budgets:
        raise IntakeAdmissionError(MODEL_GRANT_REQUIRED)
    try:
        route = route_stage(load_policy(), "research")
    except RoutingRefused:
        raise IntakeAdmissionError(MODEL_GRANT_REQUIRED) from None
    spent = projects._spent_by_budget(
        cur, workspace_id, project_id, mission_id, mission_b, project_b
    )
    remaining = min(budget.ceiling_usd - spent[budget.budget_id] for budget in budgets)
    if remaining <= 0:
        raise IntakeAdmissionError(BUDGET_EXCEEDED)
    grant_id = sha256(
        {
            "workspace_id": str(workspace_id),
            "mission_id": str(mission_id),
            "rule": route.rule,
            "model": route.model,
        }
    )
    return Admission(model_id=route.model, remaining_usd=remaining, grant_id=grant_id)


def settle_judge_usage(
    jobs: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    mission_id: UUID,
    model_id: str,
    request_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> str:
    """Record one judge usage event in its own transaction and return its usage id.

    ``work_id`` is the mission, there is no job, and the role is
    ``intake_support_judge``. Both counts are integers: the measurement is
    ``measured`` with a zero cache and no price. Both are ``None``: the
    measurement is ``unknown`` with every count and the price left null. One of
    each is ``ValueError``. ``record_usage`` is called with ``cur=None`` so it
    commits on its own and the row outlives a rollback of the caller.
    """
    if (input_tokens is None) != (output_tokens is None):
        raise ValueError(
            "input_tokens and output_tokens must both be ints or both None"
        )
    if input_tokens is None:
        measurement = _UNKNOWN
        cache_tokens = None
    else:
        measurement = _MEASURED
        cache_tokens = 0
    result = record_usage(
        jobs,
        operation_id=operation_id,
        workspace_id=workspace_id,
        actor_id=actor_id,
        work_id=mission_id,
        job_id=None,
        request_id=request_id,
        role=JUDGE_ROLE,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        measurement=measurement,
        price_usd=None,
        cur=None,
    )
    return str(result["usage_id"])


def _as_uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
