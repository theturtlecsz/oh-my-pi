"""Native research job store on the shared omp_jobs substrate (R03, OMP-324).

Research jobs are ordinary ``omp_jobs.jobs`` rows with ``source='native'`` and
``kind`` ``model`` or ``compute``. There is no second ledger: the flood mirror
(``source='flood_import'``, ``kind IS NULL``) keeps inserting and updating on the
same tables. ``omp_jobs`` has no RLS, so every query filters ``workspace_id``.

The store runs on ``omp_work_app`` with the RLS context set so it can read
``omp_research.components`` when binding a worker to its registered R02
``worker`` component.

Committed-but-unacknowledged recovery is the operation ledger: the same
``operation_id`` with the same request replays the stored result and never runs
the effect a second time.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import sha256
from omp_work.v1.store_shared import row_json


class JobError(Exception):
    """A refused native-job operation, addressed by a stable code."""

    def __init__(self, code: str, diagnostics: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class OperationOutcome:
    """Result of one idempotent operation: ``applied`` or ``replayed``."""

    state: str
    result: dict[str, object]


_JOB_FIELDS = (
    "job_id,idempotency_key,status,source,provider_partition,path_lease,"
    "expected_max,mission_id,depends_on,packet_path,blocker,flood_origin,"
    "gate_evidence,created_at,updated_at,mirror_namespace,mirror_tombstoned_at,"
    "workspace_id,work_id,trial_id,parent_job_id,kind,required_capabilities,"
    "resources,lease_seconds,fence,attempt,worker_id,lease_expires_at,"
    "settlement,settled_at,cancel_reason,cancelled_at"
)
_WORKER_FIELDS = (
    "worker_id,workspace_id,component_sha256,capabilities,capacity,state,"
    "registered_at,updated_at"
)

_JOB_STATUSES = frozenset(
    {
        "backlog",
        "admitted",
        "in_flight",
        "returned",
        "checking",
        "sealed",
        "failed",
        "cancelled",
        "empty_soft",
    }
)
_JOB_KINDS = frozenset({"model", "compute"})
_OUTBOX_STATES = frozenset({"open", "committed", "acknowledged", "closed", "failed"})


class NativeJobStore:
    def __init__(self, config: OperationsConfig) -> None:
        self._config = config

    @contextmanager
    def transaction(
        self, workspace_id: UUID, actor_id: UUID
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with psycopg.connect(
            **self._config.connection_kwargs("omp_work_app"), row_factory=dict_row
        ) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL search_path = pg_catalog")
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                        (str(workspace_id), str(actor_id)),
                    )
                    yield cur

    def run_operation(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        operation_id: str,
        workspace_id: UUID,
        kind: str,
        request: object,
        apply: Callable[[], dict[str, object]],
    ) -> OperationOutcome:
        """Idempotent operation ledger.

        Same ``operation_id`` and same request replays the stored result with no
        second effect; a different request under the same id is refused as
        ``idempotency_conflict``.
        """
        request_sha256 = sha256(request)
        cur.execute(
            "SELECT kind, request_sha256, result FROM omp_jobs.operations WHERE operation_id=%s FOR UPDATE",
            (operation_id,),
        )
        stored = cur.fetchone()
        if stored is not None:
            if stored["kind"] == kind and stored["request_sha256"] == request_sha256:
                return OperationOutcome("replayed", dict(stored["result"] or {}))
            raise JobError(
                "idempotency_conflict",
                ("operation id already used for a different request",),
            )
        result = apply()
        cur.execute(
            "INSERT INTO omp_jobs.operations(operation_id, workspace_id, kind, request_sha256, result) VALUES (%s, %s, %s, %s, %s)",
            (operation_id, workspace_id, kind, request_sha256, Jsonb(result)),
        )
        return OperationOutcome("applied", result)

    def job_view(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        job_id: str,
    ) -> dict[str, object] | None:
        """Every ``omp_jobs.jobs`` column (JSON-safe) for one workspace row."""
        cur.execute(
            f"SELECT {_JOB_FIELDS} FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",  # nosec B608 - static column list
            (workspace_id, job_id),
        )
        return row_json(cur.fetchone())

    def worker_view(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        worker_id: str,
    ) -> dict[str, object] | None:
        """Every ``omp_jobs.workers`` column (JSON-safe) for one workspace row."""
        cur.execute(
            f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s",  # nosec B608 - static column list
            (workspace_id, worker_id),
        )
        return row_json(cur.fetchone())

    def append_event(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        *,
        workspace_id: UUID,
        job_id: str,
        kind: str,
        actor: str | None = None,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
        operation_id: str | None = None,
    ) -> int:
        """Append one job event, assigning the next per-job sequence."""
        cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM omp_jobs.job_events WHERE job_id=%s",
            (job_id,),
        )
        seq = int(cur.fetchone()["seq"])
        cur.execute(
            "INSERT INTO omp_jobs.job_events(job_id, seq, actor, reason, kind, operation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                job_id,
                seq,
                actor,
                reason,
                kind,
                operation_id,
                Jsonb(payload) if payload is not None else None,
            ),
        )
        return seq

    def cancelled_ancestor(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        job_id: str,
    ) -> dict[str, object] | None:
        """Nearest cancelled ancestor of ``job_id`` in this workspace, if any.

        Cancellation is inherited: a job whose ancestor was cancelled must not be
        admitted. Only ancestors are walked — a job's own status is the caller's
        check. The walk is bounded so a malformed self-parent cannot loop.
        """
        seen: set[str] = set()
        cur.execute(
            "SELECT parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
            (workspace_id, job_id),
        )
        row = cur.fetchone()
        current = row["parent_job_id"] if row is not None else None
        while current is not None and current not in seen:
            seen.add(current)
            cur.execute(
                "SELECT job_id, status, parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (workspace_id, current),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row["status"] == "cancelled":
                return row_json(row)
            current = row["parent_job_id"]
        return None


def _capability_scope(
    cur: psycopg.Cursor[dict[str, object]],
    workspace_id: UUID,
    component_sha256: str,
) -> list[str]:
    """Capabilities declared by the registered ``worker`` component, or refuse."""
    cur.execute(
        "SELECT kind, descriptor FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
        (workspace_id, component_sha256),
    )
    row = cur.fetchone()
    if row is None:
        raise JobError("job_worker_unavailable", ("unknown worker component",))
    if row["kind"] != "worker":
        raise JobError(
            "job_worker_unavailable",
            ("component fingerprint is not a worker component",),
        )
    descriptor = row["descriptor"]
    if isinstance(descriptor, str):
        descriptor = json.loads(descriptor)
    declared = descriptor.get("capabilities") or []
    return sorted(str(capability) for capability in declared)


def register_worker(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    worker_id: str,
    component_sha256: str,
    capabilities: list[str],
    capacity: int,
) -> dict[str, object]:
    """Register (or idempotently replay) a worker bound to an R02 worker component.

    ``capabilities`` are stored sorted and must sit within the component's
    descriptor. Re-registering the same identity replays; a changed identity or a
    drained worker is refused as ``job_worker_unavailable``.
    """
    ordered = sorted(str(capability) for capability in capabilities)
    if capacity < 0:
        raise JobError("invalid_request", ("worker capacity must be non-negative",))
    request = {
        "worker_id": worker_id,
        "component_sha256": component_sha256,
        "capabilities": ordered,
        "capacity": capacity,
    }

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            scope = _capability_scope(cur, workspace_id, component_sha256)
            outside = [capability for capability in ordered if capability not in scope]
            if outside:
                raise JobError(
                    "job_worker_unavailable",
                    (f"capability outside descriptor: {', '.join(outside)}",),
                )
            cur.execute(
                f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s FOR UPDATE",  # nosec B608 - static column list
                (workspace_id, worker_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                same_identity = (
                    existing["component_sha256"] == component_sha256
                    and list(existing["capabilities"]) == ordered
                    and existing["capacity"] == capacity
                )
                if same_identity and existing["state"] == "active":
                    return {"status": "replayed", "worker": row_json(existing)}
                raise JobError(
                    "job_worker_unavailable",
                    ("worker identity changed or worker is draining",),
                )
            cur.execute(
                f"""
                INSERT INTO omp_jobs.workers(worker_id, workspace_id, component_sha256, capabilities, capacity, state)
                VALUES (%s, %s, %s, %s, %s, 'active')
                RETURNING {_WORKER_FIELDS}
                """,  # nosec B608 - static column list
                (worker_id, workspace_id, component_sha256, Jsonb(ordered), capacity),
            )
            return {"status": "applied", "worker": row_json(cur.fetchone())}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "worker_register", request, apply
        )
        status = (
            "replayed"
            if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
            else "applied"
        )
        return {
            "operation_id": operation_id,
            "status": status,
            "worker": outcome.result.get("worker"),
        }


def drain_worker(
    store: NativeJobStore,
    *,
    operation_id: str,
    workspace_id: UUID,
    actor_id: UUID,
    worker_id: str,
) -> dict[str, object]:
    """Move a worker to ``draining`` permanently (the trigger forbids reactivation)."""
    request = {"worker_id": worker_id}

    with store.transaction(workspace_id, actor_id) as cur:

        def apply() -> dict[str, object]:
            cur.execute(
                f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s FOR UPDATE",  # nosec B608 - static column list
                (workspace_id, worker_id),
            )
            existing = cur.fetchone()
            if existing is None:
                raise JobError("job_worker_unavailable", ("unknown worker",))
            if existing["state"] == "draining":
                return {"status": "replayed", "worker": row_json(existing)}
            cur.execute(
                f"""
                UPDATE omp_jobs.workers SET state='draining', updated_at=clock_timestamp()
                WHERE workspace_id=%s AND worker_id=%s
                RETURNING {_WORKER_FIELDS}
                """,  # nosec B608 - static column list
                (workspace_id, worker_id),
            )
            return {"status": "applied", "worker": row_json(cur.fetchone())}

        outcome = store.run_operation(
            cur, operation_id, workspace_id, "worker_drain", request, apply
        )
        status = (
            "replayed"
            if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
            else "applied"
        )
        return {
            "operation_id": operation_id,
            "status": status,
            "worker": outcome.result.get("worker"),
        }
