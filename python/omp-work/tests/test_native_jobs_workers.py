"""Native research jobs on the shared omp_jobs substrate (R03, OMP-324).

Contract under test: research jobs are native ``omp_jobs.jobs`` rows beside the
flood mirror on the same tables; the operation ledger makes a committed-but-
unacknowledged retry replay instead of running twice, and never replays another
workspace's result; a worker binds to its registered R02 ``worker`` component
and can only drain, never reactivate; a job-less trial projects as queued and
ineligible but still binds, while a trial with native jobs binds only once
those jobs are settled; usage is written to omp_jobs and the file ledger once.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    project_trial,
    record_usage,
    register_worker,
)
from omp_work.v1.canonical import sha256
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from test_research_contract import _plan_and_finalize, _trial_payload
from test_workflow_service import _command

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


def _propose_trial(native_jobs, *, candidate_digest: str = "c" * 64) -> UUID:
    trial_id = uuid4()
    status, body = _command(
        native_jobs.service,
        native_jobs.workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                native_jobs.campaign_id,
                native_jobs.item["work_id"],
                native_jobs.components["policy"],
                native_jobs.components["evaluator"],
                native_jobs.components["environment"],
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )
    assert status == 200, body
    return trial_id


def _bind_deliverable(native_jobs, trial_id: UUID, candidate_id: UUID) -> tuple[int, dict]:
    candidate_digest = "c" * 64
    identity = {
        "candidate_digest": candidate_digest,
        "campaign_id": str(native_jobs.campaign_id),
        "native_candidate_id": str(candidate_id),
        "revision_id": native_jobs.item["revision_id"],
        "trial_id": str(trial_id),
        "work_id": native_jobs.item["work_id"],
        "workspace_id": str(native_jobs.workspace_id),
    }
    return _command(
        native_jobs.service,
        native_jobs.workspace_id,
        {
            "type": "bind_research_deliverable",
            "payload": {
                "trial_id": str(trial_id),
                "campaign_id": str(native_jobs.campaign_id),
                "work_id": native_jobs.item["work_id"],
                "revision_id": native_jobs.item["revision_id"],
                "candidate_digest": candidate_digest,
                "native_candidate_id": str(candidate_id),
                "binding_sha256": sha256(identity),
            },
        },
    )


def _project(native_jobs, trial_id: UUID) -> dict[str, object]:
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        return project_trial(cur, native_jobs.workspace_id, trial_id)


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
    trial_id: UUID | None = None,
    **columns: object,
) -> None:
    fields = {
        "job_id": job_id,
        "workspace_id": native_jobs.workspace_id,
        "work_id": UUID(native_jobs.item["work_id"]),
        "trial_id": native_jobs.trial_id if trial_id is None else trial_id,
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


def test_run_operation_refuses_another_workspaces_operation(native_jobs) -> None:
    """The same operation id in a second workspace is a conflict, not a replay."""
    store = _store(native_jobs)
    other = uuid4()
    effects: list[str] = []

    def apply() -> dict[str, object]:
        effects.append("ran")
        return {"status": "applied"}

    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        first = store.run_operation(
            cur, "op-ws", native_jobs.workspace_id, "job_cancel", {"reason": "stop"}, apply
        )
    assert first.state == "applied"
    with store.transaction(other, native_jobs.actor_id) as cur:
        with pytest.raises(JobError) as refused:
            store.run_operation(
                cur, "op-ws", other, "job_cancel", {"reason": "stop"}, apply
            )
    assert refused.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in refused.value.diagnostics)
    assert effects == ["ran"]
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        replay = store.run_operation(
            cur, "op-ws", native_jobs.workspace_id, "job_cancel", {"reason": "stop"}, apply
        )
    assert replay.state == "replayed"
    assert effects == ["ran"]


def test_append_event_stays_inside_the_native_workspace(native_jobs) -> None:
    """Events land only on a native job in the caller's workspace."""
    _insert_native_job(native_jobs, job_id="events-home")
    other = uuid4()
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source,kind,workspace_id) VALUES('events-other','backlog','native','compute',%s)",
            (other,),
        )
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source) VALUES('events-flood','sealed','flood_import')"
        )
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        seq = store.append_event(
            cur,
            workspace_id=native_jobs.workspace_id,
            job_id="events-home",
            kind="queued",
        )
        for job_id in ("events-other", "events-flood", "events-missing"):
            with pytest.raises(JobError) as refused:
                store.append_event(
                    cur,
                    workspace_id=native_jobs.workspace_id,
                    job_id=job_id,
                    kind="queued",
                )
            assert refused.value.code == "invalid_request"
            assert any("not a native job" in item for item in refused.value.diagnostics)
        cur.execute(
            "SELECT job_id FROM omp_jobs.job_events WHERE job_id IN ('events-home','events-other','events-flood','events-missing')"
        )
        written = {row["job_id"] for row in cur.fetchall()}
    assert seq == 1
    assert written == {"events-home"}


def test_worker_handshake_does_not_replay_across_workspaces(native_jobs) -> None:
    """A second workspace reusing the handshake operation id does not replay."""
    store = _store(native_jobs)
    other = uuid4()
    component = native_jobs.components["worker"]
    register_id = str(uuid4())
    drain_id = str(uuid4())
    registered = register_worker(
        store,
        operation_id=register_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-iso",
        component_sha256=component,
        capabilities=["compute.gpu", "compute.cpu"],
        capacity=2,
    )
    assert registered["status"] == "applied"
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        stored = conn.execute(
            "SELECT request_sha256 FROM omp_jobs.operations WHERE operation_id=%s",
            (register_id,),
        ).fetchone()
    assert stored[0] == sha256(
        {
            "workspace_id": str(native_jobs.workspace_id),
            "worker_id": "worker-iso",
            "component_sha256": component,
            "capabilities": ["compute.cpu", "compute.gpu"],
            "capacity": 2,
        }
    )
    with pytest.raises(JobError) as refused:
        register_worker(
            store,
            operation_id=register_id,
            workspace_id=other,
            actor_id=native_jobs.actor_id,
            worker_id="worker-iso",
            component_sha256=component,
            capabilities=["compute.gpu", "compute.cpu"],
            capacity=2,
        )
    assert refused.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in refused.value.diagnostics)
    with store.transaction(other, native_jobs.actor_id) as cur:
        assert store.worker_view(cur, other, "worker-iso") is None

    drained = drain_worker(
        store,
        operation_id=drain_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-iso",
    )
    assert drained["status"] == "applied"
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        drain_hash = conn.execute(
            "SELECT request_sha256 FROM omp_jobs.operations WHERE operation_id=%s",
            (drain_id,),
        ).fetchone()
    assert drain_hash[0] == sha256(
        {"workspace_id": str(native_jobs.workspace_id), "worker_id": "worker-iso"}
    )
    with pytest.raises(JobError) as drain_refused:
        drain_worker(
            store,
            operation_id=drain_id,
            workspace_id=other,
            actor_id=native_jobs.actor_id,
            worker_id="worker-iso",
        )
    assert drain_refused.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in drain_refused.value.diagnostics)
    replay = drain_worker(
        store,
        operation_id=drain_id,
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        worker_id="worker-iso",
    )
    assert replay["status"] == "replayed"
    assert replay["worker"]["state"] == "draining"


def test_jobless_trial_projects_queued_and_ineligible(native_jobs) -> None:
    """A trial with no native jobs is queued and ineligible.

    A native job for the same trial in another workspace does not count.
    """
    trial = _propose_trial(native_jobs)
    other = uuid4()
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source,kind,workspace_id,trial_id) VALUES('proj-foreign','sealed','native','model',%s,%s)",
            (other, trial),
        )
    view = _project(native_jobs, trial)
    assert view == {
        "trial_id": str(trial),
        "status": "queued",
        "eligible": False,
        "job_count": 0,
    }


def test_trial_projection_maps_job_status_and_settlement(native_jobs) -> None:
    """Projection reuses the omp_jobs vocabulary and settles only a sealed job
    that has both settlement and settled_at."""
    trial = _propose_trial(native_jobs)
    _insert_native_job(native_jobs, job_id="proj-1", trial_id=trial, status="backlog")
    assert _project(native_jobs, trial)["status"] == "queued"
    assert _project(native_jobs, trial)["eligible"] is False

    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        for status, projected in (
            ("admitted", "leased"),
            ("in_flight", "leased"),
            ("returned", "leased"),
            ("checking", "leased"),
            ("failed", "failed"),
        ):
            conn.execute("UPDATE omp_jobs.jobs SET status=%s WHERE job_id='proj-1'", (status,))
            view = _project(native_jobs, trial)
            assert view["status"] == projected
            assert view["eligible"] is False

        conn.execute(
            "UPDATE omp_jobs.jobs SET status='sealed', settlement=NULL, settled_at=NULL WHERE job_id='proj-1'"
        )
        sealed = _project(native_jobs, trial)
        assert sealed["status"] == "succeeded"
        assert sealed["eligible"] is False

        conn.execute(
            "UPDATE omp_jobs.jobs SET settlement=%s WHERE job_id='proj-1'",
            (Jsonb({"outcome": "succeeded"}),),
        )
        assert _project(native_jobs, trial)["eligible"] is False

        conn.execute(
            "UPDATE omp_jobs.jobs SET settled_at=%s WHERE job_id='proj-1'",
            (datetime.now(UTC),),
        )
        settled = _project(native_jobs, trial)
        assert settled["status"] == "succeeded"
        assert settled["eligible"] is True
        assert settled["job_count"] == 1

        conn.execute(
            "INSERT INTO omp_jobs.jobs(job_id,status,source,kind,workspace_id,trial_id) VALUES('proj-2','failed','native','compute',%s,%s)",
            (native_jobs.workspace_id, trial),
        )
        mixed = _project(native_jobs, trial)
        assert mixed["status"] == "failed"
        assert mixed["eligible"] is False
        assert mixed["job_count"] == 2

        conn.execute("UPDATE omp_jobs.jobs SET status='cancelled' WHERE job_id='proj-2'")
        assert _project(native_jobs, trial)["status"] == "cancelled"

        conn.execute("UPDATE omp_jobs.jobs SET status='backlog' WHERE job_id='proj-2'")
        queued = _project(native_jobs, trial)
        assert queued["status"] == "queued"
        assert queued["eligible"] is False


def test_binding_requires_settled_native_jobs_and_keeps_jobless_trials(native_jobs) -> None:
    """Trials with native jobs bind only when those jobs are settled.

    A job-less trial still projects as queued and ineligible, and still binds
    through the current R02 path.
    """
    jobless = _propose_trial(native_jobs)
    gated = _propose_trial(native_jobs)
    _insert_native_job(native_jobs, job_id="bind-open", trial_id=gated, status="sealed")
    assert _project(native_jobs, jobless)["status"] == "queued"
    assert _project(native_jobs, jobless)["eligible"] is False
    opened = _project(native_jobs, gated)
    assert opened["status"] == "succeeded"
    assert opened["eligible"] is False

    candidate_id = _plan_and_finalize(
        native_jobs.service, native_jobs.workspace_id, native_jobs.item
    )
    status, body = _bind_deliverable(native_jobs, gated, candidate_id)
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("not settled" in item for item in body["error"]["diagnostics"])
    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        bound = conn.execute(
            "SELECT trial_id FROM omp_research.deliverable_bindings WHERE trial_id=%s",
            (gated,),
        ).fetchone()
    assert bound is None

    status, body = _bind_deliverable(native_jobs, jobless, candidate_id)
    assert status == 200, body
    assert body["result"]["status"] == "applied"

    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET settlement=%s, settled_at=%s WHERE job_id='bind-open'",
            (Jsonb({"outcome": "succeeded"}), datetime.now(UTC)),
        )
    settled = _project(native_jobs, gated)
    assert settled["status"] == "succeeded"
    assert settled["eligible"] is True
    status, body = _bind_deliverable(native_jobs, gated, candidate_id)
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["deliverable_binding"]["trial_id"] == str(gated)


def test_record_usage_writes_the_db_and_file_ledger_once(native_jobs, tmp_path) -> None:
    """WP5 accounting appends omp_jobs.usage_events and the file ledger together.

    Replaying the operation does not write either a second time. Another
    workspace reusing the operation id conflicts.
    """
    ledger = tmp_path / "usage" / "ledger.json"
    store = _store(native_jobs)
    kwargs = {
        "operation_id": "usage-op-1",
        "workspace_id": native_jobs.workspace_id,
        "actor_id": native_jobs.actor_id,
        "ledger_path": ledger,
        "usage_id": "usage-native-1",
        "work_id": UUID(native_jobs.item["work_id"]),
        "request_id": "req-usage-1",
        "model": "gemini",
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_tokens": 5,
        "measurement": "measured",
        "price_usd": Decimal("0.0125"),
        "price_version": "v1",
        "conversation_id": "conv-usage-1",
        "step_index": 1,
        "source_file": "usage.json",
    }
    first = record_usage(store, **kwargs)
    assert first["status"] == "applied"
    replay = record_usage(store, **kwargs)
    assert replay["status"] == "replayed"
    events = json.loads(ledger.read_text())["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "native_job_usage"
    assert events[0]["usage_id"] == "usage-native-1"
    assert events[0]["input_tokens"] == 10
    assert events[0]["workspace_id"] == str(native_jobs.workspace_id)

    with psycopg.connect(**native_jobs.service.config.connection_kwargs("postgres")) as conn:
        row = conn.execute(
            """
            SELECT workspace_id, work_id, model, input_tokens, output_tokens, cache_tokens,
                   measurement, price_usd, price_version
            FROM omp_jobs.usage_events WHERE usage_id='usage-native-1'
            """
        ).fetchone()
    assert row[0] == native_jobs.workspace_id
    assert row[1] == UUID(native_jobs.item["work_id"])
    assert row[2:] == ("gemini", 10, 20, 5, "measured", Decimal("0.0125"), "v1")

    changed = dict(kwargs)
    changed["price_usd"] = Decimal("9")
    with pytest.raises(JobError) as refused:
        record_usage(store, **changed)
    assert refused.value.code == "idempotency_conflict"
    assert len(json.loads(ledger.read_text())["events"]) == 1

    other = dict(kwargs)
    other["workspace_id"] = uuid4()
    with pytest.raises(JobError) as cross:
        record_usage(store, **other)
    assert cross.value.code == "idempotency_conflict"
    assert any("another workspace" in item for item in cross.value.diagnostics)
    assert len(json.loads(ledger.read_text())["events"]) == 1

    second = dict(kwargs)
    second["operation_id"] = "usage-op-2"
    second["usage_id"] = "usage-native-2"
    second["step_index"] = 2
    assert record_usage(store, **second)["status"] == "applied"
    assert len(json.loads(ledger.read_text())["events"]) == 2

    duplicate = dict(second)
    duplicate["operation_id"] = "usage-op-3"
    duplicate["step_index"] = 3
    with pytest.raises(JobError) as dup:
        record_usage(store, **duplicate)
    assert dup.value.code == "idempotency_conflict"
    assert len(json.loads(ledger.read_text())["events"]) == 2
