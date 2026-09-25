"""S2 contracts against only the temporary PostgreSQL workflow fixture."""
from __future__ import annotations

import importlib.util
import os
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from omp_work.operations.database import migration_set_sha256
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from test_workflow_service import service  # isolated native_postgres fixture

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                               reason="set OMP_WORK_POSTGRES_INTEGRATION=1")


@pytest.fixture(scope="module")
def shadow(service):
    path = Path(os.environ.get("OMP_SHADOW_TAILER_PATH", str(
        Path(__file__).resolve().parents[4] / "economy/ACTIVE/parallel-runtime/shadow/shadow_tailer.py")))
    spec = importlib.util.spec_from_file_location("tested_shadow_tailer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = migration_set_sha256()
    apply_jobs_migrations(service.config)
    assert migration_set_sha256() == before
    assert apply_jobs_migrations(service.config)["applied"] == []
    return module


def job(identity, status="sealed"):
    return (identity, status, None, "test", "test/path", 12, None, "[]", None, None, 1000, 1001)


@pytest.mark.parametrize("value", [True, False, "not-a-timestamp", "2026-09-21T12:10:09.352861", "2026-09-21", float("nan"), float("inf")])
def test_shadow_rejects_ambiguous_or_invalid_timestamps(shadow, value):
    with pytest.raises(ValueError, match="invalid source timestamp"):
        shadow.epoch_to_ts(value)


def test_shadow_mixed_source_timestamp_formats_preserve_instants(service, shadow):
    first = "2026-09-21T12:10:09.352861+00:00"
    second = "2026-09-21T12:11:29.030388+00:00"
    expected = datetime.fromisoformat(first)
    assert shadow.epoch_to_ts("2026-09-21T07:10:09.352861-05:00") == expected
    assert shadow.epoch_to_ts("2026-09-21T12:10:09.352861Z") == expected
    assert shadow.epoch_to_ts(expected.timestamp()) == expected
    assert shadow.epoch_to_ts(str(expected.timestamp())) == expected
    assert shadow.epoch_to_ts(1000) == datetime.fromtimestamp(1000, timezone.utc)
    assert shadow.epoch_to_ts(None) is None
    rows = [(*job("mixed-first")[:-1], first), (*job("mixed-second")[:-1], second)]
    leases = [("mixed/path", "mixed-first", first)]
    reservations = [("mixed-budget", "mixed-first", 12, "test", second)]
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as connection:
        with connection.transaction(force_rollback=True), connection.cursor() as cur:
            shadow.sync_jobs(cur, rows)
            shadow.sync_leases(cur, leases)
            shadow.sync_reservations(cur, reservations)
            assert shadow.parity_report(cur, {"sealed": 2}, rows, leases, reservations)["diff_zero"]
            cur.execute("SELECT updated_at FROM omp_jobs.jobs WHERE job_id='mixed-first'")
            assert cur.fetchone() == (expected,)


def test_shadow_preserves_empty_soft_and_tombstones_only_its_namespace(service, shadow):
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as connection:
        with connection.transaction(force_rollback=True), connection.cursor() as cur:
            rows = [job("source-a", "empty_soft"), job("source-b")]
            leases = [("source/path", "source-a", 2000)]
            reservations = [("source-budget", "source-a", 12, "test", 2000)]
            shadow.sync_jobs(cur, rows)
            shadow.sync_leases(cur, leases)
            shadow.sync_reservations(cur, reservations)
            assert shadow.parity_report(cur, {"empty_soft": 1, "sealed": 1}, rows, leases, reservations)["diff_zero"]
            cur.execute("INSERT INTO omp_jobs.jobs(job_id,status,source) VALUES('native-survivor','in_flight','native')")
            cur.execute("INSERT INTO omp_jobs.leases(path_glob,job_id,held_until) VALUES('native/path','native-survivor',now())")
            shadow.sync_jobs(cur, [rows[1]])
            shadow.sync_leases(cur, [])
            shadow.sync_reservations(cur, [])
            report = shadow.parity_report(cur, {"sealed": 1}, [rows[1]], [], [])
            assert report["diff_zero"] and report["tombstoned_jobs"] == 1
            cur.execute("SELECT status,mirror_tombstoned_at FROM omp_jobs.jobs WHERE job_id='source-a'")
            status, tombstone = cur.fetchone()
            assert status == "empty_soft" and tombstone is not None
            cur.execute("SELECT mirror_namespace,mirror_tombstoned_at FROM omp_jobs.leases WHERE path_glob='native/path'")
            assert cur.fetchone() == (None, None)
            shadow.sync_jobs(cur, rows)
            cur.execute("SELECT mirror_tombstoned_at FROM omp_jobs.jobs WHERE job_id='source-a'")
            assert cur.fetchone() == (None,)


def test_shadow_rejects_native_collisions_and_unknown_status(service, shadow):
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as connection:
        with connection.transaction(force_rollback=True), connection.cursor() as cur:
            cur.execute("INSERT INTO omp_jobs.jobs(job_id,status,source) VALUES('collision','in_flight','native')")
            with pytest.raises(ValueError, match="identity collision"):
                shadow.sync_jobs(cur, [job("collision")])
            cur.execute("SELECT status,source FROM omp_jobs.jobs WHERE job_id='collision'")
            assert cur.fetchone() == ("in_flight", "native")
            shadow.sync_jobs(cur, [job("source-a")])
            cur.execute("INSERT INTO omp_jobs.leases(path_glob,job_id,held_until) VALUES('collision/path','collision',now())")
            with pytest.raises(ValueError, match="identity collision"):
                shadow.sync_leases(cur, [("collision/path", "source-a", 2000)])
            cur.execute("SELECT job_id FROM omp_jobs.leases WHERE path_glob='collision/path'")
            assert cur.fetchone() == ("collision",)
            with pytest.raises(ValueError, match="unsupported source"):
                shadow.sync_jobs(cur, [job("unknown", "new_unrecognized_state")])


def test_shadow_exposes_unexplained_legacy_rows_and_identity_drift(service, shadow):
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as connection:
        with connection.transaction(force_rollback=True), connection.cursor() as cur:
            cur.execute("INSERT INTO omp_jobs.jobs(job_id,status,source) VALUES('unexplained','sealed','flood_import')")
            rows = [job("source-a", "failed"), job("source-b", "sealed")]
            shadow.sync_jobs(cur, rows)
            report = shadow.parity_report(cur, {"failed": 1, "sealed": 1}, rows, [], [])
            assert not report["diff_zero"] and report["unscoped_legacy_job_count"] == 1
            cur.execute("SELECT mirror_tombstoned_at FROM omp_jobs.jobs WHERE job_id='unexplained'")
            assert cur.fetchone() == (None,)
            cur.execute("UPDATE omp_jobs.jobs SET status=CASE job_id WHEN 'source-a' THEN 'sealed' ELSE 'failed' END WHERE mirror_namespace=%s", (shadow.MIRROR_NAMESPACE,))
            report = shadow.parity_report(cur, {"failed": 1, "sealed": 1}, rows, [], [])
            assert report["status_diffs"] == {} and report["job_identity_diff_count"] == 2


@pytest.mark.parametrize("changed", [None, "native", "status", "timestamp", "present", "identity"])
def test_legacy_allowlist_rechecks_provenance_and_never_deletes(service, shadow, changed):
    source = sqlite3.connect(":memory:")
    source.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY)")
    identity = "legacy-reviewed"
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as connection:
        with connection.transaction(force_rollback=True), connection.cursor() as cur:
            cur.execute("INSERT INTO omp_jobs.jobs(job_id,status,source,idempotency_key,flood_origin) VALUES(%s,'failed','flood_import',%s,%s::jsonb) RETURNING created_at,updated_at",
                        (identity, hashlib.sha256(f"flood:{identity}".encode()).hexdigest(), json.dumps({"job_id": identity, "status": "failed"})))
            created, updated = cur.fetchone()
            expected = {"job_id": identity, "status": "failed", "source": "flood_import",
                        "original_job_id": identity, "original_status": "failed",
                        "created_at": created.isoformat(), "updated_at": updated.isoformat()}
            if changed == "native":
                cur.execute("UPDATE omp_jobs.jobs SET source='native' WHERE job_id=%s", (identity,))
            elif changed == "status":
                cur.execute("UPDATE omp_jobs.jobs SET status='in_flight' WHERE job_id=%s", (identity,))
            elif changed == "timestamp":
                cur.execute("UPDATE omp_jobs.jobs SET updated_at=updated_at+interval '1 second' WHERE job_id=%s", (identity,))
            elif changed == "identity":
                cur.execute("UPDATE omp_jobs.jobs SET idempotency_key='different' WHERE job_id=%s", (identity,))
            elif changed == "present":
                source.execute("INSERT INTO jobs VALUES(?)", (identity,))
            if changed:
                with pytest.raises(ValueError):
                    shadow.reconcile_legacy_jobs(cur, source, [expected], "a" * 64)
                cur.execute("SELECT mirror_tombstoned_at FROM omp_jobs.jobs WHERE job_id=%s", (identity,))
                assert cur.fetchone() == (None,)
            else:
                assert shadow.reconcile_legacy_jobs(cur, source, [expected], "a" * 64) == 1
                assert shadow.reconcile_legacy_jobs(cur, source, [expected], "a" * 64) == 0
                cur.execute("SELECT status,mirror_tombstoned_at FROM omp_jobs.jobs WHERE job_id=%s", (identity,))
                status, timestamp = cur.fetchone()
                assert status == "failed" and timestamp is not None
                cur.execute("SELECT count(*) FROM omp_jobs.job_events WHERE job_id=%s AND kind='legacy_tombstone'", (identity,))
                assert cur.fetchone() == (1,)
    source.close()
