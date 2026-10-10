"""OMP-560: ensure_project retries serializable transactions on failure.

Under concurrent creation, serialization failures trigger a retry with a fresh
transaction and lookup. Three attempts maximum; the third failure raises
WorkStoreError("unavailable", ("retry_exhausted",)). Non-retryable errors
propagate immediately without retry.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from test_project_store_ensure_race import _workspace
from test_workflow_service import OWNER

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _count_workspace_rows(service, workspace_id: UUID) -> tuple[int, int]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_work.projects WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
        projects = int(row[0]) if row is not None else 0
        cur.execute(
            "SELECT count(*) FROM omp_work.project_records WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cur.fetchone()
        records = int(row[0]) if row is not None else 0
        return projects, records


def test_ensure_project_retry_succeeds_on_third_attempt(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "retry-key-success"

    attempts = 0
    in_ensure_project = False
    original_transaction = PostgresWorkStore._transaction

    @contextmanager
    def patched_transaction(
        self, ws_id: UUID, actor_id: UUID, *, serializable: bool = False
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with original_transaction(
            self, ws_id, actor_id, serializable=serializable
        ) as cur:
            yield cur
            if in_ensure_project:
                nonlocal attempts
                attempts += 1
                if attempts in (1, 2):
                    cur.execute(
                        "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='40001'; END $$"
                    )

    monkeypatch.setattr(PostgresWorkStore, "_transaction", patched_transaction)

    in_ensure_project = True
    try:
        project_id = store.ensure_project(
            workspace_id, OWNER, key, "Retry Project", "surface"
        )
    finally:
        in_ensure_project = False

    assert attempts == 3
    found = store.find_project(workspace_id, OWNER, key)
    assert found["project_id"] == str(project_id)

    projects, records = _count_workspace_rows(service, workspace_id)
    assert projects == 1
    assert records == 1

    projects_count, missing = store.count_missing_records(workspace_id, OWNER)
    assert projects_count == 1
    assert missing == 0


def test_ensure_project_retry_exhausted_raises_unavailable(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "retry-key-exhausted"

    attempts = 0
    in_ensure_project = False
    original_transaction = PostgresWorkStore._transaction

    @contextmanager
    def patched_transaction(
        self, ws_id: UUID, actor_id: UUID, *, serializable: bool = False
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with original_transaction(
            self, ws_id, actor_id, serializable=serializable
        ) as cur:
            yield cur
            if in_ensure_project:
                nonlocal attempts
                attempts += 1
                cur.execute("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='40001'; END $$")

    monkeypatch.setattr(PostgresWorkStore, "_transaction", patched_transaction)

    in_ensure_project = True
    try:
        with pytest.raises(WorkStoreError) as exc_info:
            store.ensure_project(workspace_id, OWNER, key, "Retry Project", "surface")
    finally:
        in_ensure_project = False

    assert exc_info.value.code == "unavailable"
    assert exc_info.value.diagnostics == ("retry_exhausted",)
    assert attempts == 3

    projects, records = _count_workspace_rows(service, workspace_id)
    assert projects == 0
    assert records == 0


def test_ensure_project_propagates_division_by_zero(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "retry-key-error"

    attempts = 0
    in_ensure_project = False
    original_transaction = PostgresWorkStore._transaction

    @contextmanager
    def patched_transaction(
        self, ws_id: UUID, actor_id: UUID, *, serializable: bool = False
    ) -> Iterator[psycopg.Cursor[dict[str, object]]]:
        with original_transaction(
            self, ws_id, actor_id, serializable=serializable
        ) as cur:
            yield cur
            if in_ensure_project:
                nonlocal attempts
                attempts += 1
                cur.execute("SELECT 1/0")

    monkeypatch.setattr(PostgresWorkStore, "_transaction", patched_transaction)

    in_ensure_project = True
    try:
        with pytest.raises(psycopg.errors.DivisionByZero):
            store.ensure_project(workspace_id, OWNER, key, "Retry Project", "surface")
    finally:
        in_ensure_project = False

    assert attempts == 1

    projects, records = _count_workspace_rows(service, workspace_id)
    assert projects == 0
    assert records == 0
