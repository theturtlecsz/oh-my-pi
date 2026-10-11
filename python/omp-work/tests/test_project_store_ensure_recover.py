"""OMP-585: an aborted ensure_project attempt observes the committed row.

A serialization failure or a statement timeout rolls the attempt back. The
store re-reads outside that transaction and waits once for a peer that has
not committed yet. Read Committed still lets two creators insert two projects,
which is what the race test exists to catch.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.project_store import ProjectStoreMixin
from omp_work.v1.store import PostgresWorkStore
from test_project_store_ensure_race import (
    _workspace,
    test_concurrent_ensure_project_creates_one_project as concurrent_ensure_project,
)
from test_workflow_service import OWNER

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def test_ensure_project_retries_statement_timeout(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "recover-timeout"

    calls = {"n": 0}
    original = ProjectStoreMixin._existing_project_id

    def patched(self, cur, workspace_id_arg, key_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            cur.execute("SET LOCAL statement_timeout = '200ms'")
            cur.execute("SELECT pg_sleep(1)")
        return original(self, cur, workspace_id_arg, key_arg)

    monkeypatch.setattr(ProjectStoreMixin, "_existing_project_id", patched)
    project_id = store.ensure_project(workspace_id, OWNER, key, "Timeout", "surface")

    assert calls["n"] >= 2
    found = store.find_project(workspace_id, OWNER, key)
    assert found["project_id"] == str(project_id)
    projects, missing = store.count_missing_records(workspace_id, OWNER)
    assert projects == 1
    assert missing == 0


def test_ensure_project_returns_committed_row_without_another_attempt(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "recover-committed"
    created = store.ensure_project(workspace_id, OWNER, key, "Recover", "surface")

    calls = {"n": 0}
    original = PostgresWorkStore._transaction

    @contextmanager
    def patched(
        self, ws_id: UUID, actor_id: UUID, *, serializable: bool = False
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with original(self, ws_id, actor_id, serializable=serializable) as cur:
            yield cur
            calls["n"] += 1
            if calls["n"] == 1 and serializable:
                cur.execute("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='40001'; END $$")

    monkeypatch.setattr(PostgresWorkStore, "_transaction", patched)
    found = store.ensure_project(workspace_id, OWNER, key, "Recover", "surface")

    assert found == created
    assert calls["n"] == 1
    projects, missing = store.count_missing_records(workspace_id, OWNER)
    assert projects == 1
    assert missing == 0


def test_concurrent_ensure_project_needs_serializable_isolation(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PostgresWorkStore._transaction

    @contextmanager
    def read_committed(
        self, ws_id: UUID, actor_id: UUID, *, serializable: bool = False
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with original(self, ws_id, actor_id, serializable=False) as cur:
            yield cur

    monkeypatch.setattr(PostgresWorkStore, "_transaction", read_committed)
    with pytest.raises(AssertionError, match="different ids"):
        concurrent_ensure_project(service)
