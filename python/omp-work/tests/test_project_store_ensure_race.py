"""OMP-560: two concurrent ensure_project calls serialize to one project.

Both workers must read the key as absent in the same instant (a barrier after
each worker's first absent lookup), then race the insert. Serializable
isolation aborts one of them; the retry re-reads and returns the committed
project, so both calls return the same UUID and one project plus one
project_records row remain.
"""

from __future__ import annotations

import os
import threading
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.project_store import ProjectStoreMixin
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _workspace(service, workspace_id) -> None:
    _grant(service, workspace_id)
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )


def test_concurrent_ensure_project_creates_one_project(service) -> None:
    store_a = PostgresWorkStore(service.config)
    store_b = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    key = "race-key"

    barrier = threading.Barrier(2, timeout=10)
    gated: set[int] = set()
    original = ProjectStoreMixin._existing_project_id

    def patched(self, cur, workspace_id_arg, key_arg):
        cur.execute("SET LOCAL statement_timeout='5000ms'")
        found = original(self, cur, workspace_id_arg, key_arg)
        # Gate only each worker's first absent lookup; retry reads never wait.
        if found is None and threading.get_ident() not in gated:
            gated.add(threading.get_ident())
            barrier.wait()
        return found

    results: dict[str, UUID] = {}
    errors: list[BaseException] = []

    def worker(name: str, store: PostgresWorkStore) -> None:
        try:
            results[name] = store.ensure_project(
                workspace_id, OWNER, key, "Race Project", "surface"
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=("a", store_a)),
        threading.Thread(target=worker, args=("b", store_b)),
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectStoreMixin, "_existing_project_id", patched)
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            for thread in threads:
                if thread.is_alive():
                    thread.join(timeout=5)

    for thread in threads:
        assert not thread.is_alive(), "worker thread did not finish"
    assert not errors, f"worker raised: {errors!r}"
    assert len(results) == 2, f"expected two results, got {results!r}"
    assert len(set(results.values())) == 1, (
        f"two concurrent creators returned different ids: {results!r}"
    )
    project_id = results["a"]

    found = store_a.find_project(workspace_id, OWNER, key)
    assert found["project_id"] == str(project_id)

    projects, missing = store_a.count_missing_records(workspace_id, OWNER)
    assert projects == 1
    assert missing == 0
