Review the database changes in commit 20900d3be9c5245be9aa0267bf6a3a122fd1ab7f.

## Scope
The following files are in scope for this review:
- python/omp-work/src/omp_work/jobs/store.py
- python/omp-work/src/omp_work/operations/jobs_migrations/0003_native_research_jobs.sql

## Instructions
1. Read the scope files to understand their context and current implementation.
2. Describe the change and inspect for any database issues, concurrency bugs, migration defects, or data integrity risks.
3. Make no edits to any files. This is a read-only review.

## Diff
```diff
diff --git a/python/omp-work/src/omp_work/jobs/store.py b/python/omp-work/src/omp_work/jobs/store.py
new file mode 100644
index 0000000..a6ec6d1
--- /dev/null
+++ b/python/omp-work/src/omp_work/jobs/store.py
@@ -0,0 +1,377 @@
+"""Native research job store on the shared omp_jobs substrate (R03, OMP-324).
+
+Research jobs are ordinary ``omp_jobs.jobs`` rows with ``source='native'`` and
+``kind`` ``model`` or ``compute``. There is no second ledger: the flood mirror
+(``source='flood_import'``, ``kind IS NULL``) keeps inserting and updating on the
+same tables. ``omp_jobs`` has no RLS, so every query filters ``workspace_id``.
+
+The store runs on ``omp_work_app`` with the RLS context set so it can read
+``omp_research.components`` when binding a worker to its registered R02
+``worker`` component.
+
+Committed-but-unacknowledged recovery is the operation ledger: the same
+``operation_id`` with the same request replays the stored result and never runs
+the effect a second time.
+"""
+from __future__ import annotations
+
+import json
+from collections.abc import Callable, Iterator
+from contextlib import contextmanager
+from dataclasses import dataclass
+from typing import Any
+from uuid import UUID
+
+import psycopg
+from psycopg.rows import dict_row
+from psycopg.types.json import Jsonb
+
+from omp_work.operations.config import OperationsConfig
+from omp_work.v1.canonical import sha256
+from omp_work.v1.store_shared import row_json
+
+
+class JobError(Exception):
+    """A refused native-job operation, addressed by a stable code."""
+
+    def __init__(self, code: str, diagnostics: tuple[str, ...] = ()) -> None:
+        super().__init__(code)
+        self.code = code
+        self.diagnostics = diagnostics
+
+
+@dataclass(frozen=True)
+class OperationOutcome:
+    """Result of one idempotent operation: ``applied`` or ``replayed``."""
+
+    state: str
+    result: dict[str, object]
+
+
+_JOB_FIELDS = (
+    "job_id,idempotency_key,status,source,provider_partition,path_lease,"
+    "expected_max,mission_id,depends_on,packet_path,blocker,flood_origin,"
+    "gate_evidence,created_at,updated_at,mirror_namespace,mirror_tombstoned_at,"
+    "workspace_id,work_id,trial_id,parent_job_id,kind,required_capabilities,"
+    "resources,lease_seconds,fence,attempt,worker_id,lease_expires_at,"
+    "settlement,settled_at,cancel_reason,cancelled_at"
+)
+_WORKER_FIELDS = (
+    "worker_id,workspace_id,component_sha256,capabilities,capacity,state,"
+    "registered_at,updated_at"
+)
+
+_JOB_STATUSES = frozenset(
+    {
+        "backlog",
+        "admitted",
+        "in_flight",
+        "returned",
+        "checking",
+        "sealed",
+        "failed",
+        "cancelled",
+        "empty_soft",
+    }
+)
+_JOB_KINDS = frozenset({"model", "compute"})
+_OUTBOX_STATES = frozenset({"open", "committed", "acknowledged", "closed", "failed"})
+
+
+class NativeJobStore:
+    def __init__(self, config: OperationsConfig) -> None:
+        self._config = config
+
+    @contextmanager
+    def transaction(
+        self, workspace_id: UUID, actor_id: UUID
+    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
+        with psycopg.connect(
+            **self._config.connection_kwargs("omp_work_app"), row_factory=dict_row
+        ) as conn:
+            with conn.transaction():
+                with conn.cursor() as cur:
+                    cur.execute("SET LOCAL search_path = pg_catalog")
+                    cur.execute(
+                        "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
+                        (str(workspace_id), str(actor_id)),
+                    )
+                    yield cur
+
+    def run_operation(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        operation_id: str,
+        workspace_id: UUID,
+        kind: str,
+        request: object,
+        apply: Callable[[], dict[str, object]],
+    ) -> OperationOutcome:
+        """Idempotent operation ledger.
+
+        Same ``operation_id`` and same request replays the stored result with no
+        second effect; a different request under the same id is refused as
+        ``idempotency_conflict``.
+        """
+        request_sha256 = sha256(request)
+        cur.execute(
+            "SELECT kind, request_sha256, result FROM omp_jobs.operations WHERE operation_id=%s FOR UPDATE",
+            (operation_id,),
+        )
+        stored = cur.fetchone()
+        if stored is not None:
+            if stored["kind"] == kind and stored["request_sha256"] == request_sha256:
+                return OperationOutcome("replayed", dict(stored["result"] or {}))
+            raise JobError(
+                "idempotency_conflict",
+                ("operation id already used for a different request",),
+            )
+        result = apply()
+        cur.execute(
+            "INSERT INTO omp_jobs.operations(operation_id, workspace_id, kind, request_sha256, result) VALUES (%s, %s, %s, %s, %s)",
+            (operation_id, workspace_id, kind, request_sha256, Jsonb(result)),
+        )
+        return OperationOutcome("applied", result)
+
+    def job_view(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        job_id: str,
+    ) -> dict[str, object] | None:
+        """Every ``omp_jobs.jobs`` column (JSON-safe) for one workspace row."""
+        cur.execute(
+            f"SELECT {_JOB_FIELDS} FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",  # nosec B608 - static column list
+            (workspace_id, job_id),
+        )
+        return row_json(cur.fetchone())
+
+    def worker_view(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        worker_id: str,
+    ) -> dict[str, object] | None:
+        """Every ``omp_jobs.workers`` column (JSON-safe) for one workspace row."""
+        cur.execute(
+            f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s",  # nosec B608 - static column list
+            (workspace_id, worker_id),
+        )
+        return row_json(cur.fetchone())
+
+    def append_event(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        *,
+        workspace_id: UUID,
+        job_id: str,
+        kind: str,
+        actor: str | None = None,
+        reason: str | None = None,
+        payload: dict[str, object] | None = None,
+        operation_id: str | None = None,
+    ) -> int:
+        """Append one job event, assigning the next per-job sequence."""
+        cur.execute(
+            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM omp_jobs.job_events WHERE job_id=%s",
+            (job_id,),
+        )
+        seq = int(cur.fetchone()["seq"])
+        cur.execute(
+            "INSERT INTO omp_jobs.job_events(job_id, seq, actor, reason, kind, operation_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)",
+            (
+                job_id,
+                seq,
+                actor,
+                reason,
+                kind,
+                operation_id,
+                Jsonb(payload) if payload is not None else None,
+            ),
+        )
+        return seq
+
+    def cancelled_ancestor(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        job_id: str,
+    ) -> dict[str, object] | None:
+        """Nearest cancelled ancestor of ``job_id`` in this workspace, if any.
+
+        Cancellation is inherited: a job whose ancestor was cancelled must not be
+        admitted. Only ancestors are walked — a job's own status is the caller's
+        check. The walk is bounded so a malformed self-parent cannot loop.
+        """
+        seen: set[str] = set()
+        cur.execute(
+            "SELECT parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
+            (workspace_id, job_id),
+        )
+        row = cur.fetchone()
+        current = row["parent_job_id"] if row is not None else None
+        while current is not None and current not in seen:
+            seen.add(current)
+            cur.execute(
+                "SELECT job_id, status, parent_job_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
+                (workspace_id, current),
+            )
+            row = cur.fetchone()
+            if row is None:
+                return None
+            if row["status"] == "cancelled":
+                return row_json(row)
+            current = row["parent_job_id"]
+        return None
+
+
+def _capability_scope(
+    cur: psycopg.Cursor[dict[str, object]],
+    workspace_id: UUID,
+    component_sha256: str,
+) -> list[str]:
+    """Capabilities declared by the registered ``worker`` component, or refuse."""
+    cur.execute(
+        "SELECT kind, descriptor FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
+        (workspace_id, component_sha256),
+    )
+    row = cur.fetchone()
+    if row is None:
+        raise JobError("job_worker_unavailable", ("unknown worker component",))
+    if row["kind"] != "worker":
+        raise JobError(
+            "job_worker_unavailable",
+            ("component fingerprint is not a worker component",),
+        )
+    descriptor = row["descriptor"]
+    if isinstance(descriptor, str):
+        descriptor = json.loads(descriptor)
+    declared = descriptor.get("capabilities") or []
+    return sorted(str(capability) for capability in declared)
+
+
+def register_worker(
+    store: NativeJobStore,
+    *,
+    operation_id: str,
+    workspace_id: UUID,
+    actor_id: UUID,
+    worker_id: str,
+    component_sha256: str,
+    capabilities: list[str],
+    capacity: int,
+) -> dict[str, object]:
+    """Register (or idempotently replay) a worker bound to an R02 worker component.
+
+    ``capabilities`` are stored sorted and must sit within the component's
+    descriptor. Re-registering the same identity replays; a changed identity or a
+    drained worker is refused as ``job_worker_unavailable``.
+    """
+    ordered = sorted(str(capability) for capability in capabilities)
+    if capacity < 0:
+        raise JobError("invalid_request", ("worker capacity must be non-negative",))
+    request = {
+        "worker_id": worker_id,
+        "component_sha256": component_sha256,
+        "capabilities": ordered,
+        "capacity": capacity,
+    }
+
+    with store.transaction(workspace_id, actor_id) as cur:
+
+        def apply() -> dict[str, object]:
+            scope = _capability_scope(cur, workspace_id, component_sha256)
+            outside = [capability for capability in ordered if capability not in scope]
+            if outside:
+                raise JobError(
+                    "job_worker_unavailable",
+                    (f"capability outside descriptor: {', '.join(outside)}",),
+                )
+            cur.execute(
+                f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s FOR UPDATE",  # nosec B608 - static column list
+                (workspace_id, worker_id),
+            )
+            existing = cur.fetchone()
+            if existing is not None:
+                same_identity = (
+                    existing["component_sha256"] == component_sha256
+                    and list(existing["capabilities"]) == ordered
+                    and existing["capacity"] == capacity
+                )
+                if same_identity and existing["state"] == "active":
+                    return {"status": "replayed", "worker": row_json(existing)}
+                raise JobError(
+                    "job_worker_unavailable",
+                    ("worker identity changed or worker is draining",),
+                )
+            cur.execute(
+                f"""
+                INSERT INTO omp_jobs.workers(worker_id, workspace_id, component_sha256, capabilities, capacity, state)
+                VALUES (%s, %s, %s, %s, %s, 'active')
+                RETURNING {_WORKER_FIELDS}
+                """,  # nosec B608 - static column list
+                (worker_id, workspace_id, component_sha256, Jsonb(ordered), capacity),
+            )
+            return {"status": "applied", "worker": row_json(cur.fetchone())}
+
+        outcome = store.run_operation(
+            cur, operation_id, workspace_id, "worker_register", request, apply
+        )
+        status = (
+            "replayed"
+            if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
+            else "applied"
+        )
+        return {
+            "operation_id": operation_id,
+            "status": status,
+            "worker": outcome.result.get("worker"),
+        }
+
+
+def drain_worker(
+    store: NativeJobStore,
+    *,
+    operation_id: str,
+    workspace_id: UUID,
+    actor_id: UUID,
+    worker_id: str,
+) -> dict[str, object]:
+    """Move a worker to ``draining`` permanently (the trigger forbids reactivation)."""
+    request = {"worker_id": worker_id}
+
+    with store.transaction(workspace_id, actor_id) as cur:
+
+        def apply() -> dict[str, object]:
+            cur.execute(
+                f"SELECT {_WORKER_FIELDS} FROM omp_jobs.workers WHERE workspace_id=%s AND worker_id=%s FOR UPDATE",  # nosec B608 - static column list
+                (workspace_id, worker_id),
+            )
+            existing = cur.fetchone()
+            if existing is None:
+                raise JobError("job_worker_unavailable", ("unknown worker",))
+            if existing["state"] == "draining":
+                return {"status": "replayed", "worker": row_json(existing)}
+            cur.execute(
+                f"""
+                UPDATE omp_jobs.workers SET state='draining', updated_at=clock_timestamp()
+                WHERE workspace_id=%s AND worker_id=%s
+                RETURNING {_WORKER_FIELDS}
+                """,  # nosec B608 - static column list
+                (workspace_id, worker_id),
+            )
+            return {"status": "applied", "worker": row_json(cur.fetchone())}
+
+        outcome = store.run_operation(
+            cur, operation_id, workspace_id, "worker_drain", request, apply
+        )
+        status = (
+            "replayed"
+            if outcome.state == "replayed" or outcome.result.get("status") == "replayed"
+            else "applied"
+        )
+        return {
+            "operation_id": operation_id,
+            "status": status,
+            "worker": outcome.result.get("worker"),
+        }
diff --git a/python/omp-work/src/omp_work/operations/jobs_migrations/0003_native_research_jobs.sql b/python/omp-work/src/omp_work/operations/jobs_migrations/0003_native_research_jobs.sql
new file mode 100644
index 0000000..e4e3dc2
--- /dev/null
+++ b/python/omp-work/src/omp_work/operations/jobs_migrations/0003_native_research_jobs.sql
@@ -0,0 +1,125 @@
+-- Additive native research jobs on the shared omp_jobs substrate (R03, OMP-324).
+-- The one substrate keeps working: a flood mirror row (source='flood_import',
+-- kind IS NULL) inserts and updates untouched, while a native research row
+-- (source='native') carries model|compute identity, capability routing,
+-- leasing/fencing, settlement, cancellation, an idempotent operation ledger,
+-- a durable outbox, worker registration, reservations, job events, and stable
+-- usage identities. omp_jobs has no RLS; every query filters workspace_id.
+
+ALTER TABLE omp_jobs.jobs
+    ADD COLUMN workspace_id uuid,
+    ADD COLUMN work_id uuid,
+    ADD COLUMN trial_id uuid,
+    ADD COLUMN parent_job_id text REFERENCES omp_jobs.jobs(job_id),
+    ADD COLUMN kind text,
+    ADD COLUMN required_capabilities jsonb,
+    ADD COLUMN resources jsonb,
+    ADD COLUMN lease_seconds integer,
+    ADD COLUMN fence integer NOT NULL DEFAULT 0,
+    ADD COLUMN attempt integer NOT NULL DEFAULT 0,
+    ADD COLUMN worker_id text,
+    ADD COLUMN lease_expires_at timestamptz,
+    ADD COLUMN settlement jsonb,
+    ADD COLUMN settled_at timestamptz,
+    ADD COLUMN cancel_reason text,
+    ADD COLUMN cancelled_at timestamptz,
+    ADD CONSTRAINT jobs_native_kind_check CHECK (kind IS NULL OR kind IN ('model','compute')),
+    ADD CONSTRAINT jobs_native_kind_source_check CHECK (kind IS NULL OR source = 'native'),
+    ADD CONSTRAINT jobs_required_capabilities_check CHECK (
+        required_capabilities IS NULL
+        OR (jsonb_typeof(required_capabilities) = 'array' AND octet_length(required_capabilities::text) <= 8192)
+    ),
+    ADD CONSTRAINT jobs_resources_check CHECK (
+        resources IS NULL
+        OR (jsonb_typeof(resources) = 'object' AND octet_length(resources::text) <= 8192)
+    ),
+    ADD CONSTRAINT jobs_lease_seconds_check CHECK (lease_seconds IS NULL OR lease_seconds BETWEEN 1 AND 3600),
+    ADD CONSTRAINT jobs_settlement_check CHECK (settlement IS NULL OR octet_length(settlement::text) <= 65536),
+    ADD CONSTRAINT jobs_cancel_reason_check CHECK (cancel_reason IS NULL OR octet_length(cancel_reason) <= 4096);
+
+CREATE TABLE omp_jobs.workers (
+    worker_id text PRIMARY KEY,
+    workspace_id uuid NOT NULL,
+    component_sha256 text NOT NULL CHECK (component_sha256 ~ '^[0-9a-f]{64}$'),
+    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
+        jsonb_typeof(capabilities) = 'array' AND octet_length(capabilities::text) <= 8192
+    ),
+    capacity integer NOT NULL CHECK (capacity >= 0),
+    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','draining')),
+    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
+    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
+);
+
+CREATE TABLE omp_jobs.operations (
+    operation_id text PRIMARY KEY,
+    workspace_id uuid NOT NULL,
+    kind text NOT NULL,
+    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
+    result jsonb NOT NULL CHECK (octet_length(result::text) <= 65536),
+    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
+);
+
+CREATE TABLE omp_jobs.outbox (
+    event_id text PRIMARY KEY,
+    workspace_id uuid NOT NULL,
+    operation_id text NOT NULL,
+    kind text NOT NULL,
+    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 65536),
+    state text NOT NULL DEFAULT 'open'
+        CHECK (state IN ('open','committed','acknowledged','closed','failed')),
+    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
+    ack_token text,
+    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
+    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
+    CONSTRAINT outbox_ack_token_check CHECK ((state IN ('acknowledged','closed')) = (ack_token IS NOT NULL))
+);
+
+ALTER TABLE omp_jobs.reservations
+    ADD COLUMN workspace_id uuid,
+    ADD COLUMN worker_id text,
+    ADD COLUMN fence integer NOT NULL DEFAULT 0,
+    ADD COLUMN resources jsonb,
+    ADD COLUMN released_at timestamptz,
+    ADD COLUMN release_reason text,
+    ADD CONSTRAINT reservations_resources_check CHECK (
+        resources IS NULL
+        OR (jsonb_typeof(resources) = 'object' AND octet_length(resources::text) <= 8192)
+    ),
+    ADD CONSTRAINT reservations_release_pair_check CHECK ((released_at IS NULL) = (release_reason IS NULL));
+
+ALTER TABLE omp_jobs.job_events
+    ADD COLUMN operation_id text,
+    ADD COLUMN payload jsonb,
+    ADD CONSTRAINT job_events_payload_check CHECK (payload IS NULL OR octet_length(payload::text) <= 65536);
+
+ALTER TABLE omp_jobs.usage_events
+    ADD COLUMN usage_id text,
+    ADD COLUMN workspace_id uuid,
+    ADD COLUMN work_id uuid,
+    ADD COLUMN request_id text,
+    ADD COLUMN model text,
+    ADD COLUMN input_tokens bigint,
+    ADD COLUMN output_tokens bigint,
+    ADD COLUMN cache_tokens bigint,
+    ADD COLUMN measurement text,
+    ADD COLUMN price_usd numeric(20,10),
+    ADD COLUMN price_version text,
+    ADD CONSTRAINT usage_events_usage_id_key UNIQUE (usage_id);
+
+-- Drain is permanent: no write path may move a worker back to active.
+CREATE OR REPLACE FUNCTION omp_control.reject_worker_reactivation() RETURNS trigger
+    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
+BEGIN
+    IF OLD.state = 'draining' AND NEW.state IS DISTINCT FROM 'draining' THEN
+        RAISE EXCEPTION 'drained worker cannot be reactivated';
+    END IF;
+    RETURN NEW;
+END $$;
+
+CREATE TRIGGER workers_no_reactivation BEFORE UPDATE ON omp_jobs.workers
+    FOR EACH ROW EXECUTE FUNCTION omp_control.reject_worker_reactivation();
+
+-- New tables: app reads and writes native job state. The tables 0001 created
+-- already carry ALL to app through its grants/default privileges, so only the
+-- tables added here need an explicit grant.
+GRANT SELECT, INSERT, UPDATE ON omp_jobs.workers, omp_jobs.operations, omp_jobs.outbox TO omp_work_app;

```