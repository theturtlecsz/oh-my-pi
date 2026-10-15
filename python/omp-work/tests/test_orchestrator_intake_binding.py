"""Mission intake bindings: prepared, admitted on a PASS, and held on advance.

A binding records which typed-intake revision and which PASS receipt a mission
was planned on. The gate decision is re-derived on admit, so a planted receipt
id is refused rather than trusted. Advancing the intake head holds every open
binding in the appender's transaction, leaving its revision and receipt intact,
so a fresh reader sees the mission held, never a silently re-pointed row.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from omp_work.intake_repository import append_revision, hold_bindings, lock_revision, read_revision
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.intake_binding import (
    Binding, IntakeBindingRefused, admit, hold, prepare, read,
)
from omp_work.orchestrator.intake_gate import INTAKE_REVISION_ADVANCED, cursor, read_head
from omp_work.v1.models import BoundedIntakeDraft
from test_nsi2_repository import connection
from test_project_store import _insert_project, _workspace
from test_workflow_service import OWNER
from typed_intake_support import advance_revision, forge_receipt, record_pass, seed_typed_intake

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def world(service):
    workspace_id, mission_id = uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, uuid4(), "binding")
    cases = json.loads((Path(__file__).parent / "fixtures/bounded_intake_golden.json").read_text())
    raw = next(case["input"]["draft"] for case in cases if case["id"] == "01-valid-ready")
    draft = BoundedIntakeDraft.model_validate({**raw, "unknowns": []})
    store = NativeJobStore(service.config)
    scope = dict(workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id, run_id="bind")
    receipt = seed_typed_intake(store, **scope, draft=draft, mode="pass")
    return store, scope, receipt


def test_binding_table_isolated_and_app_columns_limited(service, world):
    store, scope, _ = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        prepare(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind", revision=0)
    with connection(service, workspace_id, "postgres") as conn:
        assert conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='omp_work.mission_intake_bindings'::regclass",
        ).fetchone() == (True, True)
        for role, expected in (
            ("omp_work_app", (True, True)),
            ("omp_work_readonly", (True, False)),
            ("omp_work_backup", (True, False)),
            ("omp_work_importer", (False, False)),
        ):
            privileges = conn.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT'),has_table_privilege(%s,%s,'INSERT'),"
                "has_table_privilege(%s,%s,'UPDATE'),has_table_privilege(%s,%s,'DELETE'),"
                "has_table_privilege(%s,%s,'TRUNCATE')",
                (role, "omp_work.mission_intake_bindings") * 5,
            ).fetchone()
            assert privileges == (expected[0], expected[1], False, False, False)
        for column, allowed in (("state", True), ("hold_code", True), ("revision", False),
                                ("assessment_id", False), ("run_id", False), ("mission_id", False)):
            assert conn.execute(
                "SELECT has_column_privilege('omp_work_app','omp_work.mission_intake_bindings',%s,'UPDATE')",
                (column,),
            ).fetchone() == (allowed,)
    for role in ("omp_work_app", "omp_work_readonly", "omp_work_owner"):
        with connection(service, uuid4(), "postgres") as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert conn.execute("SELECT count(*) FROM omp_work.mission_intake_bindings").fetchone() == (0,)
    with connection(service, uuid4()) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO omp_work.mission_intake_bindings "
                "(binding_id,workspace_id,mission_id,run_id,revision,assessment_id,state) "
                "VALUES (%s,%s,%s,'bind',0,%s,'prepared')",
                (uuid4(), workspace_id, mission_id, "foreign-receipt"),
            )
    with connection(service, workspace_id) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE omp_work.mission_intake_bindings SET revision=3")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM omp_work.mission_intake_bindings")


def test_prepare_admit_and_fresh_read_newest(world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        prepared = prepare(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind", revision=0)
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            prepared, "bind", 0, None, "prepared", None)
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
              revision=0, assessment_id=receipt)
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        newest = read(cur, workspace_id=workspace_id, mission_id=mission_id)
    assert newest == Binding(newest.binding_id, "bind", 0, receipt, "admitted", None)
    assert newest.binding_id != prepared


def test_admit_refuses_unknown_receipt(world):
    store, scope, _ = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        prepare(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind", revision=0)
        with pytest.raises(IntakeBindingRefused) as refused:
            admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                  revision=0, assessment_id=forge_receipt())
    assert refused.value.code == "intake_receipt_forged"
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id).state == "prepared"


def test_hold_updates_only_state_and_code(world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        prepare(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind", revision=0)
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
        cur.execute(
            "SELECT binding_id,run_id,revision,assessment_id,created_at FROM omp_work.mission_intake_bindings "
            "WHERE workspace_id=%s AND mission_id=%s ORDER BY binding_id",
            (workspace_id, mission_id),
        )
        evidence = cur.fetchall()
        hold(cur, workspace_id=workspace_id, mission_id=mission_id, code="owner_cancelled")
        cur.execute(
            "SELECT binding_id,run_id,revision,assessment_id,created_at,state,hold_code "
            "FROM omp_work.mission_intake_bindings "
            "WHERE workspace_id=%s AND mission_id=%s ORDER BY binding_id",
            (workspace_id, mission_id),
        )
        assert cur.fetchall() == [(*row, "held", "owner_cancelled") for row in evidence]
        held = read(cur, workspace_id=workspace_id, mission_id=mission_id)
        assert held == Binding(admitted, "bind", 0, receipt, "held", "owner_cancelled")
        # A second hold never rewrites the recorded revision or receipt.
        hold(cur, workspace_id=workspace_id, mission_id=mission_id, code="intake_revision_advanced")
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == held


def test_advancing_revision_holds_open_binding(world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
    assert advance_revision(store, workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id) == 1
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            admitted, "bind", 0, receipt, "held", INTAKE_REVISION_ADVANCED)


def test_hold_bindings_scopes_to_mission(world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    other_mission = uuid4()
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        cur.execute(
            "INSERT INTO omp_work.project_missions(workspace_id,mission_id,project_id,objective,status) "
            "SELECT %s,%s,project_id,'other','draft' FROM omp_work.projects "
            "WHERE workspace_id=%s ORDER BY project_id LIMIT 1",
            (workspace_id, other_mission, workspace_id),
        )
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
        hold_bindings(cur, workspace_id=workspace_id, mission_id=other_mission,
                      code="intake_revision_advanced")
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            admitted, "bind", 0, receipt, "admitted", None)


def test_append_revision_holds_prepared_and_admitted_in_one_commit(service, world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        prepared = prepare(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind", revision=0)
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=workspace_id, run_id="bind", revision=0).body
        body.snapshot.revision += 1
        body.binding.revision += 1
        body.assessments = []
        body.derivations = []
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                               expected_revision=0, operation_id=operation_id)
        assert append_revision(cur, locked, body, operation_id=operation_id) == 1
        # Within the appender's own transaction both open rows and the head move
        # together, so no reader can observe the new head against a stale binding.
        assert read_head(cur, workspace_id=workspace_id, mission_id=mission_id).revision == 1
        cur.execute(
            "SELECT binding_id,revision,assessment_id,state,hold_code "
            "FROM omp_work.mission_intake_bindings "
            "WHERE workspace_id=%s AND mission_id=%s",
            (workspace_id, mission_id),
        )
        assert set(cur.fetchall()) == {
            (prepared, 0, None, "held", INTAKE_REVISION_ADVANCED),
            (admitted, 0, receipt, "held", INTAKE_REVISION_ADVANCED),
        }
        conn.commit()
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            admitted, "bind", 0, receipt, "held", INTAKE_REVISION_ADVANCED)


def test_new_pass_leaves_history_held_until_explicit_admission(world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
    advance_revision(store, workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id)
    new_receipt = record_pass(store, workspace_id=workspace_id, actor_id=OWNER, mission_id=mission_id)
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            admitted, "bind", 0, receipt, "held", INTAKE_REVISION_ADVANCED)
        new_admission = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                              revision=1, assessment_id=new_receipt)
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            new_admission, "bind", 1, new_receipt, "admitted", None)
        cur.execute(
            "SELECT revision,assessment_id,state,hold_code FROM omp_work.mission_intake_bindings "
            "WHERE workspace_id=%s AND binding_id=%s", (workspace_id, admitted),
        )
        assert cur.fetchone() == (0, receipt, "held", INTAKE_REVISION_ADVANCED)


def test_rolled_back_revision_keeps_prior_admission(service, world):
    store, scope, receipt = world
    workspace_id, mission_id = scope["workspace_id"], scope["mission_id"]
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        admitted = admit(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                         revision=0, assessment_id=receipt)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        body = read_revision(cur, workspace_id=workspace_id, run_id="bind", revision=0).body
        body.snapshot.revision += 1
        body.binding.revision += 1
        body.assessments = []
        body.derivations = []
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id, run_id="bind",
                               expected_revision=0, operation_id=operation_id)
        append_revision(cur, locked, body, operation_id=operation_id)
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id).state == "held"
        conn.rollback()
    with cursor(store, workspace_id=workspace_id, actor_id=OWNER) as cur:
        assert read_head(cur, workspace_id=workspace_id, mission_id=mission_id).revision == 0
        assert read(cur, workspace_id=workspace_id, mission_id=mission_id) == Binding(
            admitted, "bind", 0, receipt, "admitted", None)
        cur.execute(
            "SELECT binding_id,revision,assessment_id,state FROM omp_work.mission_intake_bindings "
            "WHERE workspace_id=%s AND mission_id=%s", (workspace_id, mission_id),
        )
        assert cur.fetchall() == [(admitted, 0, receipt, "admitted")]
