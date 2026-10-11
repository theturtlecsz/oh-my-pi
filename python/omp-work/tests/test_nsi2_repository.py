"""NSI-2 repository boundaries: isolated, typed, append-only revision history."""
from __future__ import annotations

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from test_project_store import _insert_project, _workspace
from test_workflow_service import OWNER, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@contextmanager
def connection(service, workspace_id, role="omp_work_app"):
    with psycopg.connect(**service.config.connection_kwargs(role)) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        yield conn


def insert(conn, table, values):
    conn.execute(
        sql.SQL("INSERT INTO omp_work.{} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    )


@pytest.fixture
def repository(service):
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, project_id, "NSI")
    common = dict(workspace_id=workspace_id, run_id="run", revision=0,
                  operation_id=uuid4(), nsi_version="nsi/v1")
    revision = dict(state="collecting", **{f"{name}_hash": "a" * 64 for name in
                    ("source", "claim_set", "rule_bundle", "schema", "contract")})
    records = {
        "intake_heads": dict(mission_id=mission_id, current_revision=0),
        "intake_revisions": revision,
        "intake_artifacts": dict(artifact_id="source", bytes=b"hello", uri="file:///source",
                                 media_type="text/plain", content_hash="a" * 64),
        "intake_ontology": dict(definition_id="predicate", kind="predicate", title="Uses"),
        "intake_entities": dict(entity_id="entity", entity_type="system"),
        "intake_claims": dict(claim_id="claim", category="domain_and_data_model",
                              subject_entity_id="entity", predicate_id="predicate",
                              value=Jsonb({"type": "string", "value": "hello"}),
                              origin="owner_statement", state="proposed", modality="desired",
                              polarity="positive", truth="unknown", asserted_by=str(OWNER)),
        "intake_claim_evidence": dict(claim_id="claim", artifact_id="source", start_byte=0,
                                      end_byte=5, quote="hello"),
        "intake_claim_premises": dict(claim_id="claim", premise_id="prior"),
        "intake_claim_supersessions": dict(claim_id="claim", superseded_claim_id="prior"),
        "intake_issues": dict(issue_id="question", kind="question", reason="Clarify", question="Which?"),
        "intake_issue_claims": dict(issue_id="question", claim_id="claim"),
        "intake_question_issues": dict(question_id="question", issue_id="question"),
        "intake_owner_decisions": dict(decision_id="decision", question_id="question", owner_id=str(OWNER),
                                 reason="Chosen", created_at="2026-01-01T00:00:00Z",
                                 answer=Jsonb({"type": "boolean", "value": True})),
        "intake_settled_claims": dict(decision_id="decision", claim_id="claim"),
        "intake_instruction_provenances": dict(provenance_id="instruction", decision_id="decision",
                                              claim_id="claim", artifact_id="source", instruction="Use it"),
        "intake_criteria": dict(criterion_id="criterion", description="Works", verification_method="test",
                                requirement_ids=["requirement"], expected_result=Jsonb({"type": "null", "value": None})),
        "intake_assessments": dict(assessment_id="assessment", assessed_revision=0, status="needs_spec",
                                   assessed_by="engine", created_at="2026-01-01T00:00:00Z"),
        "intake_derivations": dict(derivation_id="derivation", rule_id="rule", derived_by="engine",
                                   created_at="2026-01-01T00:00:00Z"),
        "intake_derivation_claims": dict(derivation_id="derivation", claim_id="claim", role="conclusion"),
    }
    with connection(service, workspace_id) as conn:
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id,mission_id,project_id,objective,status) "
            "VALUES (%s,%s,%s,'Intake','draft')", (workspace_id, mission_id, project_id),
        )
        for table, fields in records.items():
            base = common if table != "intake_heads" else {k: v for k, v in common.items() if k != "revision"}
            insert(conn, table, base | fields)
            if table == "intake_entities":
                insert(conn, table, base | fields | dict(entity_id="value-entity", entity_type="component"))
            if table == "intake_claims":
                insert(conn, table, base | fields | dict(claim_id="prior"))
    return workspace_id, common, records


def test_bootstrap_head_advances_only_to_next_existing_revision(service, repository):
    workspace_id, common, records = repository
    with connection(service, workspace_id) as conn:
        insert(conn, "intake_revisions", common | records["intake_revisions"] | dict(revision=1))
        conn.execute("UPDATE omp_work.intake_heads SET current_revision=1 WHERE workspace_id=%s", (workspace_id,))
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s", (workspace_id,)).fetchone() == (1,)
        assert conn.execute("SELECT bytes,uri FROM omp_work.intake_artifacts WHERE workspace_id=%s", (workspace_id,)).fetchone() == (b"hello", "file:///source")
        for revision in (0, 1, 3):
            with pytest.raises(psycopg.Error, match="advance by one"), conn.transaction():
                conn.execute("UPDATE omp_work.intake_heads SET current_revision=%s WHERE workspace_id=%s", (revision, workspace_id))
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, "intake_revisions", common | records["intake_revisions"] | dict(revision=3))
    # Deferred head FK must still reject a nonexistent revision on commit.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with connection(service, workspace_id) as conn:
            conn.execute("UPDATE omp_work.intake_heads SET current_revision=2 WHERE workspace_id=%s", (workspace_id,))


def test_all_records_are_workspace_isolated_and_roles_have_only_allowed_grants(service, repository):
    workspace_id, common, records = repository
    with connection(service, workspace_id, "postgres") as conn:
        for table in records:
            assert conn.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
                (f"omp_work.{table}",),
            ).fetchone() == (True, True)
            for role in ("omp_work_app", "omp_work_readonly", "omp_work_importer"):
                privileges = conn.execute(
                    "SELECT has_table_privilege(%s,%s,'SELECT'),has_table_privilege(%s,%s,'INSERT'),"
                    "has_table_privilege(%s,%s,'UPDATE'),has_table_privilege(%s,%s,'DELETE'),"
                    "has_table_privilege(%s,%s,'TRUNCATE')",
                    (role, f"omp_work.{table}") * 5,
                ).fetchone()
                assert privileges == (role != "omp_work_importer", role == "omp_work_app", False, False, False)
            if table == "intake_heads":
                assert conn.execute("SELECT has_column_privilege('omp_work_app','omp_work.intake_heads','current_revision','UPDATE')").fetchone() == (True,)
    for role in ("omp_work_app", "omp_work_readonly", "omp_work_owner"):
        # SET ROLE proves FORCE RLS for table owner as well as application readers.
        with connection(service, uuid4(), "postgres") as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            for table in records:
                assert conn.execute(sql.SQL("SELECT count(*) FROM omp_work.{}").format(sql.Identifier(table))).fetchone() == (0,)
    with connection(service, uuid4()) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            insert(conn, "intake_artifacts", common | records["intake_artifacts"] | dict(artifact_id="foreign"))
    with connection(service, workspace_id, "omp_work_readonly") as conn:
        assert conn.execute("SELECT claim_id FROM omp_work.intake_claims ORDER BY claim_id").fetchall() == [("claim",), ("prior",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            insert(conn, "intake_artifacts", common | records["intake_artifacts"] | dict(artifact_id="readonly"))


@pytest.mark.parametrize("claim", ["workspace_id", "actor_id"])
def test_read_requires_both_workspace_and_actor_claims(service, repository, claim):
    workspace_id, _, records = repository
    with connection(service, workspace_id) as conn:
        conn.execute("SELECT set_config(%s, '', false)", (f"omp.{claim}",))
        for table in records:
            with pytest.raises(psycopg.Error, match="claim required"), conn.transaction():
                conn.execute(sql.SQL("SELECT * FROM omp_work.{}").format(sql.Identifier(table)))


def test_history_triggers_reject_updates_and_deletes_even_with_elevated_privileges(service, repository):
    workspace_id, _, records = repository
    with connection(service, workspace_id, "postgres") as conn:
        for table in records:
            for verb in ("UPDATE", "DELETE"):
                statement = f"UPDATE omp_work.{table} SET operation_id=operation_id" if verb == "UPDATE" else f"DELETE FROM omp_work.{table}"
                if table == "intake_heads" and verb == "UPDATE":
                    message = "advance by one"
                else:
                    message = "immutable|cannot be deleted"
                with pytest.raises(psycopg.Error, match=message), conn.transaction():
                    conn.execute(statement + " WHERE workspace_id=%s", (workspace_id,))
    with connection(service, workspace_id) as conn:
        for table in records:
            for statement in (
                f"UPDATE omp_work.{table} SET operation_id=operation_id",
                f"DELETE FROM omp_work.{table}",
                f"TRUNCATE omp_work.{table}",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                    conn.execute(statement)


@pytest.mark.parametrize("field,bad,error", [
    ("nsi_version", "nsi/v2", psycopg.errors.CheckViolation),
    ("operation_id", None, psycopg.errors.NotNullViolation),
])
def test_every_record_requires_version_and_operation_id(service, repository, field, bad, error):
    workspace_id, common, records = repository
    with connection(service, workspace_id) as conn:
        for table, values in records.items():
            base = common if table != "intake_heads" else {k: v for k, v in common.items() if k != "revision"}
            with pytest.raises(error), conn.transaction():
                insert(conn, table, base | values | {field: bad})


@pytest.mark.parametrize("table,field,id_field", [
    ("intake_claims", "value", "claim_id"),
    ("intake_owner_decisions", "answer", "decision_id"),
    ("intake_criteria", "expected_result", "criterion_id"),
])
def test_typed_values_and_entity_coherence_are_enforced_on_each_consumer(service, repository, table, field, id_field):
    workspace_id, common, records = repository
    valid = [
        {"type": "string", "value": "text"}, {"type": "integer", "value": 2.0},
        {"type": "number", "value": 1.25}, {"type": "boolean", "value": False},
        {"type": "null", "value": None}, {"type": "entity", "value": {"id": "value-entity", "type": "component"}},
    ]
    invalid = [
        {}, None, [], {"type": "string"}, {"value": "text"},
        {"type": "string", "value": 1}, {"type": "integer", "value": 1.5},
        {"type": "number", "value": "1"}, {"type": "boolean", "value": 0},
        {"type": "null", "value": "null"}, {"type": "array", "value": []},
        {"type": "entity", "value": "entity"}, {"type": "entity", "value": {"id": "entity"}},
        {"type": "entity", "value": {"id": "", "type": "system"}},
        {"type": "entity", "value": {"id": "entity", "type": "system", "extra": 1}},
        {"type": "string", "value": "text", "extra": 1},
    ]
    with connection(service, workspace_id) as conn:
        for index, value in enumerate(valid):
            values = common | records[table] | {id_field: f"valid-{index}", field: Jsonb(value)}
            if value["type"] == "entity":
                values |= dict(value_entity_id="value-entity", value_entity_type="component")
            insert(conn, table, values)
        for value in invalid:
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                insert(conn, table, common | records[table] | {id_field: "invalid", field: Jsonb(value)})
        entity_value = Jsonb(valid[-1])
        for columns in ({}, dict(value_entity_id="other", value_entity_type="component"),
                        dict(value_entity_id="value-entity", value_entity_type="other")):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                insert(conn, table, common | records[table] | {id_field: "incoherent", field: entity_value} | columns)
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            insert(conn, table, common | records[table] | {id_field: "nonentity", "value_entity_id": "entity", "value_entity_type": "system"})
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, table, common | records[table] | {id_field: "missing", field: Jsonb({"type": "entity", "value": {"id": "missing", "type": "system"}}), "value_entity_id": "missing", "value_entity_type": "system"})
        # Keep all other FKs valid: only the typed value's entity is absent locally.
        insert(conn, "intake_revisions", common | records["intake_revisions"] | dict(revision=1))
        for local in (dict(revision=1), dict(run_id="other")):
            if "run_id" in local:
                insert(conn, "intake_heads", {k: v for k, v in common.items() if k != "revision"} | records["intake_heads"] | local)
                insert(conn, "intake_revisions", common | records["intake_revisions"] | local)
            for dependency in ("intake_entities", "intake_ontology", "intake_issues"):
                insert(conn, dependency, common | records[dependency] | local)
            with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                insert(conn, table, common | records[table] | local | {
                    id_field: "foreign-entity", field: entity_value,
                    "value_entity_id": "value-entity", "value_entity_type": "component",
                })


def test_mission_and_assessment_references_are_qualified_by_workspace_and_run(service, repository):
    workspace_id, common, records = repository
    with connection(service, workspace_id) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, "intake_assessments", common | records["intake_assessments"] | dict(assessment_id="bad", assessed_revision=1))
        insert(conn, "intake_revisions", common | records["intake_revisions"] | dict(revision=1))
        insert(conn, "intake_heads", {k: v for k, v in common.items() if k != "revision"} | records["intake_heads"] | dict(run_id="other"))
        insert(conn, "intake_revisions", common | records["intake_revisions"] | dict(run_id="other"))
        # Revision 1 exists, but only in the original run.
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, "intake_assessments", common | records["intake_assessments"] | dict(run_id="other", assessed_revision=1))
    with connection(service, uuid4(), "postgres") as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, "intake_heads", {k: v for k, v in common.items() if k != "revision"} | records["intake_heads"] | dict(workspace_id=uuid4()))
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
