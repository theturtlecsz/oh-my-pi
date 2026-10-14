"""Disposable PostgreSQL contracts for the read-only qualification boundary."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work import qualification_snapshot
from omp_work.jobs.store import NativeJobStore
from omp_work.qualification_records import unavailable_report
from omp_work.qualification_snapshot import load_snapshot
from omp_work.v1.canonical import sha256
from test_native_jobs_fencing import _claim, _enqueue, _settle, _worker
from test_workflow_service import OWNER, _command, _create, _grant, _plan

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _credentials(config, workspace_id):
    for name, value in (("workspace-id", workspace_id), ("operator-actor-id", OWNER)):
        path = config.secret_path(name)
        path.write_text(str(value))
        path.chmod(0o600)


def _database_state(config):
    # Whole rows plus explicit counts catch writes to evidence, operations and
    # event ledgers as well as changes to the current item/job projections.
    tables = [
        "omp_work.work_items", "omp_work.work_revisions", "omp_evidence.receipts",
        "omp_audit.domain_events", "omp_research.campaigns", "omp_research.trials",
        "omp_research.observations", "omp_research.components", "omp_jobs.jobs",
        "omp_jobs.job_events", "omp_jobs.operations", "omp_jobs.outbox",
        "omp_jobs.reservations", "omp_control.idempotent_commands",
        "omp_control.operations_evidence",
    ]
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        return {
            table: conn.execute(psycopg.sql.SQL(
                "SELECT count(*), coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]') FROM {} t"
            ).format(psycopg.sql.Identifier(*table.split(".")))).fetchone()
            for table in tables
        }


def test_complete_scoped_histories_and_readonly_transaction(native_jobs, monkeypatch):
    service = native_jobs.service
    config = service.config
    ws = native_jobs.workspace_id
    _credentials(config, ws)
    plan = _plan(service, ws, native_jobs.item)
    observation_id = uuid4()
    payload = {"result": "negative science", "path": "/not/a/readable/evidence/path"}
    status, body = _command(service, ws, {
        "type": "record_research_observation", "payload": {
            "observation_id": str(observation_id),
            "campaign_id": str(native_jobs.campaign_id), "trial_id": None,
            "issuer_kind": "candidate_authored", "source_ref": "/missing/observation.json",
            "execution_status": "completed", "payload": payload,
            "payload_sha256": sha256(payload), "observed_at": datetime.now(UTC).isoformat(),
        },
    })
    assert status == 200, body
    revision_id = uuid4()
    status, body = _command(service, ws, {
        "type": "revise_work", "payload": {
            "work_id": native_jobs.item["work_id"],
            "expected_revision_id": native_jobs.item["revision_id"],
            "revision": {
                "revision_id": str(revision_id), "work_id": native_jobs.item["work_id"],
                "revision_number": 2, "title": "snapshot revision", "description": "history",
                "scope": "", "acceptance_criteria": [],
                "content_sha256": sha256({"title": "snapshot revision", "description": "history"}),
                "created_by": "owner", "created_at": datetime.now(UTC).isoformat(),
            },
        },
    })
    assert status == 200, body
    store = NativeJobStore(config)
    job_id = f"snapshot-{uuid4()}"
    _enqueue(native_jobs, store, job_id=job_id)
    worker_id = _worker(native_jobs, store, ["compute.cpu"])
    claimed = _claim(native_jobs, store, worker_id)
    assert claimed["job"]["job_id"] == job_id
    _settle(native_jobs, store, job_id=job_id, worker_id=worker_id, fence=claimed["job"]["fence"])
    queued_id = f"queued-{uuid4()}"
    _enqueue(native_jobs, store, job_id=queued_id)

    other_ws = uuid4()
    _grant(service, other_ws)
    other_item = _create(service, other_ws, "foreign task")
    other_plan = _plan(service, other_ws, other_item)
    foreign_job = f"foreign-{uuid4()}"
    _enqueue(native_jobs, store, job_id=foreign_job, workspace_id=other_ws, work_id=UUID(other_item["work_id"]))
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute("INSERT INTO omp_jobs.job_events(job_id,seq,kind) VALUES(%s,1000,'foreign')", (foreign_job,))
        # > typical page sizes, no annotation: every event must survive.
        conn.execute(
            "INSERT INTO omp_jobs.job_events(job_id,seq,kind,at) "
            "SELECT %s,1000+n,'poll', '2020-01-01'::timestamptz + n*interval '1 second' FROM generate_series(1,125) n",
            (job_id,),
        )
        expected_events = conn.execute(
            "SELECT event_id::text, sequence FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence", (ws,),
        ).fetchall()

    before = _database_state(config)
    real_connect = qualification_snapshot._connect
    real_row_json = qualification_snapshot.row_json
    active = []
    settings = []

    def connect(config, role):
        assert role == "omp_work_readonly"
        conn = real_connect(config, role)
        active.append(conn)
        return conn

    def inspect_row(row):
        if not settings:
            conn = active[0]
            settings.append(conn.execute(
                "SELECT current_user, current_setting('transaction_read_only'), "
                "current_setting('transaction_isolation'), current_setting('omp.workspace_id'), current_setting('omp.actor_id')"
            ).fetchone())
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute("CREATE TABLE public.qualification_write_probe(id int)")
        return real_row_json(row)

    monkeypatch.setattr(qualification_snapshot, "_connect", connect)
    monkeypatch.setattr(qualification_snapshot, "row_json", inspect_row)
    snapshot = load_snapshot(config)
    assert settings == [("omp_work_readonly", "on", "repeatable read", str(ws), str(OWNER))]
    assert _database_state(config) == before
    assert snapshot.protocol is None
    assert [r.recorded_at for r in snapshot.records] == sorted(r.recorded_at for r in snapshot.records)
    records = {(r.source, r.record_id): json.loads(r.row_json) for r in snapshot.records}
    assert ("omp_work.work_items", native_jobs.item["work_id"]) in records
    assert ("omp_work.work_revisions", native_jobs.item["revision_id"]) in records
    assert ("omp_work.work_revisions", str(revision_id)) in records
    assert ("omp_research.campaigns", str(native_jobs.campaign_id)) in records
    assert ("omp_research.trials", str(native_jobs.trial_id)) in records
    assert all(("omp_research.components", component) in records for component in native_jobs.components.values())
    assert records["omp_jobs.jobs", job_id]["settlement"]["outcome"] == "succeeded"
    assert records["omp_jobs.jobs", queued_id]["attempt"] == 0
    assert records["omp_research.observations", str(observation_id)]["payload"] == payload
    assert snapshot.observations[0].record_id == str(observation_id)
    assert plan["receipt_id"] in {r.record_id for r in snapshot.receipts}
    events = [r for r in snapshot.records if r.source == "omp_audit.domain_events"]
    assert [(r.record_id, json.loads(r.row_json)["sequence"]) for r in events] == expected_events
    assert all(("omp_jobs.job_events", f"{job_id}:{1000+n}") in records for n in range(1,126))
    serialized = json.dumps([json.loads(r.row_json) for r in snapshot.records])
    assert all(foreign not in serialized for foreign in (str(other_ws), other_item["work_id"], other_plan["receipt_id"], foreign_job))
    report = unavailable_report(snapshot, "20-consecutive-accepted")
    assert report.status == "not started" and report.count is None
    assert report.strata_status == "not preregistered" and report.allocations == ()
    assert {r.record_id for r in snapshot.records} <= {e.record_id for e in report.exclusions}


def test_empty_workspace_does_not_invent_freeze(native_jobs):
    config = native_jobs.service.config
    _credentials(config, uuid4())
    before = _database_state(config)
    snapshot = load_snapshot(config)
    assert snapshot.records == () and snapshot.protocol is None
    report = unavailable_report(snapshot, "72h/30-distinct-task")
    assert (report.status, report.count, report.strata_status) == ("not started", None, "not preregistered")
    assert report.exclusions == () and report.allocations == ()
    assert _database_state(config) == before


def test_database_and_query_errors_are_not_empty_reports(native_jobs, monkeypatch):
    config = native_jobs.service.config
    _credentials(config, native_jobs.workspace_id)
    with pytest.raises(psycopg.OperationalError):
        load_snapshot(replace(config, database="qualification_missing_database"))
    # A nonexistent relation exercises a real server query error.
    real_sources = qualification_snapshot._SOURCES
    monkeypatch.setattr(qualification_snapshot, "_SOURCES", (
        ("omp_work.qualification_missing_table", "work_id", "created_at"), *real_sources,
    ))
    with pytest.raises(psycopg.errors.UndefinedTable):
        load_snapshot(config)


def test_invalid_stored_evidence_retains_source_and_is_not_repaired(native_jobs):
    config = native_jobs.service.config
    _credentials(config, native_jobs.workspace_id)
    observation_id = uuid4()
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_research.observations("
            "observation_id,workspace_id,campaign_id,trial_id,issuer_kind,source_ref,"
            "execution_status,payload,payload_sha256,observed_at) "
            "VALUES(%s,%s,%s,%s,'candidate_authored','/missing/evidence','crashed',"
            "'{\"result\":\"failure\"}'::jsonb,%s,clock_timestamp())",
            (observation_id, native_jobs.workspace_id, native_jobs.campaign_id,
             native_jobs.trial_id, "0" * 64),
        )
    before = _database_state(config)
    snapshot = load_snapshot(config)
    assert str(observation_id) not in {r.record_id for r in snapshot.observations}
    exclusion = next(e for e in snapshot.exclusions if e.record_id == str(observation_id))
    assert "payload hash mismatch" in exclusion.reason
    row = next(r for r in snapshot.records if r.record_id == str(observation_id))
    assert json.loads(row.row_json)["trial_id"] == str(native_jobs.trial_id)
    assert json.loads(row.row_json)["payload"] == {"result": "failure"}
    assert _database_state(config) == before
