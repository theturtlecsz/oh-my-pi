"""Launch history stays tenant isolated and is held atomically with intake advance."""
from __future__ import annotations

import os
from dataclasses import fields, replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from omp_work.intake_repository import append_revision, hold_bindings, lock_revision, read_revision
from omp_work.orchestrator.intake_binding import admit
from omp_work.orchestrator.intake_gate import INTAKE_REVISION_ADVANCED, cursor, read_head
from omp_work.orchestrator.launch_binding import LaunchBinding, read, record
from test_nsi2_repository import connection
from test_orchestrator_intake_binding import world  # noqa: F401
from test_workflow_service import OWNER
from typed_intake_support import record_pass

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def launch(world):
    store, scope, receipt = world
    with cursor(store, workspace_id=scope["workspace_id"], actor_id=OWNER) as cur:
        binding_id = admit(cur, workspace_id=scope["workspace_id"], mission_id=scope["mission_id"],
                           run_id=scope["run_id"], revision=0, assessment_id=receipt)
    return store, dict(
        workspace_id=scope["workspace_id"], mission_id=scope["mission_id"],
        intake_binding_id=binding_id, run_id=scope["run_id"], revision=0,
        assessment_id=receipt, plan_receipt_id="plan-receipt", plan_sha256="a" * 64,
        item_id="packet", work_id=uuid4(), candidate_id="candidate",
    )


def key(values):
    return {name: values[name] for name in ("workspace_id", "mission_id", "work_id")}


def test_launch_permissions_and_tenant_isolation(service, launch):
    store, values = launch
    workspace_id = values["workspace_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        launch_id = record(cur, **values)
    table = "omp_work.mission_launch_bindings"
    with connection(service, workspace_id, "postgres") as conn:
        assert conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
            (table,),
        ).fetchone() == (True, True)
        for role, select, insert in (
            ("omp_work_app", True, True), ("omp_work_readonly", True, False),
            ("omp_work_backup", True, False), ("omp_work_importer", False, False),
        ):
            assert conn.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT'),has_table_privilege(%s,%s,'INSERT'),"
                "has_table_privilege(%s,%s,'UPDATE'),has_table_privilege(%s,%s,'DELETE'),"
                "has_table_privilege(%s,%s,'TRUNCATE')", (role, table) * 5,
            ).fetchone() == (select, insert, False, False, False)
            for field in fields(LaunchBinding):
                assert conn.execute(
                    "SELECT has_column_privilege(%s,%s,%s,'UPDATE')", (role, table, field.name),
                ).fetchone() == (role == "omp_work_app" and field.name in ("state", "hold_code"),)
    for role in ("omp_work_app", "omp_work_readonly", "omp_work_owner"):
        with connection(service, uuid4(), "postgres") as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert conn.execute("SELECT count(*) FROM omp_work.mission_launch_bindings").fetchone() == (0,)
            assert read(conn.cursor(), **key(values)) is None
    with connection(service, uuid4()) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            record(conn.cursor(), **values)
    with connection(service, workspace_id) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE omp_work.mission_launch_bindings SET revision=3")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM omp_work.mission_launch_bindings")
    with connection(service, workspace_id, "postgres") as conn:
        conn.execute("SET ROLE omp_work_readonly")
        assert read(conn.cursor(), **key(values)).launch_binding_id == launch_id
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE omp_work.mission_launch_bindings SET state='held'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            record(conn.cursor(), **{**values, "candidate_id": "another"})


def test_record_is_idempotent_and_read_is_newest_per_work(launch):
    store, values = launch
    with cursor(store, workspace_id=values["workspace_id"], actor_id=OWNER) as cur:
        assert read(cur, **key(values)) is None
        first = record(cur, **values)
        assert record(cur, **values) == first
        cur.execute("SELECT count(*) FROM omp_work.mission_launch_bindings WHERE workspace_id=%s",
                    (values["workspace_id"],))
        assert cur.fetchone() == (1,)
        original = read(cur, **key(values))
        assert isinstance(original, LaunchBinding)
        assert all(getattr(original, name) == value for name, value in values.items())
        assert original.state == "bound" and original.hold_code is None
        # A later row for another work item must not shadow this item's history.
        record(cur, **{**values, "work_id": uuid4()})
        assert read(cur, **key(values)) == original
        changed = {**values, "candidate_id": "new-candidate"}
        newest = record(cur, **changed)
        assert newest != first
    with cursor(store, workspace_id=values["workspace_id"], actor_id=OWNER) as cur:
        assert read(cur, **key(values)) == replace(
            original, launch_binding_id=newest, candidate_id="new-candidate",
            created_at=read(cur, **key(values)).created_at,
        )
        # Returning to old coordinates appends history, rather than finding an old match.
        assert record(cur, **values) not in (first, newest)
        assert read(cur, **{**key(values), "mission_id": uuid4()}) is None
        assert read(cur, **{**key(values), "work_id": uuid4()}) is None


@pytest.mark.parametrize("coordinate,value", [
    ("intake_binding_id", None), ("run_id", "new-run"), ("revision", 1),
    ("assessment_id", "new-receipt"), ("plan_receipt_id", "new-plan"),
    ("plan_sha256", "b" * 64), ("item_id", "new-packet"),
])
def test_changed_coordinates_append_new_binding(launch, coordinate, value):
    store, values = launch
    with cursor(store, workspace_id=values["workspace_id"], actor_id=OWNER) as cur:
        first = record(cur, **values)
        if coordinate == "intake_binding_id":
            value = admit(cur, workspace_id=values["workspace_id"], mission_id=values["mission_id"],
                          run_id=values["run_id"], revision=0, assessment_id=values["assessment_id"])
        newest = record(cur, **{**values, coordinate: value})
        assert newest != first
        assert read(cur, **key(values)).launch_binding_id == newest
        assert getattr(read(cur, **key(values)), coordinate) == value


def test_app_hold_preserves_coordinates_and_held_history(launch):
    store, values = launch
    with cursor(store, workspace_id=values["workspace_id"], actor_id=OWNER) as cur:
        first = record(cur, **values)
        before = read(cur, **key(values))
        hold_bindings(cur, workspace_id=values["workspace_id"], mission_id=uuid4(), code="other")
        assert read(cur, **key(values)) == before
        hold_bindings(cur, workspace_id=values["workspace_id"], mission_id=values["mission_id"], code="stop")
        held = replace(before, state="held", hold_code="stop")
        assert read(cur, **key(values)) == held
        hold_bindings(cur, workspace_id=values["workspace_id"], mission_id=values["mission_id"], code="later")
        assert read(cur, **key(values)) == held
        assert record(cur, **values) != first
        cur.execute("SELECT state,hold_code FROM omp_work.mission_launch_bindings WHERE launch_binding_id=%s",
                    (first,))
        assert cur.fetchone() == ("held", "stop")


@pytest.mark.parametrize("commit", [True, False], ids=["commit-holds", "rollback-keeps-bound"])
def test_append_revision_holds_launch_atomically(service, launch, commit):
    store, values = launch
    workspace_id, mission_id = values["workspace_id"], values["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        record(cur, **values)
        before = read(cur, **key(values))
        record(cur, **{**values, "work_id": uuid4(), "item_id": "second-packet"})
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=workspace_id, run_id=values["run_id"], revision=0).body
        body.snapshot.revision += 1
        body.binding.revision += 1
        body.assessments = []
        body.derivations = []
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id, run_id=values["run_id"],
                               expected_revision=0, operation_id=operation_id)
        append_revision(cur, locked, body, operation_id=operation_id)
        held = replace(before, state="held", hold_code=INTAKE_REVISION_ADVANCED)
        assert read(cur, **key(values)) == held
        cur.execute("SELECT state,hold_code FROM omp_work.mission_launch_bindings WHERE workspace_id=%s",
                    (workspace_id,))
        assert cur.fetchall() == [("held", INTAKE_REVISION_ADVANCED)] * 2
        if commit:
            conn.commit()
        else:
            conn.rollback()
    if commit:
        record_pass(store, workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id)
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read_head(cur, workspace_id=workspace_id, mission_id=mission_id).revision == int(commit)
        assert read(cur, **key(values)) == (held if commit else before)
