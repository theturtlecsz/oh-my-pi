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
accounting. Each acquisition installs one lifecycle owner token. The token is
the only owner of that hold: transaction cleanup, gate release, and the
accounting settled flag are separate, and ``Lock.locked()`` is not an ownership
test. Owner transitions are serialized. Cleanup of the cursor and connection
finishes before that token releases the gate once. Admission refusal, completed
receipt replay, settlement, abort, and context exit all go through the owner.
A consumed exit stack is not a live transaction. Cross-process exclusion is the
database lock itself. Completed receipts are checked before authorization so
retries can replay after cancellation or stop without another upstream call.
Failed settlements retain their owner until private reconciliation verifies or
commits the captured accounting in a fresh transaction. A receipt replay whose
read-transaction commit fails before ``admit`` returns keeps that closed,
unresolved owner in process-wide state. A later identical admit resolves it
before acquiring another gate, and only after a durable matching receipt is
read and cleaned up. Changed intent conflicts and leaves the retained owner in
place.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self
from uuid import UUID

import psycopg

from omp_work.jobs.budget import item_budget, item_spend, root_work_id
from omp_work.jobs.store import JobError, NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.model_proxy_upstream import Prepared
from omp_work.operations.config import OperationsConfig
from omp_work.project_store import ProjectAuthorityRefused, ProjectStoreMixin
from omp_work.spend_budget import SpendDecision
from omp_work.v1.agent_stop import read_stop_state
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.store import PostgresWorkStore

__all__ = [
    "ModelCallAdmission",
    "ModelProxyBudget",
    "ModelProxyBudgetError",
    "acquire_captured_locks",
    "lock_captured_identity",
]

# One gate for every ModelProxyBudget in the process. Held from the first
# database lock through transaction cleanup so an in-process admission cannot
# be interleaved. The owner token, not Lock.locked(), is the hold. Reentrancy
# is deliberately absent: an admission nested inside another admission would
# deadlock against its own held budget locks anyway.
class _ProcessGate:
    transition = threading.Condition()
    owner: object | None = None
    # Call identity -> replay admission whose commit failed before admit returned.
    retained_replay: dict[str, ModelCallAdmission] = {}


def _acquire_process_gate(token: object, identity: str) -> ModelCallAdmission | None:
    """Wait for either ownership or a retained replay of this identity.

    Publication and acquisition share one condition: a caller already waiting
    when replay commit fails must wake to resolve or conflict with that owner.
    """
    with _ProcessGate.transition:
        while True:
            retained = _ProcessGate.retained_replay.get(identity)
            if retained is not None:
                return retained
            if _ProcessGate.owner is None:
                _ProcessGate.owner = token
                return None
            _ProcessGate.transition.wait()


def _release_process_gate(token: object) -> None:
    """Release the gate only when ``token`` still owns it. A stale token no-ops."""
    with _ProcessGate.transition:
        if _ProcessGate.owner is not token:
            return
        _ProcessGate.owner = None
        _ProcessGate.transition.notify_all()


class _OneShotExitStack(contextlib.ExitStack):
    """Exit stack that cannot be entered again after it has been consumed."""

    def __init__(self) -> None:
        super().__init__()
        self.consumed = False

    def enter_context(self, cm: contextlib.AbstractContextManager[Any]) -> Any:
        if self.consumed:
            raise RuntimeError("consumed ExitStack cannot be reused as a live transaction")
        return super().enter_context(cm)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        if self.consumed:
            return False
        self.consumed = True
        return bool(super().__exit__(exc_type, exc, tb))


class _LifecycleOwner:
    """One acquisition's gate token and its one transaction stack.

    Transaction cleanup, gate release, and accounting settlement are separate.
    Only this token can release the gate it acquired, and only once.
    """

    def __init__(self, token: object, stack: _OneShotExitStack) -> None:
        self.token = token
        self.stack = stack
        self._mu = threading.Lock()
        self._claimed = False

    @classmethod
    def acquire(cls, identity: str) -> _LifecycleOwner | ModelCallAdmission:
        token = object()
        retained = _acquire_process_gate(token, identity)
        if retained is not None:
            return retained
        return cls(token, _OneShotExitStack())

    def claim(self) -> bool:
        """Serialize the close transition. The first caller owns cleanup."""
        with self._mu:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def consume(self, error: BaseException | None = None) -> bool:
        """Finish the database transaction once.

        A consumed stack is not a live transaction: calling this again does not
        commit or roll back a second time. The gate is not released here.
        """
        stack = self.stack
        if stack.consumed:
            return False
        if error is None:
            stack.close()
        else:
            stack.__exit__(type(error), error, error.__traceback__)
        return True

    def release(self) -> None:
        _release_process_gate(self.token)

    def complete(self, error: BaseException | None = None) -> bool:
        """Clean up the transaction, then release this token's gate once."""
        if not self.claim():
            return False
        try:
            return self.consume(error)
        finally:
            self.release()


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

    def lock_captured_identity(
        self,
        cur: psycopg.Cursor[dict[str, Any]],
        workspace_id: UUID | str,
        receipt_id: str,
        project_id: UUID | str | None = None,
        mission_id: UUID | str | None = None,
        root_work_id: UUID | str | None = None,
        work_id: UUID | str | None = None,
        job_id: str | None = None,
        *,
        projects: ProjectStoreMixin | None = None,
    ) -> None:
        """Acquire all spend and identity locks for a captured admission identity."""
        lock_captured_identity(
            cur,
            workspace_id,
            receipt_id,
            project_id=project_id,
            mission_id=mission_id,
            root_work_id=root_work_id,
            work_id=work_id,
            job_id=job_id,
            projects=projects if projects is not None else self._projects,
        )

    acquire_captured_locks = lock_captured_identity
    acquire_locks = lock_captured_identity

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

        A matching completed receipt returns ``settled=True``; callers skip the
        upstream send and can verify/replay its settlement. Changed intent is
        an idempotency conflict, even after stop or cancellation. If committing
        that read fails, the closed unresolved owner stays reachable for every
        instance. An identical later admit resolves it before taking a new gate
        and returns settled only after the durable receipt matches and cleanup
        finishes. Changed intent does not replace that owner.

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

        identity = sha256({
            "workspace_id": str(workspace_uuid), "work_id": str(work_uuid),
            "job_id": job_id, "request_id": request_id,
        })
        receipt_id = f"model_call:receipt:{identity}"
        fingerprint = {
            "workspace_id": str(workspace_uuid), "actor_id": str(actor_uuid),
            "work_id": str(work_uuid), "project_id": str(project_uuid) if project_uuid else None,
            "mission_id": str(mission_uuid) if mission_uuid else None,
            "job_id": job_id, "worker_id": worker_id, "fence": fence,
            "request_id": request_id, "operation_id": operation_id, "role": role,
            "task_ceiling_usd": _decimal_text(task_ceiling) if task_ceiling is not None else None,
            "prepared": {
                "body_sha256": hashlib.sha256(prepared.body).hexdigest(),
                "route": {
                    key: _decimal_text(value) if isinstance(value, Decimal) else value
                    for key, value in asdict(prepared.route).items()
                },
                "url": prepared.url,
                "max_input_tokens": prepared.max_input_tokens,
                "max_output_tokens": prepared.max_output_tokens,
                "max_usd": _decimal_text(max_usd),
            },
        }
        # Capture complete intent without request headers or credentials.
        receipt_request = {"workspace_id": str(workspace_uuid), "fingerprint": fingerprint}

        # Acquisition also wakes for retained-owner publication while waiting.
        owner = _LifecycleOwner.acquire(identity)
        if isinstance(owner, ModelCallAdmission):
            return owner._resolve_retained_replay(receipt_request)
        retained = False
        try:
            cur = owner.stack.enter_context(
                self._jobs.transaction(workspace_uuid, actor_uuid)
            )

            # Serialize this identity across processes before fresh authorization.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('omp_jobs.operation:' || %s, 0))",
                (receipt_id,),
            )
            cur.execute("SELECT 1 FROM omp_jobs.operations WHERE operation_id=%s", (receipt_id,))
            if cur.fetchone() is not None:
                def no_effect() -> dict[str, object]:
                    raise AssertionError("existing receipt disappeared under operation lock")

                try:
                    receipt = self._jobs.run_operation(
                        cur, receipt_id, workspace_uuid, "model_call_receipt", receipt_request, no_effect,
                    ).result
                except JobError as refused:
                    raise ModelProxyBudgetError(refused.code) from refused
                admission = ModelCallAdmission(
                    budget=self, _transaction=owner.stack, _owner=owner, _cur=cur,
                    workspace_id=workspace_uuid, actor_id=actor_uuid, work_id=work_uuid,
                    root_work_id=UUID(str(receipt["root_work_id"])),
                    project_id=project_uuid, mission_id=mission_uuid,
                    job_id=job_id, worker_id=worker_id, fence=fence,
                    request_id=request_id, operation_id=operation_id, role=role,
                    prepared=prepared, decision=None, _identity=identity,
                    _receipt_request=canonical_json(receipt_request),
                )
                # Commit the read, then publish settled and release this owner once.
                # A commit exception happens before return: keep the owner reachable
                # and do not free the gate for another send.
                admission._state.closed = True
                try:
                    if not owner.claim() or not owner.consume():
                        raise ModelProxyBudgetError(
                            "already_settled", "admission is already settled or closed"
                        )
                except BaseException:
                    retained = _keep_replay_owner(identity, admission, receipt)
                    raise
                admission._state.receipt = receipt
                admission._state.settled = True
                owner.release()
                return admission

            # Refuse before budget locks. A waiter blocked later is checked
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

            budget_work_id = work_uuid
            root_before: UUID | None = None
            if job_id is not None:
                root_before = root_work_id(cur, workspace_uuid, job_id)
                if root_before is not None:
                    budget_work_id = root_before

            self.lock_captured_identity(
                cur,
                workspace_uuid,
                receipt_id,
                project_id=project_uuid,
                mission_id=mission_uuid,
                root_work_id=budget_work_id,
                work_id=work_uuid,
                job_id=job_id,
            )

            decision: SpendDecision | None = None
            if project_uuid is not None:
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
                _transaction=owner.stack,
                _owner=owner,
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
                _identity=identity,
                _receipt_request=canonical_json(receipt_request),
            )
        except BaseException as error:
            # Retained replay owners stay until a later identical admit resolves
            # them. Every other failure still cleans up and releases this token.
            if not retained:
                owner.complete(error)
            raise


@dataclass(frozen=True)
class _Settlement:
    payload: str
    decision: SpendDecision | None


@dataclass
class _AdmissionState:
    settled: bool = False
    closed: bool = False
    receipt: dict[str, object] | None = None
    unresolved: _Settlement | None = None
    transition: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass(frozen=True, slots=True)
class ModelCallAdmission:
    """Immutable admitted identity; transaction holds locks until settlement."""

    budget: ModelProxyBudget
    _transaction: _OneShotExitStack
    _owner: _LifecycleOwner = field(repr=False)
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
    _identity: str
    _receipt_request: str
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

        The spend row uses authorization captured at admission. A failed write
        or commit retains the consumed call and gate; an identical retry checks
        its durable receipt in a fresh transaction without reauthorization.
        """
        with self._state.transition:
            if self.closed and not self.settled and self._state.unresolved is None:
                raise ModelProxyBudgetError(
                    "already_settled", "admission is already settled or closed"
                )
            usd = _nonneg_decimal("actual_usd", actual_usd)
            counted = _token_sum(input_tokens, output_tokens, cache_tokens)
            payload = {
                "actual_usd": _decimal_text(usd), "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cache_tokens": cache_tokens,
                "measurement": measurement, "model": model or self.prepared.route.model,
                "price_version": price_version,
            }
            if self._state.receipt is not None:
                if self._state.receipt["settlement"] != payload:
                    raise ModelProxyBudgetError("idempotency_conflict", "settlement payload changed")
                return {**self._state.receipt["result"], "actual_usd": usd}

            captured = self._state.unresolved
            if captured is not None:
                if captured.payload != canonical_json(payload):
                    raise ModelProxyBudgetError("idempotency_conflict", "settlement payload changed")
                return self._resolve()

            if usd > self.max_usd:
                raise ModelProxyBudgetError(
                    "usage_exceeds_admission",
                    f"actual USD {usd} exceeds admitted bound {self.max_usd}",
                )
            if counted > self.max_tokens:
                raise ModelProxyBudgetError(
                    "usage_exceeds_admission",
                    f"actual tokens {counted} exceed admitted bound {self.max_tokens}",
                )

            captured = _Settlement(canonical_json(payload), self.decision)
            try:
                with self._cur.connection.transaction():
                    receipt = self._persist(self._cur, captured)
                self._state.closed = True
                if not self._owner.claim() or not self._owner.consume():
                    raise ModelProxyBudgetError("already_settled", "transaction already consumed")
            except BaseException as error:
                # The call was consumed. Roll back/clean up its old transaction but
                # retain the process gate until accounting is durably reconciled.
                self._state.closed = True
                self._state.unresolved = captured
                self._owner.claim()
                self._owner.consume(error)
                if isinstance(error, JobError):
                    raise ModelProxyBudgetError(error.code) from error
                raise
            return self._finish_settlement(receipt)

    def _persist(
        self, cur: psycopg.Cursor[dict[str, object]], captured: _Settlement,
    ) -> dict[str, object]:
        """One atomic usage/spend/receipt writer, also used by reconciliation."""
        payload = json.loads(captured.payload)
        fingerprint = json.loads(self._receipt_request)["fingerprint"]

        def apply() -> dict[str, object]:
            if self.job_id is not None:
                current_root = root_work_id(cur, self.workspace_id, self.job_id)
                if current_root != self.root_work_id:
                    raise ModelProxyBudgetError("idempotency_conflict", "captured root changed")
            usage = self._record(
                cur=cur, decision=captured.decision,
                usd=Decimal(payload["actual_usd"]),
                input_tokens=payload["input_tokens"], output_tokens=payload["output_tokens"],
                cache_tokens=payload["cache_tokens"], measurement=payload["measurement"],
                model=payload["model"], price_version=payload["price_version"],
            )
            return {
                "root_work_id": str(self.root_work_id), "fingerprint": fingerprint,
                "settlement": payload,
                "result": {"status": "settled", "actual_usd": payload["actual_usd"], "usage": usage},
            }

        receipt = self.budget._jobs.run_operation(
            cur, f"model_call:receipt:{self._identity}", self.workspace_id,
            "model_call_receipt", json.loads(self._receipt_request), apply,
        ).result
        if (
            receipt.get("root_work_id") != str(self.root_work_id)
            or receipt.get("fingerprint") != fingerprint
            or receipt.get("settlement") != payload
        ):
            raise ModelProxyBudgetError("idempotency_conflict", "settlement receipt changed")
        return receipt

    def _resolve(self) -> dict[str, object]:
        """Resolve only a failed consumed call; never acquire fresh authority."""
        with self._state.transition:
            captured = self._state.unresolved
            if captured is None or not self.closed or self.settled:
                raise ModelProxyBudgetError("already_settled", "no unresolved settlement")
            stack = _OneShotExitStack()
            try:
                cur = stack.enter_context(
                    self.budget._jobs.transaction(self.workspace_id, self.actor_id)
                )
                self.budget.lock_captured_identity(
                    cur, self.workspace_id, f"model_call:receipt:{self._identity}",
                    project_id=self.project_id, mission_id=self.mission_id,
                    root_work_id=self.root_work_id, work_id=self.work_id, job_id=self.job_id,
                )
                receipt = self._persist(cur, captured)
                stack.close()
            except BaseException as error:
                stack.__exit__(type(error), error, error.__traceback__)
                if isinstance(error, JobError):
                    raise ModelProxyBudgetError(error.code) from error
                raise
            return self._finish_settlement(receipt)

    def _resolve_retained_replay(self, receipt_request: dict[str, object]) -> ModelCallAdmission:
        """Resolve this replay owner. The caller must not have acquired the gate.

        Identical intent re-reads the durable receipt and returns this admission
        only after that transaction commits and cleans up. Changed intent
        conflicts. Concurrent callers share this admission's transition lock, so
        a later one observes the settled owner and does not release a newer gate.
        """
        with self._state.transition:
            if canonical_json(receipt_request) != self._receipt_request:
                raise ModelProxyBudgetError("idempotency_conflict", "model call intent changed")
            if self.settled:
                return self
            if not self.closed or self._state.unresolved is None:
                raise ModelProxyBudgetError("already_settled", "no unresolved settlement")
            receipt = self._read_durable_replay()
            self._finish_settlement(receipt)
            return self

    def _read_durable_replay(self) -> dict[str, object]:
        """Read the committed receipt. Do not authorize or write a new charge."""
        stack = _OneShotExitStack()
        try:
            cur = stack.enter_context(
                self.budget._jobs.transaction(self.workspace_id, self.actor_id)
            )

            def no_effect() -> dict[str, object]:
                raise AssertionError("existing receipt disappeared under operation lock")

            try:
                receipt = self.budget._jobs.run_operation(
                    cur,
                    f"model_call:receipt:{self._identity}",
                    self.workspace_id,
                    "model_call_receipt",
                    json.loads(self._receipt_request),
                    no_effect,
                ).result
            except JobError as refused:
                raise ModelProxyBudgetError(refused.code) from refused
            fingerprint = json.loads(self._receipt_request)["fingerprint"]
            if (
                receipt.get("root_work_id") != str(self.root_work_id)
                or receipt.get("fingerprint") != fingerprint
                or not isinstance(receipt.get("settlement"), dict)
            ):
                raise ModelProxyBudgetError("idempotency_conflict", "settlement receipt changed")
            stack.close()
            return receipt
        except BaseException as error:
            if not stack.consumed:
                stack.__exit__(type(error), error, error.__traceback__)
            raise

    def _finish_settlement(self, receipt: dict[str, object]) -> dict[str, object]:
        # Called only after the writing/verifying transaction and cleanup finish.
        self._state.receipt = receipt
        self._state.settled = True
        self._state.unresolved = None
        _drop_replay_owner(self._identity, self)
        self._owner.release()
        return {**receipt["result"], "actual_usd": Decimal(receipt["settlement"]["actual_usd"])}

    def settle_unmeasurable_failure(
        self, error: BaseException | str | None = None
    ) -> None:
        """Charge the admitted bound, commit, and refuse (capacity is never freed).

        Used when the request may have reached the provider but its usage cannot
        be measured; ``Prepared`` documents that the caller then charges
        ``max_usd`` and ``max_tokens``.
        """
        self._settle_admitted_bound()
        detail = str(error) if error is not None else "unmeasurable upstream failure"
        raise ModelProxyBudgetError("unmeasurable_failure", detail)

    def _settle_admitted_bound(self) -> dict[str, object]:
        """Persist the admitted input/output capacity as estimated settlement."""
        return self.settle(**self._admitted_bound_payload())

    def _admitted_bound_payload(self) -> dict[str, object]:
        """Build bound accounting only from the captured admission, never caller usage."""
        return {
            "actual_usd": self.prepared.max_usd,
            "input_tokens": self.prepared.max_input_tokens,
            "output_tokens": self.prepared.max_output_tokens,
            "cache_tokens": 0,
            "measurement": "estimated",
            "model": self.prepared.route.model,
            "price_version": self.prepared.route.price_version,
        }

    def abort(self) -> None:
        """Abort before any send: roll back, write no ledgers, and free the locks."""
        with self._state.transition:
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
        cur: psycopg.Cursor[dict[str, object]],
        decision: SpendDecision | None,
        usd: Decimal,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_tokens: int | None,
        measurement: str,
        model: str,
        price_version: str | None,
    ) -> dict[str, object]:
        if self.project_id is not None and usd > 0:
            if decision is None:
                raise AssertionError("project spend requires a budget decision")
            self.budget._projects.append_spend_cursor(
                cur,
                self.workspace_id,
                self.project_id,
                self.mission_id,
                usd,
                decision,
                self._spend_record_id(),
            )
        usage = record_usage(
            self.budget._jobs,
            operation_id=f"model_call:usage:{self._identity}",
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            work_id=self.work_id,
            job_id=self.job_id,
            request_id=f"model_call:request:{self._identity}",
            role=self.role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_tokens,
            measurement=measurement,
            price_usd=usd,
            price_version=price_version,
            cur=cur,
        )
        # A usage row without this atomic receipt is not a completed settlement.
        if usage.get("status") == "replayed":
            raise ModelProxyBudgetError("idempotency_conflict", "usage exists without call receipt")
        return usage

    def _spend_record_id(self) -> UUID:
        return UUID(bytes=bytes.fromhex(self._identity[:32]))

    def _close(self, error: BaseException | None = None) -> bool:
        """Finish this owner's transaction, then release its gate once.

        A repeated call, including abort or context exit after another owner
        holds the gate, does not release that later hold.
        """
        claimed = self._owner.claim()
        self._state.closed = True
        if not claimed:
            return False
        try:
            return self._owner.consume(error)
        finally:
            self._owner.release()


def _keep_replay_owner(
    identity: str, admission: ModelCallAdmission, receipt: dict[str, object]
) -> bool:
    """Retain a replay owner that failed before admit returned.

    A different owner that still holds the gate is not replaced. The admission
    stays closed and unresolved so another instance cannot send it again.
    """
    settlement = receipt.get("settlement")
    payload = canonical_json(settlement) if isinstance(settlement, dict) else "{}"
    admission._state.closed = True
    admission._state.unresolved = _Settlement(payload, None)
    with _ProcessGate.transition:
        current = _ProcessGate.retained_replay.get(identity)
        if (
            current is not None
            and current is not admission
            and _ProcessGate.owner is current._owner.token
        ):
            return False
        _ProcessGate.retained_replay[identity] = admission
        _ProcessGate.transition.notify_all()
        return True


def _drop_replay_owner(identity: str, admission: ModelCallAdmission) -> None:
    with _ProcessGate.transition:
        if _ProcessGate.retained_replay.get(identity) is admission:
            del _ProcessGate.retained_replay[identity]


class _LockHelper(ProjectStoreMixin):
    """Fallback mixin instance for locking mission and budget rows."""


_DEFAULT_PROJECT_LOCKS = _LockHelper()


def _lock_job(
    cur: psycopg.Cursor[dict[str, Any]],
    workspace_id: UUID,
    job_id: str | None,
) -> None:
    """Lock the job row FOR UPDATE if a job is present."""
    if job_id is None:
        return
    cur.execute(
        "SELECT job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s FOR UPDATE",
        (workspace_id, job_id),
    )
    if cur.fetchone() is None:
        raise ModelProxyBudgetError("job_fence_stale", f"job {job_id} not found")


def lock_captured_identity(
    cur: psycopg.Cursor[dict[str, Any]],
    workspace_id: UUID | str,
    receipt_id: str,
    project_id: UUID | str | None = None,
    mission_id: UUID | str | None = None,
    root_work_id: UUID | str | None = None,
    work_id: UUID | str | None = None,
    job_id: str | None = None,
    *,
    projects: ProjectStoreMixin | None = None,
) -> None:
    """Acquire all spend and identity locks for a captured admission identity.

    Locks in order:
    1. Receipt operation advisory lock
    2. Project and mission rows (FOR UPDATE)
    3. Mission budget then project budget rows (FOR UPDATE, via ProjectStoreMixin)
    4. Captured root-item and task advisory locks
    5. Job row if present (FOR UPDATE)

    This helper does not authorize spend or validate stop/cap/lease status,
    so it can be reused across admission and settlement.
    """
    if isinstance(workspace_id, ProjectStoreMixin):
        projects = workspace_id
        workspace_id = receipt_id
        receipt_id = str(project_id)
        project_id = mission_id
        mission_id = root_work_id
        root_work_id = work_id
        work_id = job_id
        job_id = None

    workspace_uuid = _as_uuid("workspace_id", workspace_id)
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('omp_jobs.operation:' || %s, 0))",
        (receipt_id,),
    )

    project_uuid = _as_uuid("project_id", project_id)
    mission_uuid = _as_uuid("mission_id", mission_id)

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

    projects_helper = projects if projects is not None else _DEFAULT_PROJECT_LOCKS

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
            projects_helper._lock_mission(cur, workspace_uuid, project_uuid, mission_uuid)
            if cur.fetchone() is None:
                raise ModelProxyBudgetError(
                    "mission_not_found", f"no mission with id {mission_uuid}"
                )
            projects_helper._load_budget_locked(cur, workspace_uuid, project_uuid, mission_uuid)
        projects_helper._load_budget_locked(cur, workspace_uuid, project_uuid, None)

    task_uuid = _as_uuid("work_id", work_id)
    root_uuid = _as_uuid("root_work_id", root_work_id)
    if task_uuid is None:
        if root_uuid is None:
            raise ModelProxyBudgetError("invalid_request", "work_id is required")
        task_uuid = root_uuid
    if root_uuid is None:
        root_uuid = task_uuid

    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"omp_work.item_budget:{workspace_uuid}:{root_uuid}",),
    )
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"omp_work.task_budget:{workspace_uuid}:{task_uuid}",),
    )

    _lock_job(cur, workspace_uuid, job_id)


acquire_captured_locks = lock_captured_identity


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


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
