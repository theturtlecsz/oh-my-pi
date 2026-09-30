"""Worker loop and handler dispatch for native research jobs (R03, OMP-324, OMP-400).

Provides JobWorker to drive reconcile, claim, execute with renewal, settlement,
usage recording, and outbox delivery on the shared omp_jobs substrate.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import psycopg

from omp_work.jobs.admission import claim_job
from omp_work.jobs.lease import reconcile_jobs, renew_lease, settle_job
from omp_work.jobs.outbox import deliver_outbox
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.jobs.usage import record_usage
from omp_work.operations.database import _jobs_migration_state, check_migrations

__all__ = [
    "Handler",
    "JobWorker",
    "Settlement",
    "WorkerConfig",
    "load_handler",
]


@dataclass
class Settlement:
    outcome: str
    receipts: list[dict[str, object]]
    usage: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = []


@runtime_checkable
class Handler(Protocol):
    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        ...

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        ...


def load_handler(spec: dict[str, Any] | Handler) -> Handler:
    """Load a handler instance from a spec dict or return existing handler."""
    if isinstance(spec, Handler):
        return spec
    if not isinstance(spec, dict):
        raise TypeError("spec must be a dict or Handler")
    factory_str = spec.get("factory")
    if not isinstance(factory_str, str) or ":" not in factory_str:
        raise ValueError("factory must be in 'module:callable' format")
    module_name, callable_name = factory_str.split(":", 1)
    mod = importlib.import_module(module_name)
    factory = getattr(mod, callable_name)
    options = spec.get("options") or {}
    if not isinstance(options, dict):
        raise TypeError("options must be a dict")
    return factory(**options)


@dataclass
class WorkerConfig:
    """Configuration for native research job workers.

    Worker `capacity` is a resource-weight admission budget, not a concurrency setting; workers claim and execute one job at a time serially regardless of capacity.
    """

    workspace_id: UUID
    actor_id: UUID
    worker_id: str
    component_sha256: str
    capabilities: list[str] = field(default_factory=list)
    capacity: int = 1
    resource_limits: dict[str, int] | None = None
    ledger_path: Path | str | None = None


class JobWorker:
    """Production driver for the native jobs substrate.

    Worker `capacity` is a resource-weight admission budget, not a concurrency setting; `tick` claims and runs one job at a time and `run_forever` loops tick serially.
    """

    def __init__(
        self,
        store: NativeJobStore,
        config: WorkerConfig | dict[str, Any] | Any,
        handlers: Any,
    ) -> None:
        self.store = store
        self.config = config
        self._started = False

        def _get(key: str, default: Any = None) -> Any:
            if isinstance(config, dict):
                return config.get(key, default)
            return getattr(config, key, default)

        ws = _get("workspace_id")
        self.workspace_id = ws if isinstance(ws, UUID) else UUID(str(ws))
        actor = _get("actor_id")
        self.actor_id = actor if isinstance(actor, UUID) else UUID(str(actor))
        self.worker_id = str(_get("worker_id"))
        self.component_sha256 = str(_get("component_sha256"))
        self.capabilities = list(_get("capabilities") or [])
        self.capacity = int(_get("capacity", 1))
        self.resource_limits = _get("resource_limits", None)
        self.ledger_path = _get("ledger_path", None)

        if isinstance(handlers, dict):
            self.handlers = {
                k: load_handler(v) if isinstance(v, dict) and "factory" in v else v
                for k, v in handlers.items()
            }
        elif hasattr(handlers, "run"):
            self.handlers = {"*": handlers}
        else:
            self.handlers = handlers

    @property
    def current_resource_limits(self) -> dict[str, int] | None:
        if isinstance(self.config, dict):
            return self.config.get("resource_limits", self.resource_limits)
        return getattr(self.config, "resource_limits", self.resource_limits)

    @property
    def current_ledger_path(self) -> Path | str | None:
        if isinstance(self.config, dict):
            return self.config.get("ledger_path", self.ledger_path)
        return getattr(self.config, "ledger_path", self.ledger_path)

    def _migrator_kwargs(self) -> dict[str, object]:
        if hasattr(self.store, "_config") and hasattr(self.store._config, "connection_kwargs"):
            return self.store._config.connection_kwargs("omp_work_migrator")
        if hasattr(self.config, "connection_kwargs"):
            return self.config.connection_kwargs("omp_work_migrator")
        raise RuntimeError("Cannot determine database connection configuration for migrator")

    def start(self) -> dict[str, object]:
        """Verify schema migrations and register the worker."""
        migrator_kwargs = self._migrator_kwargs()
        with psycopg.connect(**migrator_kwargs) as conn:
            with conn.cursor() as cur:
                jobs_pending, jobs_drift = _jobs_migration_state(cur)
            if jobs_pending or jobs_drift:
                raise RuntimeError(
                    f"jobs migration pending or drift: pending={jobs_pending}, drift={jobs_drift}"
                )
            try:
                check_migrations(conn, allow_pending=True)
            except ValueError as err:
                raise RuntimeError(f"migrations check failed: {err}") from err

        result = register_worker(
            self.store,
            operation_id=str(uuid4()),
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            worker_id=self.worker_id,
            component_sha256=self.component_sha256,
            capabilities=self.capabilities,
            capacity=self.capacity,
        )
        self._started = True
        return result

    def _resolve_handler(self, job: dict[str, Any]) -> Handler:
        if hasattr(self.handlers, "run"):
            return self.handlers
        if isinstance(self.handlers, dict):
            kind = job.get("kind")
            if kind and kind in self.handlers:
                return self.handlers[kind]
            if "*" in self.handlers:
                return self.handlers["*"]
            if "default" in self.handlers:
                return self.handlers["default"]
            if len(self.handlers) == 1:
                return next(iter(self.handlers.values()))
        raise RuntimeError(f"No handler found for job {job.get('job_id')} with kind {job.get('kind')}")

    def tick(self) -> str | None:
        """Run one worker cycle: reconcile, claim, execute with renewal, settle, and drain outbox."""
        reconcile_jobs(
            self.store,
            operation_id=str(uuid4()),
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
        )
        claimed = claim_job(
            self.store,
            operation_id=str(uuid4()),
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            worker_id=self.worker_id,
            resource_limits=self.current_resource_limits,
        )
        job = claimed.get("job")
        if job is None:
            return None

        job_id = str(job["job_id"])
        fence = int(job["fence"])
        lease_seconds = int(job.get("lease_seconds") or 30)

        handler = self._resolve_handler(job)

        with self.store.transaction(self.workspace_id, self.actor_id) as cur:
            cur.execute(
                "SELECT 1 FROM omp_jobs.job_events WHERE job_id=%s AND kind='lease_expired' LIMIT 1",
                (job_id,),
            )
            has_expired = cur.fetchone() is not None

        settlement: Settlement | None = None
        if has_expired:
            settlement = handler.observe(self, job)

        if settlement is None:
            stop_renew = threading.Event()
            renewal_failed = threading.Event()

            def _renew_loop() -> None:
                interval = max(0.01, lease_seconds / 3.0)
                while not stop_renew.wait(timeout=interval):
                    try:
                        renew_lease(
                            self.store,
                            operation_id=str(uuid4()),
                            workspace_id=self.workspace_id,
                            actor_id=self.actor_id,
                            job_id=job_id,
                            worker_id=self.worker_id,
                            fence=fence,
                        )
                    except Exception:  # noqa: BLE001 - renewal failure indicates lost lease or cancelled job
                        renewal_failed.set()
                        break

            thread = threading.Thread(target=_renew_loop, daemon=True)
            thread.start()
            try:
                settlement = handler.run(self, job)
            finally:
                stop_renew.set()
                thread.join()

            if renewal_failed.is_set():
                return job_id

        if settlement is not None:
            for n, usage_item in enumerate(settlement.usage):
                item_kwargs = dict(usage_item)
                item_kwargs.pop("ledger_path", None)
                if "job_id" not in item_kwargs:
                    item_kwargs["job_id"] = job_id
                if "workspace_id" not in item_kwargs:
                    item_kwargs["workspace_id"] = self.workspace_id
                if "actor_id" not in item_kwargs:
                    item_kwargs["actor_id"] = self.actor_id
                if "work_id" not in item_kwargs and job.get("work_id"):
                    item_kwargs["work_id"] = job["work_id"]
                record_usage(
                    self.store,
                    operation_id=f"usage:{job_id}:{fence}:{n}",
                    **item_kwargs,
                )

            settle_job(
                self.store,
                operation_id=f"settle:{job_id}:{fence}",
                workspace_id=self.workspace_id,
                actor_id=self.actor_id,
                job_id=job_id,
                worker_id=self.worker_id,
                fence=fence,
                outcome=settlement.outcome,
                receipts=settlement.receipts,
            )

            ledger_path = self.current_ledger_path
            if ledger_path is not None:
                deliver_outbox(
                    self.store,
                    workspace_id=self.workspace_id,
                    actor_id=self.actor_id,
                    ledger_path=ledger_path,
                )

        return job_id

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        idle_sleep: float = 0.1,
    ) -> None:
        """Run the worker loop continuously until stop_event is set."""
        if not self._started:
            self.start()
        if stop_event is None:
            stop_event = threading.Event()
        while not stop_event.is_set():
            job_id = self.tick()
            if job_id is None:
                stop_event.wait(timeout=idle_sleep)
