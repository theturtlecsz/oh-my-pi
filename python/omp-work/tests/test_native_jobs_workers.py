"""Native research jobs on the shared omp_jobs substrate (R03, OMP-324).

Contract under test: research jobs are native ``omp_jobs.jobs`` rows beside the
flood mirror on the same tables; the operation ledger makes a committed-but-
unacknowledged retry replay instead of running twice; a worker binds to its
registered R02 ``worker`` component and can only drain, never reactivate.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omp_work.contracts.v1.recovery import CloseoutState
from omp_work.jobs.store import (
    JobError,
    NativeJobStore,
    drain_worker,
    register_worker,
)
from native_jobs_support import native_jobs  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _job_columns(service) -> set[str]:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='omp_jobs' AND table_name='jobs'"
        ).fetchall()
    return {row[0] for row in rows}


def _worker_columns(service) -> set[str]:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='omp_jobs' AND table_name='workers'"
        ).fetchall()
    return {row[0] for row in rows}


def _insert_native_job(
    native_jobs,
    *,
    job_id: str,
    kind: str = "compute",
    parent_job_id: str | None = None,
    **columns: object,
) -> None:
    fields = {
        "job_id": job_id,
        "workspace_id": native_jobs.workspace_id,
        "work_id": UUID(native_jobs.item["work_id"]),
        "trial_id": native_jobs.trial_id,
        "parent_job_id": parent_job_id,
        "kind": kind,
        "status": "backlog",
        "source": "native",
        "required_capabilities": Jsonb(["compute.gpu"]),
        **columns,
    }
    names = ",".join(fields)
    placeholders = ",".join(["%s"] * len(fields))
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            f"INSERT INTO omp_jobs.jobs({names}) VALUES ({placeholders})",  # nosec B608 - test-built
            tuple(fields.values()),
        )


def test_flood_import_row_still_inserts_untouched_beside_native_rows(native_jobs) -> None:
    """One substrate: a mirror row (kind NULL, flood_import) and a native row coexist."""
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source,idempotency_key) VALUES('mirror-1','sealed','flood_import','flood:mirror-1')"
        )
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source,kind,workspace_id) VALUES('native-1','backlog','native','compute',%s)",
            (native_jobs.workspace_id,),
        )
        conn.execute("UPDATE omp_jobs.jobs SET status='in_flight' WHERE job_id='mirror-1'")
        rows = {
            job_id: (source, kind)
            for job_id, source, kind in conn.execute(
                "SELECT job_id, source, kind FROM omp_jobs.jobs WHERE job_id IN ('mirror-1','native-1')"
            ).fetchall()
        }
    assert rows == {"mirror-1": ("flood_import", None), "native-1": ("native", "compute")}
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        stored = conn.execute(
            "SELECT source,kind,status FROM omp_jobs.jobs WHERE job_id='native-1'"
        ).fetchone()
        mirror = conn.execute(
            "SELECT source,kind,status FROM omp_jobs.jobs WHERE job_id='mirror-1'"
        ).fetchone()
    assert stored == ("native", "compute", "backlog")
    assert mirror == ("flood_import", None, "in_flight")


def test_native_kind_requires_native_source(native_jobs) -> None:
    """kind is set only on native rows; a flood_import row cannot claim a kind."""
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="jobs_native_kind_source_check"):
            conn.execute(
                "INSERT INTO omp_jobs.jobs(job_id,status,source,kind) VALUES('bad-kind','backlog','flood_import','model')"
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="jobs_native_kind_check"):
            conn.execute(
                "INSERT INTO omp_jobs.jobs(job_id,status,source,kind) VALUES('bad-kind-2','backlog','native','batch')"
            )


def test_native_job_lease_bounds_and_defaults(native_jobs) -> None:
    """lease_seconds is 1..3600; fence and attempt default to 0."""
    _insert_native_job(native_jobs, job_id="lease-ok", lease_seconds=1, fence=3)
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        view = store.job_view(cur, native_jobs.workspace_id, "lease-ok")
    assert view["lease_seconds"] == 1
    assert view["fence"] == 3
    assert view["attempt"] == 0

    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        for bad in (0, 3601):
            with pytest.raises(psycopg.errors.CheckViolation, match="jobs_lease_seconds_check"):
                conn.execute(
                    "INSERT INTO omp_jobs.jobs(job_id,status,source,lease_seconds) VALUES(%s,'backlog','native',%s)",
                    (f"lease-bad-{bad}", bad),
                )


def test_job_view_exposes_every_jobs_column(native_jobs) -> None:
    """job_view returns the whole row, not a hand-picked projection."""
    _insert_native_job(native_jobs, job_id="view-1", lease_seconds=60)
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        view = store.job_view(cur, native_jobs.workspace_id, "view-1")
        missing = store.job_view(cur, uuid4(), "view-1")
    assert view is not None
    assert set(view) == _job_columns(native_jobs.service)
    assert view["workspace_id"] == str(native_jobs.workspace_id)
    assert view["kind"] == "compute"
    assert missing is None


def test_run_operation_replays_same_request_and_refuses_changed_request(native_jobs) -> None:
    """Committed-but-unacknowledged retry replays the stored result; a different
    request under the same operation id is an idempotency_conflict."""
    _insert_native_job(native_jobs, job_id="op-job")
    store = _store(native_jobs)
    effects: list[int] = []

    def apply() -> dict[str, object]:
        effects.append(1)
        return {"status": "applied", "job_id": "op-job"}

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        first = store.run_operation(
            cur, "op-1", native_jobs.workspace_id, "job_cancel", {"reason": "stop"}, apply
        )
        replay = store.run_operation(
            cur, "op-1", native_jobs.workspace_id, "job_cancel", {"reason": "stop"}, apply
        )
        with pytest.raises(JobError) as refused:
            store.run_operation(
                cur, "op-1", native_jobs.workspace_id, "job_cancel", {"reason": "other"}, apply
            )
    assert first.state == "applied" and first.result == {"status": "applied", "job_id": "op-job"}
    assert replay.state == "replayed" and replay.result == first.result
    assert refused.value.code == "idempotency_conflict"
    assert effects == [1]

    # The replay survives a fresh store/connection, i.e. it is durable, not cached.
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        again = store.run_operation(
            cur, "op-1", native_jobs.workspace_id, "job_cancel", {"reason": "stop"}, apply
        )
    assert again.state == "replayed" and effects == [1]


def test_append_event_sequences_monotonically(native_jobs) -> None:
    """append_event assigns a per-job sequence and keeps operation_id/payload."""
    _insert_native_job(native_jobs, job_id="events-1")
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        first = store.append_event(
            cur,
            workspace_id=native_jobs.workspace_id,
            job_id="events-1",
            kind="queued",
            actor="worker-1",
            operation_id="op-ev-1",
            payload={"attempt": 0},
        )
        second = store.append_event(
            cur,
            workspace_id=native_jobs.workspace_id,
            job_id="events-1",
            kind="admitted",
            operation_id="op-ev-1",
        )
        cur.execute(
            "SELECT seq,kind,operation_id,payload FROM omp_jobs.job_events WHERE job_id='events-1' ORDER BY seq"
        )
        rows = cur.fetchall()
    assert (first, second) == (1, 2)
    assert [(row["seq"], row["kind"]) for row in rows] == [(1, "queued"), (2, "admitted")]
    assert all(row["operation_id"] == "op-ev-1" for row in rows)
    assert rows[0]["payload"] == {"attempt": 0}


def test_cancelled_ancestor_finds_nearest_cancelled_parent(native_jobs) -> None:
    """Cancellation is inherited: the walk reports the nearest cancelled ancestor."""
    _insert_native_job(native_jobs, job_id="root", status="cancelled", cancel_reason="campaign")
    _insert_native_job(native_jobs, job_id="child", parent_job_id="root", status="admitted")
    _insert_native_job(native_jobs, job_id="grandchild", parent_job_id="child", status="backlog")
    _insert_native_job(native_jobs, job_id="orphan", status="backlog")
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        grandchild = store.cancelled_ancestor(cur, native_jobs.workspace_id, "grandchild")
        root = store.cancelled_ancestor(cur, native_jobs.workspace_id, "root")
        orphan = store.cancelled_ancestor(cur, native_jobs.workspace_id, "orphan")
        other = store.cancelled_ancestor(cur, uuid4(), "grandchild")
    assert grandchild is not None and grandchild["job_id"] == "root"
    assert grandchild["status"] == "cancelled"
    # A job's own status is not an ancestor: the root reports no cancelled parent.
    assert root is None
    assert orphan is None
    assert other is None


def test_register_worker_sorts_capabilities_within_descriptor_and_replays(native_jobs) -> None:
    """Registration binds the R02 worker component; capabilities are sorted and
    must sit inside its descriptor. Identical re-registration replays."""
    store = _store(native_jobs)
    worker_component = native_jobs.components["worker"]
    first = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-a",
        component_sha256=worker_component,
        capabilities=["compute.gpu", "compute.cpu"],
        capacity=4,
    )
    assert first["status"] == "applied"
    assert first["worker"]["capabilities"] == ["compute.cpu", "compute.gpu"]
    assert first["worker"]["state"] == "active"
    assert set(first["worker"]) == _worker_columns(native_jobs.service)

    replay = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-a",
        component_sha256=worker_component,
        capabilities=["compute.cpu", "compute.gpu"],
        capacity=4,
    )
    assert replay["status"] == "replayed"
    assert replay["worker"]["worker_id"] == "worker-a"


def test_register_worker_refuses_unavailable_worker(native_jobs) -> None:
    """Outside-descriptor capabilities, unknown component, wrong kind, and a
    changed identity are all job_worker_unavailable (not a silent re-register)."""
    store = _store(native_jobs)
    worker_component = native_jobs.components["worker"]
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-b",
        component_sha256=worker_component,
        capabilities=["compute.gpu"],
        capacity=2,
    )
    for kwargs, match in (
        ({"capabilities": ["compute.gpu", "compute.tpu"], "capacity": 2}, "outside descriptor"),
        ({"component_sha256": native_jobs.components["evaluator"], "capabilities": [], "capacity": 1}, "not a worker component"),
        ({"component_sha256": "0" * 64, "capabilities": [], "capacity": 1}, "unknown worker component"),
    ):
        with pytest.raises(JobError) as refused:
            register_worker(
                store,
                operation_id=str(uuid4()),
                workspace_id=native_jobs.workspace_id,
                actor_id=native_jobs.actor_id,
                worker_id="worker-b",
                capabilities=kwargs["capabilities"],
                component_sha256=kwargs.get("component_sha256", worker_component),
                capacity=kwargs["capacity"],
            )
        assert refused.value.code == "job_worker_unavailable"
        assert any(match in item for item in refused.value.diagnostics)

    with pytest.raises(JobError) as changed:
        register_worker(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id="worker-b",
            component_sha256=worker_component,
            capabilities=["compute.gpu"],
            capacity=8,
        )
    assert changed.value.code == "job_worker_unavailable"
    assert any("identity changed" in item for item in changed.value.diagnostics)


def test_drain_worker_is_permanent(native_jobs) -> None:
    """drain_worker moves a worker to draining; it can never reactivate."""
    store = _store(native_jobs)
    worker_component = native_jobs.components["worker"]
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-c",
        component_sha256=worker_component,
        capabilities=["compute.gpu"],
        capacity=1,
    )
    drained = drain_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-c",
    )
    assert drained["status"] == "applied"
    assert drained["worker"]["state"] == "draining"

    replay = drain_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-c",
    )
    assert replay["status"] == "replayed"

    with pytest.raises(JobError) as refused:
        register_worker(
            store,
            operation_id=str(uuid4()),
            workspace_id=native_jobs.workspace_id,
            actor_id=native_jobs.actor_id,
            worker_id="worker-c",
            component_sha256=worker_component,
            capabilities=["compute.gpu"],
            capacity=1,
        )
    assert refused.value.code == "job_worker_unavailable"

    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("postgres")
    ) as conn:
        with conn.cursor() as cur:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="drained worker cannot be reactivated",
            ):
                cur.execute(
                    "UPDATE omp_jobs.workers SET state='active' WHERE worker_id='worker-c'"
                )
        conn.rollback()


def test_reservations_carry_native_binding_and_release_pair(native_jobs) -> None:
    """Reservations bind workspace/worker/fence/resources and release atomically."""
    _insert_native_job(native_jobs, job_id="res-job")
    now = datetime.now(UTC)
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            """
            INSERT INTO omp_jobs.reservations(
                reservation_id, job_id, tokens, workspace_id, worker_id, fence, resources
            ) VALUES ('rsv-1','res-job',25000,%s,'worker-a',1,%s)
            """,
            (native_jobs.workspace_id, Jsonb({"cpu": 2})),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="reservations_release_pair_check"):
            conn.execute(
                "UPDATE omp_jobs.reservations SET released_at=%s WHERE reservation_id='rsv-1'",
                (now,),
            )
        conn.execute(
            "UPDATE omp_jobs.reservations SET released_at=%s, release_reason='settled' WHERE reservation_id='rsv-1'",
            (now + timedelta(seconds=1),),
        )
        row = conn.execute(
            "SELECT workspace_id, worker_id, fence, resources, release_reason FROM omp_jobs.reservations WHERE reservation_id='rsv-1'"
        ).fetchone()
    assert row[0] == native_jobs.workspace_id
    assert row[1] == "worker-a" and row[2] == 1
    assert row[3] == {"cpu": 2}
    assert row[4] == "settled"


def test_usage_events_usage_id_is_unique_and_carries_native_identity(native_jobs) -> None:
    """WP5 accounting keeps a stable usage identity beside the file ledger."""
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            """
            INSERT INTO omp_jobs.usage_events(
                conversation_id, step_index, source_file, usage_id, workspace_id, work_id,
                request_id, model, input_tokens, output_tokens, cache_tokens,
                measurement, price_usd, price_version
            ) VALUES ('conv-1',1,'usage.json','usage-1',%s,%s,'req-1','gemini',10,20,5,'measured',0.0125,'v1')
            """,
            (native_jobs.workspace_id, UUID(native_jobs.item["work_id"])),
        )
        with pytest.raises(psycopg.errors.UniqueViolation, match="usage_events_usage_id_key"):
            conn.execute(
                """
                INSERT INTO omp_jobs.usage_events(
                    conversation_id, step_index, source_file, usage_id, workspace_id
                ) VALUES ('conv-2',1,'usage.json','usage-1',%s)
                """,
                (native_jobs.workspace_id,),
            )
        row = conn.execute(
            "SELECT model, input_tokens, output_tokens, cache_tokens, measurement, price_version FROM omp_jobs.usage_events WHERE usage_id='usage-1'"
        ).fetchone()
    assert row == ("gemini", 10, 20, 5, "measured", "v1")


def test_outbox_states_match_closeout_recovery_vocabulary(native_jobs) -> None:
    """The outbox state check admits exactly recovery.py's CloseoutState literals
    and rejects anything else, so the durable outbox cannot drift from recovery."""
    declared = set(get_args(CloseoutState))
    assert declared == {"open", "committed", "acknowledged", "closed", "failed"}
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        for state in sorted(declared):
            token = "ack-1" if state in {"acknowledged", "closed"} else None
            conn.execute(
                """
                INSERT INTO omp_jobs.outbox(event_id, workspace_id, operation_id, kind, payload, state, revision, ack_token)
                VALUES (%s, %s, 'op-outbox', 'closeout', %s, %s, 1, %s)
                """,
                (f"event-{state}", native_jobs.workspace_id, Jsonb({"state": state}), state, token),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO omp_jobs.outbox(event_id, workspace_id, operation_id, kind, payload, state) VALUES ('event-bad', %s, 'op-outbox', 'closeout', '{}', 'recovering')",
                (native_jobs.workspace_id,),
            )
        rows = conn.execute(
            "SELECT state, ack_token FROM omp_jobs.outbox ORDER BY event_id"
        ).fetchall()
    assert {row[0] for row in rows} == declared
    assert {state: token for state, token in rows if token is not None} == {
        "acknowledged": "ack-1",
        "closed": "ack-1",
    }
