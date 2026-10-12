"""Real PostgreSQL deadlines, including transport and lost COMMIT acknowledgement."""
from __future__ import annotations

import os
import select
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from omp_work.jobs.store import NativeJobStore, TransactionDeadline, TransactionDeadlineExceeded
from test_workflow_service import OWNER, _grant, _seed_project, service

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@contextmanager
def _transport(config, *, stall_connect=False, stall_setup=False):
    """One bounded tunnel; suppress replies while still forwarding client SQL."""
    stop = threading.Event()
    blocked = threading.Event()
    commit = threading.Event()
    errors = []
    if stall_connect:
        blocked.set()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.05)
        port = listener.getsockname()[1]

        def tunnel():
            try:
                end = time.monotonic() + 5
                while not stop.is_set() and time.monotonic() < end:
                    try:
                        client, _ = listener.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
                with client, socket.create_connection((config.host, config.port), timeout=0.5) as server:
                    client.settimeout(0.1)
                    server.settimeout(0.1)
                    while not stop.is_set() and time.monotonic() < end:
                        ready, _, _ = select.select([client, server], [], [], 0.05)
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            if source is client:
                                if stall_setup and b"BEGIN" in data:
                                    blocked.set()
                                if b"COMMIT" in data:
                                    commit.set()
                                server.sendall(data)
                            elif not blocked.is_set():
                                client.sendall(data)
            except OSError as error:
                if not stop.is_set():
                    errors.append(error)

        worker = threading.Thread(target=tunnel)
        worker.start()
        try:
            yield replace(config, port=port), blocked, commit
        finally:
            stop.set()
            worker.join(timeout=2)
            assert not worker.is_alive(), "test tunnel survived cleanup"
            assert not errors, errors


def _insert(cur, workspace, operation):
    cur.execute(
        "INSERT INTO omp_jobs.operations(operation_id, workspace_id, kind, request_sha256, result) "
        "VALUES (%s, %s, 'deadline-test', %s, '{}'::jsonb)",
        (operation, workspace, "0" * 64),
    )


def _count(service, operation):
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM omp_jobs.operations WHERE operation_id=%s", (operation,)
        ).fetchone()[0]


def _assert_backend_gone(service, pid):
    end = time.monotonic() + 2
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        while conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone():
            assert time.monotonic() < end, "expired backend survived disposal"
            time.sleep(0.02)


def test_bounded_commit_keeps_role_search_path_and_rls_identity(service):
    workspace, other, project = uuid4(), uuid4(), uuid4()
    _grant(service, workspace)
    _grant(service, other)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s), (%s)", (workspace, other))
    _seed_project(service, workspace, project)
    _seed_project(service, other, uuid4())
    operation = str(uuid4())
    store = NativeJobStore(service.config)
    with store.transaction(workspace, OWNER, deadline=time.monotonic() + 2) as cur:
        cur.execute(
            "SELECT current_user AS role, current_setting('search_path') AS path, "
            "current_setting('omp.workspace_id') AS workspace, "
            "current_setting('omp.actor_id') AS actor"
        )
        assert cur.fetchone() == {
            "role": "omp_work_app", "path": "pg_catalog",
            "workspace": str(workspace), "actor": str(OWNER),
        }
        cur.execute("SELECT project_id FROM omp_work.projects WHERE workspace_id IN (%s, %s)", (workspace, other))
        assert cur.fetchall() == [{"project_id": project}]
        _insert(cur, workspace, operation)
    assert _count(service, operation) == 1
    # Ordinary callers retain transaction rollback semantics.
    rolled_back = str(uuid4())
    with pytest.raises(ValueError):
        with store.transaction(workspace, OWNER) as cur:
            _insert(cur, workspace, rolled_back)
            raise ValueError("caller failed")
    assert _count(service, rolled_back) == 0


@pytest.mark.parametrize("phase", ["connect", "setup"])
def test_connection_and_identity_setup_are_bounded(service, phase):
    with _transport(
        service.config, stall_connect=phase == "connect", stall_setup=phase == "setup",
    ) as (config, _, _):
        start = time.monotonic()
        with pytest.raises(TransactionDeadlineExceeded) as raised:
            with NativeJobStore(config).transaction(uuid4(), OWNER, deadline=start + 0.4):
                pytest.fail("stalled connection reached transaction body")
        assert time.monotonic() - start < 0.7
        assert not raised.value.outcome_uncertain


@pytest.mark.parametrize("fault", ["advisory", "statement", "transport", "rollback"])
def test_expired_sql_cannot_later_write(service, fault):
    workspace, operation, lock = uuid4(), str(uuid4()), 934287
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as holder:
        if fault == "advisory":
            holder.execute("SELECT pg_advisory_lock(%s)", (lock,))
        try:
            transport = (
                _transport(service.config) if fault in {"transport", "rollback"}
                else nullcontext((service.config, None, None))
            )
            with transport as (config, blocked, _):
                start = time.monotonic()
                with pytest.raises(TransactionDeadlineExceeded) as raised:
                    with NativeJobStore(config).transaction(workspace, OWNER, deadline=start + 0.6) as cur:
                        cur.execute("SELECT pg_backend_pid() AS pid")
                        pid = cur.fetchone()["pid"]
                        _insert(cur, workspace, operation)
                        if fault in {"transport", "rollback"}:
                            blocked.set()
                            if fault == "rollback":
                                raise ValueError("caller failure with stalled rollback acknowledgement")
                            cur.execute("SELECT 1")
                        elif fault == "advisory":
                            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
                        else:
                            cur.execute("SELECT pg_sleep(3)")
                        _insert(cur, workspace, operation + "-late")
                assert time.monotonic() - start < 0.9
                assert not raised.value.outcome_uncertain
                assert cur.connection.closed
                with pytest.raises((TransactionDeadlineExceeded, psycopg.OperationalError)):
                    _insert(cur, workspace, operation + "-late")
        finally:
            if fault == "advisory":
                holder.execute("SELECT pg_advisory_unlock(%s)", (lock,))
    _assert_backend_gone(service, pid)
    assert _count(service, operation) == 0
    assert _count(service, operation + "-late") == 0


def test_stalled_commit_acknowledgement_reports_uncertainty(service):
    operation = str(uuid4())
    with _transport(service.config) as (config, blocked, commit):
        start = time.monotonic()
        with pytest.raises(TransactionDeadlineExceeded) as raised:
            with NativeJobStore(config).transaction(uuid4(), OWNER, deadline=start + 0.6) as cur:
                cur.execute("SELECT pg_backend_pid() AS pid")
                pid = cur.fetchone()["pid"]
                _insert(cur, uuid4(), operation)
                blocked.set()
        assert time.monotonic() - start < 0.9
        assert commit.wait(timeout=0.2)
        assert raised.value.outcome_uncertain
        assert cur.connection.closed
        with pytest.raises((TransactionDeadlineExceeded, psycopg.OperationalError)):
            _insert(cur, uuid4(), operation + "-late")
    _assert_backend_gone(service, pid)
    assert _count(service, operation) == 1
    assert _count(service, operation + "-late") == 0


def test_expiry_between_statements_prevents_commit(service):
    operation = str(uuid4())
    with pytest.raises(TransactionDeadlineExceeded):
        with NativeJobStore(service.config).transaction(uuid4(), OWNER, deadline=time.monotonic() + 0.2) as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            pid = cur.fetchone()["pid"]
            _insert(cur, uuid4(), operation)
            time.sleep(0.21)
    _assert_backend_gone(service, pid)
    assert _count(service, operation) == 0


@pytest.mark.parametrize("fault", ["statement", "commit", "rollback"])
def test_late_armed_deadline_bounds_sql_and_cleanup(service, fault):
    workspace, operation = uuid4(), str(uuid4())
    control = TransactionDeadline()
    transport = (
        _transport(service.config) if fault != "statement"
        else nullcontext((service.config, None, None))
    )
    with transport as (config, blocked, commit):
        with pytest.raises(TransactionDeadlineExceeded) as raised:
            with NativeJobStore(config).transaction(workspace, OWNER, deadline_control=control) as cur:
                cur.execute("SELECT pg_backend_pid() AS pid")
                pid = cur.fetchone()["pid"]
                _insert(cur, workspace, operation)
                # Upstream work outlasts the eventual resolution allowance.
                time.sleep(0.6)
                cur.execute("SELECT 1")
                start = time.monotonic()
                control.arm(start + 0.4)
                if fault == "statement":
                    cur.execute("SELECT pg_sleep(3)")
                    _insert(cur, workspace, operation + "-late")
                else:
                    blocked.set()
                    if fault == "rollback":
                        raise ValueError("upstream failure; rollback reply lost")
        assert time.monotonic() - start < 0.7
        assert raised.value.outcome_uncertain is (fault == "commit")
        assert cur.connection.closed
        if fault == "commit":
            assert commit.wait(timeout=0.2)
        elif commit is not None:
            assert not commit.is_set()
        with pytest.raises((TransactionDeadlineExceeded, psycopg.OperationalError)):
            _insert(cur, workspace, operation + "-late")
        if blocked is not None:
            blocked.clear()
    _assert_backend_gone(service, pid)
    assert _count(service, operation) == (1 if fault == "commit" else 0)
    assert _count(service, operation + "-late") == 0


def test_inactive_control_preserves_commit_and_rollback(service):
    store = NativeJobStore(service.config)
    workspace, committed, rolled_back = uuid4(), str(uuid4()), str(uuid4())
    with store.transaction(workspace, OWNER, deadline_control=TransactionDeadline()) as cur:
        _insert(cur, workspace, committed)
    with pytest.raises(ValueError):
        with store.transaction(workspace, OWNER, deadline_control=TransactionDeadline()) as cur:
            _insert(cur, workspace, rolled_back)
            raise ValueError("caller failed before arming")
    assert _count(service, committed) == 1
    assert _count(service, rolled_back) == 0


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf")])
def test_control_rejects_nonfinite_deadline_without_losing_budget(deadline):
    control = TransactionDeadline()
    with pytest.raises(ValueError, match="finite"):
        control.arm(deadline)
    control.arm(time.monotonic() + 1)
    with pytest.raises(ValueError, match="already armed"):
        control.arm(time.monotonic() + 10)


def test_late_bounded_mode_rejects_unsupported_libpq(service, monkeypatch):
    monkeypatch.setattr(psycopg.pq, "version", lambda: 160000)
    with pytest.raises(RuntimeError, match="libpq >= 17"):
        with NativeJobStore(service.config).transaction(uuid4(), OWNER, deadline_control=TransactionDeadline()):
            pytest.fail("unsupported bounded mode reached transaction body")
