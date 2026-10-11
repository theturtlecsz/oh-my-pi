"""Model-call admission over shared project, item, and task budgets (OMP-404, OMP-418).

``ModelProxyBudget`` owns one connection and transaction per admitted call and
holds the shared spend locks until the call settles: the project row, the
mission row, the mission and project budget rows (the existing spend order),
then the root work item's advisory lock and the task advisory lock. Because a
later waiter can pass a stop or lease check that happened before it blocked, the
stop state and the job/work/root identity are read again after every lock using
a fresh ``clock_timestamp()``, never the transaction ``now()``. A refusal writes
nothing and happens before any upstream send.

Every instance in the process shares one serialization gate (a module-level
lock) so two in-process admissions cannot interleave their lock acquisition and
accounting; the gate is acquired before the first database lock and released
when the admission settles or aborts. Cross-process exclusion is the database
lock itself.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self
from uuid import UUID

import psycopg

from omp_work.jobs.budget import item_budget, item_spend, root_work_id
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy_upstream import Prepared
from omp_work.operations.config import OperationsConfig
from omp_work.project_store import ProjectAuthorityRefused, ProjectStoreMixin
from omp_work.spend_budget import SpendDecision
from omp_work.v1.agent_stop import read_stop_state
from omp_work.v1.canonical import sha256
from omp_work.v1.store import PostgresWorkStore

__all__ = [
    "ModelCallAdmission",
    "ModelProxyBudget",
    "ModelProxyBudgetError",
]

# One gate for every ModelProxyBudget in the process. Held from the first
# database lock through settlement so an in-process admission cannot be
# interleaved. Reentrancy is deliberately absent: an admission nested inside
# another admission would deadlock against its own held budget locks anyway.
_GATE = threading.Lock()


class ModelProxyBudgetError(Exception):
    """Raised when model-call admission or settlement is refused."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(f"{code}: {self.detail}" if detail and detail != code else code)


class ModelProxyBudget:
    """Admits bounded model calls against shared budgets under held locks."""

    def __init__(
        self,
        config: OperationsConfig,
        *,
        jobs: NativeJobStore | None = None,
        projects: ProjectStoreMixin | None = None,
    ) -> None:
        self._config = config
        self._jobs = jobs if jobs is not None else NativeJobStore(config)
        self._projects = projects if projects is not None else PostgresWorkStore(config)

    def admit(
        self,
        workspace_id: UUID | str,
        actor_id: UUID | str,
        project_id: UUID | str | None,
        mission_id: UUID | str | None,
        work_id: UUID | str,
        job_id: str | None,
        worker_id: str | None,
        fence: int | None,
        request_id: str,
        task_ceiling_usd: Decimal | str | None,
        prepared: Prepared,
        *,
        role: str = "model_call",
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> ModelCallAdmission:
        """Admit a bounded model call, holding locks until it settles.

        The bound is ``prepared.max_usd`` and ``prepared.max_tokens``. Admission
        locks the project row, the mission row, then the mission and project
        budget rows (through ``authorize_spend_cursor``), then the root item and
        task advisory locks. Only after every wait does it lock the job row and
        re-read the stop state and the job's source/status/worker/fence/lease and
        root identity. Any exhausted or exceeded cap, a stale lease, or a stopped
        workspace raises ``ModelProxyBudgetError`` and writes nothing.
        """
        workspace_uuid = _as_uuid("workspace_id", workspace_id)
        actor_uuid = _as_uuid("actor_id", actor_id)
        work_uuid = _as_uuid("work_id", work_id)
        project_uuid = _as_uuid("project_id", project_id)
        mission_uuid = _as_uuid("mission_id", mission_id)
        now = now or datetime.now(UTC)

        _require_prepared(prepared)
        max_usd = prepared.max_usd
        max_tokens = prepared.max_tokens

        if not isinstance(request_id, str) or not request_id:
            raise ModelProxyBudgetError("invalid_request", "request_id is required")
        if not isinstance(role, str) or not role:
            raise ModelProxyBudgetError("invalid_request", "role is required")

        if job_id is not None and (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(worker_id, str)
            or not worker_id
            or isinstance(fence, bool)
            or not isinstance(fence, int)
        ):
            raise ModelProxyBudgetError(
                "invalid_request",
                "worker_id and fence are required when job_id is set",
            )

        task_ceiling = _optional_decimal("task_ceiling_usd", task_ceiling_usd)
        operation_id = operation_id or f"model_call:{request_id}"

        _GATE.acquire()
        transaction = contextlib.ExitStack()
        try:
            cur = transaction.enter_context(
                self._jobs.transaction(workspace_uuid, actor_uuid)
            )

            # Fast refuse before any lock. A waiter blocked later is checked
            # again after those locks, in _revalidate.
            _refuse_if_stopped(cur, workspace_uuid)

            if project_uuid is None and mission_uuid is not None:
                cur.execute(
                    "SELECT project_id FROM omp_work.project_missions"
                    " WHERE workspace_id=%s AND mission_id=%s",
                    (workspace_uuid, mission_uuid),
                )
                mission_row = cur.fetchone()
                if mission_row is None:
                    raise ModelProxyBudgetError(
                        "mission_not_found", f"no mission with id {mission_uuid}"
                    )
                project_uuid = UUID(str(mission_row["project_id"]))

            decision: SpendDecision | None = None
            if project_uuid is not None:
                cur.execute(
                    "SELECT project_id FROM omp_work.projects"
                    " WHERE workspace_id=%s AND project_id=%s FOR UPDATE",
                    (workspace_uuid, project_uuid),
                )
                if cur.fetchone() is None:
                    raise ModelProxyBudgetError(
                        "project_not_found", f"no project with id {project_uuid}"
                    )
                if mission_uuid is not None:
                    cur.execute(
                        "SELECT mission_id FROM omp_work.project_missions"
                        " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s FOR UPDATE",
                        (workspace_uuid, project_uuid, mission_uuid),
                    )
                    if cur.fetchone() is None:
                        raise ModelProxyBudgetError(
                            "mission_not_found", f"no mission with id {mission_uuid}"
                        )
                # Locks the mission budget then the project budget, in the
                # existing spend order, and pins them until settlement.
                try:
                    decision = self._projects.authorize_spend_cursor(
                        cur,
                        workspace_uuid,
                        project_uuid,
                        mission_uuid,
                        max_usd,
                        now,
                        reject_exhausted=True,
                    )
                except ProjectAuthorityRefused as refused:
                    raise ModelProxyBudgetError(refused.code, str(refused)) from refused

            budget_work_id = work_uuid
            root_before: UUID | None = None
            if job_id is not None:
                root_before = root_work_id(cur, workspace_uuid, job_id)
                if root_before is not None:
                    budget_work_id = root_before

            # Shared advisory locks, taken before the job row so a job lock is
            # never held while waiting for them.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"omp_work.item_budget:{workspace_uuid}:{budget_work_id}",),
            )
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"omp_work.task_budget:{workspace_uuid}:{work_uuid}",),
            )

            _revalidate(
                cur,
                workspace_uuid,
                work_uuid,
                job_id,
                worker_id,
                fence,
                root_before,
            )

            _enforce_item_cap(cur, workspace_uuid, budget_work_id, max_usd, max_tokens)
            if task_ceiling is not None:
                _enforce_task_cap(cur, workspace_uuid, work_uuid, max_usd, task_ceiling)

            return ModelCallAdmission(
                budget=self,
                _transaction=transaction,
                _cur=cur,
                workspace_id=workspace_uuid,
                actor_id=actor_uuid,
                work_id=work_uuid,
                root_work_id=budget_work_id,
                project_id=project_uuid,
                mission_id=mission_uuid,
                job_id=job_id,
                worker_id=worker_id,
                fence=fence,
                request_id=request_id,
                operation_id=operation_id,
                role=role,
                prepared=prepared,
                decision=decision,
            )
        except BaseException as error:
            try:
                transaction.__exit__(type(error), error, error.__traceback__)
            finally:
                _GATE.release()
            raise


@dataclass
class _AdmissionState:
    settled: bool = False
    closed: bool = False


@dataclass(frozen=True, slots=True)
class ModelCallAdmission:
    """Immutable admitted identity; transaction holds locks until settlement."""

    budget: ModelProxyBudget
    _transaction: contextlib.ExitStack
    _cur: psycopg.Cursor[dict[str, Any]]
    workspace_id: UUID
    actor_id: UUID
    work_id: UUID
    root_work_id: UUID
    project_id: UUID | None
    mission_id: UUID | None
    job_id: str | None
    worker_id: str | None
    fence: int | None
    request_id: str
    operation_id: str
    role: str
    prepared: Prepared
    decision: SpendDecision | None
    _state: _AdmissionState = field(default_factory=_AdmissionState, init=False)

    @property
    def settled(self) -> bool:
        return self._state.settled

    @property
    def closed(self) -> bool:
        return self._state.closed

    @property
    def cursor(self) -> psycopg.Cursor[dict[str, Any]]:
        """The caller-owned cursor holding the budget locks."""
        return self._cur

    @property
    def bounds(self) -> Prepared:
        """The immutable ``Prepared`` identity and bounds this call was admitted on."""
        return self.prepared

    @property
    def max_usd(self) -> Decimal:
        return self.prepared.max_usd

    @property
    def max_tokens(self) -> int:
        return self.prepared.max_tokens

    def settle(
        self,
        *,
        actual_usd: Decimal | str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_tokens: int | None = None,
        model: str | None = None,
        measurement: str = "measured",
        price_version: str | None = None,
    ) -> dict[str, object]:
        """Record measured usage and the project spend once, then release the locks.

        The spend row uses the authorization captured at admission; the budget
        rows are still locked, so the decision cannot have been overtaken. A
        repeated identity replays the usage and records no second spend.
        """
        if self.settled or self.closed:
            raise ModelProxyBudgetError(
                "already_settled", "admission is already settled or closed"
            )
        usd = _nonneg_decimal("actual_usd", actual_usd)
        if usd > self.max_usd:
            raise ModelProxyBudgetError(
                "usage_exceeds_admission",
                f"actual USD {usd} exceeds admitted bound {self.max_usd}",
            )
        counted = _token_sum(input_tokens, output_tokens, cache_tokens)
        if counted > self.max_tokens:
            raise ModelProxyBudgetError(
                "usage_exceeds_admission",
                f"actual tokens {counted} exceed admitted bound {self.max_tokens}",
            )
        try:
            usage = self._record(
                usd=usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                measurement=measurement,
                model=model or self.prepared.route.model,
                price_version=price_version,
            )
            self._close()
            self._state.settled = True
        except BaseException as error:
            self._close(error)
            raise
        return {"status": "settled", "actual_usd": usd, "usage": usage}

    def settle_unmeasurable_failure(
        self, error: BaseException | str | None = None
    ) -> None:
        """Charge the admitted bound, commit, and refuse (capacity is never freed).

        Used when the request may have reached the provider but its usage cannot
        be measured; ``Prepared`` documents that the caller then charges
        ``max_usd`` and ``max_tokens``.
        """
        if self.settled or self.closed:
            raise ModelProxyBudgetError(
                "already_settled", "admission is already settled or closed"
            )
        try:
            self._record(
                usd=self.max_usd,
                input_tokens=self.max_tokens,
                output_tokens=0,
                cache_tokens=0,
                measurement="estimated",
                model=self.prepared.route.model,
                price_version=None,
            )
            self._close()
            self._state.settled = True
        except BaseException as error:
            self._close(error)
            raise
        detail = str(error) if error is not None else "unmeasurable upstream failure"
        raise ModelProxyBudgetError("unmeasurable_failure", detail)

    def abort(self) -> None:
        """Abort before any send: roll back, write no ledgers, and free the locks."""
        if self.closed:
            return
        self._close(ModelProxyBudgetError("aborted_before_send"))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        if not self.settled and not self.closed:
            self.abort()
        return False

    def _record(
        self,
        *,
        usd: Decimal,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_tokens: int | None,
        measurement: str,
        model: str,
        price_version: str | None,
    ) -> dict[str, object]:
        usage = record_usage(
            self.budget._jobs,
            operation_id=self.operation_id,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            work_id=self.work_id,
            job_id=self.job_id,
            request_id=self.request_id,
            role=self.role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_tokens,
            measurement=measurement,
            price_usd=usd,
            price_version=price_version,
            cur=self._cur,
        )
        # A replayed usage records no second project charge. The spend identity is
        # deterministic, so a replay maps to the same spend_records row.
        if (
            usage.get("status") != "replayed"
            and self.project_id is not None
            and usd > 0
        ):
            assert self.decision is not None
            self.budget._projects.append_spend_cursor(
                self._cur,
                self.workspace_id,
                self.project_id,
                self.mission_id,
                usd,
                self.decision,
                self._spend_record_id(),
            )
        return usage

    def _spend_record_id(self) -> UUID:
        digest = sha256(
            {
                "kind": "model_proxy_spend",
                "workspace_id": str(self.workspace_id),
                "work_id": str(self.work_id),
                "job_id": self.job_id,
                "request_id": self.request_id,
                "project_id": (
                    None if self.project_id is None else str(self.project_id)
                ),
                "mission_id": (
                    None if self.mission_id is None else str(self.mission_id)
                ),
            }
        )
        return UUID(bytes=bytes.fromhex(digest[:32]))

    def _close(self, error: BaseException | None = None) -> None:
        if self.closed:
            return
        self._state.closed = True
        try:
            if error is None:
                self._transaction.close()
            else:
                self._transaction.__exit__(type(error), error, error.__traceback__)
        finally:
            _GATE.release()


def _revalidate(
    cur: psycopg.Cursor[dict[str, Any]],
    workspace_id: UUID,
    work_id: UUID,
    job_id: str | None,
    worker_id: str | None,
    fence: int | None,
    root_before: UUID | None,
) -> None:
    """Re-read stop and job identity after every lock, with a fresh clock.

    The stop state and the job's source/status/worker/fence and lease are read
    again so a waiter cannot pass a check that happened before it blocked. The
    lease is compared against ``clock_timestamp()`` in the database, never the
    transaction ``now()``, so a lease that expired while waiting is refused. The
    root work item is re-read and must match the one the advisory lock keyed.
    """
    if job_id is not None:
        # Finish the final potentially blocking lock before evaluating stop or
        # lease. PostgreSQL may evaluate SELECT projections before FOR UPDATE waits.
        cur.execute(
            "SELECT job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s FOR UPDATE",
            (workspace_id, job_id),
        )
        cur.fetchone()
    _refuse_if_stopped(cur, workspace_id)
    if job_id is None:
        return
    cur.execute(
        """
        SELECT status, source, worker_id, fence, work_id,
               (lease_expires_at > clock_timestamp()) AS lease_held
        FROM omp_jobs.jobs
        WHERE workspace_id=%s AND job_id=%s
        """,
        (workspace_id, job_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ModelProxyBudgetError("job_fence_stale", f"job {job_id} not found")
    job_work_id = row["work_id"]
    if job_work_id is None or UUID(str(job_work_id)) != work_id:
        raise ModelProxyBudgetError(
            "invalid_request", "job does not belong to the specified work item"
        )
    if row["source"] != "native" or row["status"] != "admitted":
        raise ModelProxyBudgetError(
            "job_fence_stale", f"job {job_id} is not an admitted native job"
        )
    if row["worker_id"] != worker_id:
        raise ModelProxyBudgetError("job_fence_stale", "worker_id mismatch on lease")
    if int(row["fence"]) != fence:
        raise ModelProxyBudgetError("job_fence_stale", "fence mismatch on lease")
    if not row["lease_held"]:
        raise ModelProxyBudgetError("job_fence_stale", "job lease has expired")
    if root_work_id(cur, workspace_id, job_id) != root_before:
        raise ModelProxyBudgetError(
            "invalid_request", "root work item changed while waiting for locks"
        )


def _enforce_item_cap(
    cur: psycopg.Cursor[dict[str, Any]],
    workspace_id: UUID,
    work_id: UUID,
    max_usd: Decimal,
    max_tokens: int,
) -> None:
    """Refuse when the root item's USD, tokens, or wall clock is spent out."""
    budget = item_budget(cur, workspace_id, work_id)
    if budget is None:
        return
    spend = item_spend(cur, workspace_id, work_id)
    if spend.usd >= Decimal(budget.usd) or spend.usd + max_usd > Decimal(budget.usd):
        raise ModelProxyBudgetError(
            "item_budget_exceeded",
            f"item USD cap {budget.usd} exceeded by {spend.usd + max_usd}",
        )
    if spend.tokens >= budget.tokens or spend.tokens + max_tokens > budget.tokens:
        raise ModelProxyBudgetError(
            "item_budget_exceeded",
            f"item token cap {budget.tokens} exceeded by {spend.tokens + max_tokens}",
        )
    if spend.wall_clock_seconds >= budget.wall_clock_seconds:
        raise ModelProxyBudgetError(
            "item_budget_exceeded",
            f"item wall clock cap {budget.wall_clock_seconds} exhausted",
        )


def _enforce_task_cap(
    cur: psycopg.Cursor[dict[str, Any]],
    workspace_id: UUID,
    work_id: UUID,
    max_usd: Decimal,
    task_ceiling: Decimal,
) -> None:
    """Refuse when the work item's recorded spend plus the bound exceeds the ceiling."""
    cur.execute(
        "SELECT COALESCE(SUM(price_usd), 0) AS usd FROM omp_jobs.usage_events"
        " WHERE workspace_id=%s AND work_id=%s",
        (workspace_id, work_id),
    )
    row = cur.fetchone()
    spent = Decimal(str(row["usd"])) if row is not None else Decimal("0")
    if spent >= task_ceiling or spent + max_usd > task_ceiling:
        raise ModelProxyBudgetError(
            "task_ceiling_exceeded",
            f"task USD ceiling {task_ceiling} exceeded by {spent + max_usd}",
        )


def _refuse_if_stopped(
    cur: psycopg.Cursor[dict[str, Any]], workspace_id: UUID
) -> None:
    if read_stop_state(cur, workspace_id)["stopped"]:
        raise ModelProxyBudgetError("agent_stop_engaged", "agent stop is engaged")


def _require_prepared(prepared: object) -> None:
    if not isinstance(prepared, Prepared):
        raise ModelProxyBudgetError("invalid_request", "prepared must be a Prepared")
    if (
        not isinstance(prepared.max_usd, Decimal)
        or not prepared.max_usd.is_finite()
        or prepared.max_usd < 0
    ):
        raise ModelProxyBudgetError("invalid_request", "prepared.max_usd is invalid")
    if (
        isinstance(prepared.max_input_tokens, bool)
        or not isinstance(prepared.max_input_tokens, int)
        or prepared.max_input_tokens < 0
        or isinstance(prepared.max_output_tokens, bool)
        or not isinstance(prepared.max_output_tokens, int)
        or prepared.max_output_tokens < 0
    ):
        raise ModelProxyBudgetError("invalid_request", "prepared token bounds are invalid")


def _token_sum(
    input_tokens: int | None, output_tokens: int | None, cache_tokens: int | None
) -> int:
    total = 0
    for value in (input_tokens, output_tokens, cache_tokens):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelProxyBudgetError(
                "invalid_request", "token counts must be integers >= 0 or None"
            )
        total += value
    return total


def _as_uuid(name: str, value: UUID | str | None) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError) as error:
        raise ModelProxyBudgetError(
            "invalid_request", f"{name} must be a UUID"
        ) from error


def _nonneg_decimal(name: str, value: Decimal | str) -> Decimal:
    if isinstance(value, bool):
        raise ModelProxyBudgetError(
            "invalid_request", f"{name} must be a non-negative finite decimal"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ModelProxyBudgetError(
            "invalid_request", f"{name} must be a non-negative finite decimal"
        ) from error
    if not parsed.is_finite() or parsed < 0:
        raise ModelProxyBudgetError(
            "invalid_request", f"{name} must be a non-negative finite decimal"
        )
    return parsed


def _optional_decimal(name: str, value: Decimal | str | None) -> Decimal | None:
    if value is None:
        return None
    return _nonneg_decimal(name, value)
