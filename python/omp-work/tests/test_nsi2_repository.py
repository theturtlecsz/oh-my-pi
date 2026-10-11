"""NSI-2 repository boundaries: isolated, typed, append-only revision history."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from omp_work.intake_repository import (
    IntakeRepositoryError, IntakeRevisionBody, append_revision, lock_revision, read_revision,
    revision_hashes,
)
from test_nsi_schemas import (
    ARTIFACT, ASSESSMENT, BINDING, CLAIM, CONFLICT, CRITERION, DECISION,
    DIMENSION, ENTITY, GAP, PREDICATE, QUESTION, RECEIPT,
)

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


@pytest.fixture
def write_repository(service):
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _workspace(service, workspace_id)
    _insert_project(service, workspace_id, project_id, "NSI writes")
    with connection(service, workspace_id) as conn:
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id,mission_id,project_id,objective,status) "
            "VALUES (%s,%s,%s,'Intake','draft')", (workspace_id, mission_id, project_id),
        )
    return workspace_id, mission_id


def revision_body(workspace_id, revision=0):
    claim = deepcopy(CLAIM)
    claim.update(predicate_id=PREDICATE["id"], premise_ids=["prior"], evidence=[
        dict(artifact_id=ARTIFACT["id"], start_byte=0, end_byte=5, quote="hello"),
    ])
    prior = {**CLAIM, "id": "prior", "predicate_id": PREDICATE["id"]}
    conflict = {**CONFLICT, "predicate_id": PREDICATE["id"], "claim_ids": [claim["id"], "prior"]}
    gap = {**GAP, "dimension_id": DIMENSION["id"], "premise_id": "prior"}
    return deepcopy(dict(
        workspace_id=workspace_id,
        snapshot=dict(run_id="run-1", revision=revision, state="collecting", artifacts=[ARTIFACT],
                      entities=[ENTITY], claims=[claim, prior],
                      ontology=dict(predicates=[PREDICATE], dimensions=[DIMENSION])),
        binding={**BINDING, "revision": revision},
        artifact_bytes={ARTIFACT["id"]: b"hello\x00\xff\r\n"},
        conflicts=[conflict], gaps=[gap],
        questions=[{**QUESTION, "conflict_ids": [conflict["id"]]}],
        decisions=[dict(decision={**DECISION, "revision": revision}, settled_claim_ids=[claim["id"]])],
        instructions=[dict(id="instruction", decision_id=DECISION["id"], claim_id=claim["id"],
                           artifact_id=ARTIFACT["id"], instruction="Keep exact bytes")],
        criteria=[CRITERION],
        assessments=[dict(id=f"assessment-{revision}", assessment={**ASSESSMENT, "revision": 0,
                                                       "gaps": [], "conflicts": []})],
        derivations=[{**RECEIPT, "revision": revision, "premise_ids": ["prior"],
                      "conclusion_ids": [claim["id"]]}],
        supersessions={claim["id"]: ["prior"]},
    ))


def locked(cur, repository, expected=-1, operation_id=None):
    workspace_id, mission_id = repository
    return lock_revision(cur, workspace_id=workspace_id, run_id="run-1",
                         expected_revision=expected, mission_id=mission_id,
                         operation_id=operation_id or uuid4())


def intake_tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT c.table_name FROM information_schema.columns c "
        "WHERE c.table_schema='omp_work' AND c.column_name='nsi_version' "
        "AND c.table_name LIKE 'intake_%'",
    ).fetchall()]


def test_read_legacy_inserts_reconstructs_provenance_and_defaults_relation(service, write_repository):
    workspace_id, _ = write_repository
    operation_id = uuid4()
    common = dict(workspace_id=workspace_id, run_id="run-1", revision=0,
                  operation_id=operation_id, nsi_version="nsi/v1")
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        locked(cur, write_repository, operation_id=operation_id)
        insert(conn, "intake_revisions", common | dict(state="collecting", **{
            f"{name}_hash": "a" * 64 for name in ("source", "claim_set", "rule_bundle", "schema", "contract")
        }))
        insert(conn, "intake_artifacts", common | dict(
            artifact_id="source", bytes=b"hello", uri="file:///source",
            media_type="text/plain", content_hash="a" * 64))
        insert(conn, "intake_entities", common | dict(entity_id=ENTITY["id"], entity_type=ENTITY["type"]))
        insert(conn, "intake_ontology", common | dict(
            definition_id=PREDICATE["id"], kind="predicate", **{k: v for k, v in PREDICATE.items() if k != "id"}))
        for origin in ("owner_statement", "policy_rule"):
            insert(conn, "intake_claims", common | dict(
                claim_id=origin, category=CLAIM["category"], subject_entity_id=ENTITY["id"],
                predicate_id=PREDICATE["id"], value=Jsonb(CLAIM["value"]), origin=origin,
                state="proposed", modality="desired", polarity="positive", truth="unknown",
                asserted_by="asserter", admitted_by="admitter"))
        insert(conn, "intake_claim_evidence", common | dict(
            claim_id="owner_statement", artifact_id="source", start_byte=0, end_byte=5, quote="hello"))
        view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        artifact = view.snapshot.artifacts[0]
        assert artifact.actor.id == str(operation_id)
        assert artifact.actor.kind == "service"
        assert artifact.revision == "0"
        claims = {claim.id: claim for claim in view.snapshot.claims}
        assert claims["owner_statement"].asserted_by.kind == "owner"
        assert claims["owner_statement"].admitted_by.kind == "owner"
        assert claims["policy_rule"].asserted_by.kind == "rule_engine"
        assert claims["policy_rule"].admitted_by.kind == "rule_engine"
        assert view.body.evidence_links[0].model_dump() == dict(
            claim_id="owner_statement", artifact_id="source", start_byte=0, end_byte=5,
            quote="hello", relation="neither")


def test_append_preserves_source_and_claim_provenance_and_evidence_relations(service, write_repository):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    body["assessments"] = []
    artifact = body["snapshot"]["artifacts"][0]
    artifact.update(actor=dict(id="source-owner", kind="owner"), revision="source-rev-7")
    claim = body["snapshot"]["claims"][0]
    claim.update(origin="owner_statement", asserted_by=dict(id="claim-owner", kind="owner"),
                 admitted_by=dict(id="admission-engine", kind="rule_engine"))
    claim["evidence"].extend([
        dict(artifact_id=artifact["id"], start_byte=5, end_byte=6),
        dict(artifact_id=artifact["id"], start_byte=6, end_byte=7),
    ])
    body["evidence_links"] = [
        dict(claim_id=claim["id"], **span, relation=relation)
        for span, relation in zip(claim["evidence"], ("supports", "contradicts"))
    ]
    original = IntakeRevisionBody.model_validate(body)
    hashes = revision_hashes(original.snapshot, original.artifact_bytes)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        assert view.snapshot.artifacts == original.snapshot.artifacts
        restored = next(item for item in view.snapshot.claims if item.id == claim["id"])
        assert restored.asserted_by == original.snapshot.claims[0].asserted_by
        assert restored.admitted_by == original.snapshot.claims[0].admitted_by
        assert [link.model_dump() for link in sorted(view.body.evidence_links,
                                                   key=lambda link: link.start_byte)] == [
            {"quote": None, **span, "claim_id": claim["id"], "relation": relation}
            for span, relation in zip(claim["evidence"], ("supports", "contradicts", "neither"))
        ]
        assert (view.claim_set_hash, view.source_hash) == hashes
        # Copying a read body into the next revision retains evidence relations.
        copied = view.body.model_dump(exclude_none=True)
    copied["snapshot"]["revision"] = copied["binding"]["revision"] = 1
    for item in copied["decisions"]:
        item["decision"]["revision"] = 1
    for item in copied["derivations"]:
        item["revision"] = 1
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository, 0), copied, operation_id=uuid4())
        next_view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=1)
        assert next_view.body.evidence_links == view.body.evidence_links
        assert next_view.snapshot.artifacts == view.snapshot.artifacts


def test_write_helpers_leave_initial_and_existing_writes_to_caller_rollback(service, write_repository):
    workspace_id, _ = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        assert append_revision(cur, locked(cur, write_repository), revision_body(workspace_id),
                               operation_id=uuid4()) == 0
        conn.rollback()
    with connection(service, workspace_id) as conn:
        for table in intake_tables(conn):
            assert conn.execute(sql.SQL("SELECT count(*) FROM omp_work.{}").format(sql.Identifier(table))).fetchone() == (0,)
        with conn.cursor() as cur:
            append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository, 0), revision_body(workspace_id, 1), operation_id=uuid4())
        conn.rollback()
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        for table in intake_tables(conn):
            if table != "intake_heads":
                assert conn.execute(sql.SQL("SELECT count(*) FROM omp_work.{} WHERE revision=1").format(sql.Identifier(table))).fetchone() == (0,)


def test_append_stores_every_typed_row_with_operation_and_original_bytes(service, write_repository):
    workspace_id, _ = write_repository
    initial, appended = uuid4(), uuid4()
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository, operation_id=initial),
                        revision_body(workspace_id), operation_id=initial)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository, 0), revision_body(workspace_id, 1), operation_id=appended)
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision,operation_id FROM omp_work.intake_heads").fetchone() == (1, initial)
        assert conn.execute("SELECT bytes,uri FROM omp_work.intake_artifacts WHERE revision=1").fetchone() == (
            revision_body(workspace_id)["artifact_bytes"][ARTIFACT["id"]], ARTIFACT["uri"],
        )
        # Assessment written in revision 1 still assesses revision 0.
        assert conn.execute("SELECT revision,assessed_revision FROM omp_work.intake_assessments ORDER BY assessment_id").fetchall() == [(0, 0), (0, 0)]
        for table in intake_tables(conn):
            if table != "intake_heads":
                assert {row[0] for row in conn.execute(
                    sql.SQL("SELECT DISTINCT operation_id FROM omp_work.{}").format(sql.Identifier(table)),
                ).fetchall()} == {initial, appended}


def test_lock_rejects_stale_heads_and_missing_missions(service, write_repository):
    workspace_id, _ = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="stale_revision"):
            locked(cur, write_repository, 0)
        with pytest.raises(IntakeRepositoryError, match="stale_revision"):
            locked(cur, (workspace_id, uuid4()))
        append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        for expected in (-1, 1):
            with pytest.raises(IntakeRepositoryError, match="stale_revision"):
                locked(cur, write_repository, expected)
        with pytest.raises(IntakeRepositoryError, match="stale_revision"):
            locked(cur, (workspace_id, uuid4()), 0)


@pytest.mark.parametrize("initial", [True, False])
def test_only_one_writer_can_append_from_same_expected_head(service, write_repository, initial):
    workspace_id, _ = write_repository
    if not initial:
        with connection(service, workspace_id) as conn, conn.cursor() as cur:
            append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
    expected = -1 if initial else 0
    started = Event()
    pid = []

    def contender():
        with connection(service, workspace_id) as conn, conn.cursor() as cur:
            conn.execute("SET LOCAL lock_timeout='5s'")
            pid.append(conn.info.backend_pid)
            started.set()
            with pytest.raises(IntakeRepositoryError, match="stale_revision"):
                locked(cur, write_repository, expected)

    with connection(service, workspace_id) as first, first.cursor() as cur, ThreadPoolExecutor(max_workers=1) as pool:
        head = locked(cur, write_repository, expected)
        future = pool.submit(contender)
        try:
            assert started.wait(5)
            with connection(service, workspace_id, "postgres") as observer:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    # Clear stats snapshot to observe the contender's lock wait.
                    observer.execute("SELECT pg_stat_clear_snapshot()")
                    if observer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid[0],)).fetchone() == ("Lock",):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("second writer never waited for caller's head lock")
            append_revision(cur, head, revision_body(workspace_id, expected + 1), operation_id=uuid4())
            first.commit()
        finally:
            first.rollback()
        future.result(timeout=6)
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (expected + 1,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (expected + 2,)


@pytest.mark.parametrize("consumer", ["subject", "value", "answer", "expected_result"])
def test_append_rejects_entity_id_type_mismatch_before_writing(service, write_repository, consumer):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    bad_ref = {**ENTITY, "type": "wrong"}
    if consumer == "subject":
        body["snapshot"]["claims"][0]["subject"] = bad_ref
    else:
        value = dict(type="entity", value=bad_ref)
        if consumer == "value":
            body["snapshot"]["claims"][0]["value"] = value
        elif consumer == "answer":
            body["decisions"][0]["decision"]["answer"] = value
        else:
            body["criteria"][0]["expected_result"] = value
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="entity_type_mismatch"):
            append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (0,)
        conn.rollback()


def test_append_rejects_uri_rebind_without_advancing_head(service, write_repository):
    workspace_id, _ = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
    body = revision_body(workspace_id, 1)
    body["artifact_bytes"][ARTIFACT["id"]] = b"replacement"
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="uri_rebind"):
            append_revision(cur, locked(cur, write_repository, 0), body, operation_id=uuid4())
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (1,)


@pytest.mark.parametrize("field", ["workspace", "snapshot_run", "decision_run", "assessment_run"])
def test_append_rejects_rows_outside_locked_workspace_and_run(service, write_repository, field):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    if field == "workspace":
        body["workspace_id"] = uuid4()
    elif field == "snapshot_run":
        body["snapshot"]["run_id"] = "other"
    elif field == "decision_run":
        body["decisions"][0]["decision"]["run_id"] = "other"
    else:
        body["assessments"][0]["assessment"]["run_id"] = "other"
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(IntakeRepositoryError, match="scope_mismatch"):
            append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
        conn.rollback()


def test_late_insert_failure_leaves_head_unadvanced_and_caller_can_rollback(service, write_repository):
    workspace_id, _ = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
    body = revision_body(workspace_id, 1)
    body["derivations"][0]["conclusion_ids"] = ["missing"]
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            append_revision(cur, locked(cur, write_repository, 0), body, operation_id=uuid4())
        conn.rollback()
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (1,)


def test_append_revalidates_mutated_nsi_models_before_writing(service, write_repository):
    workspace_id, _ = write_repository
    body = IntakeRevisionBody.model_validate(revision_body(workspace_id))
    body.snapshot.claims[0].evidence[0].start_byte = -1
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(ValidationError):
            append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (0,)
        conn.rollback()


def test_typed_entity_values_store_json_and_qualified_entity_columns(service, write_repository):
    workspace_id, _ = write_repository
    body = revision_body(workspace_id)
    value = dict(type="entity", value=ENTITY)
    body["snapshot"]["claims"][0]["value"] = value
    body["decisions"][0]["decision"]["answer"] = value
    body["criteria"][0]["expected_result"] = value
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), body, operation_id=uuid4())
    with connection(service, workspace_id) as conn:
        for table, column, id_column, record_id in (
            ("intake_claims", "value", "claim_id", CLAIM["id"]),
            ("intake_owner_decisions", "answer", "decision_id", DECISION["id"]),
            ("intake_criteria", "expected_result", "criterion_id", CRITERION["id"]),
        ):
            assert conn.execute(sql.SQL(
                "SELECT {},value_entity_id,value_entity_type FROM omp_work.{} WHERE {}=%s",
            ).format(sql.Identifier(column), sql.Identifier(table), sql.Identifier(id_column)),
                (record_id,)).fetchone() == (value, ENTITY["id"], ENTITY["type"])


def test_read_history_preserves_bytes_hashes_and_relational_links(service, write_repository):
    workspace_id, _ = write_repository
    bodies = []
    for revision in (0, 1):
        body = revision_body(workspace_id, revision)
        # Preserve a noncanonical entity type in both subject and typed values.
        entity = dict(id=ENTITY["id"], type="custom-service")
        body["snapshot"]["entities"] = [entity]
        body["snapshot"]["claims"][0]["subject"] = entity
        body["snapshot"]["claims"][1]["subject"] = entity
        body["snapshot"]["claims"][0]["value"] = dict(type="entity", value=entity)
        body["snapshot"]["claims"][1]["value"] = dict(type="integer", value=revision)
        # Relational span keys reconstruct byte order, not insertion order.
        body["snapshot"]["claims"][0]["evidence"].insert(0, dict(
            artifact_id=ARTIFACT["id"], start_byte=5, end_byte=6))
        body["decisions"][0]["decision"]["answer"] = dict(type="entity", value=entity)
        body["criteria"][0]["expected_result"] = dict(type="entity", value=entity)
        body["snapshot"]["artifacts"][0]["uri"] += f"/{revision}"
        body["artifact_bytes"][ARTIFACT["id"]] += bytes([revision])
        parsed = IntakeRevisionBody.model_validate(body)
        claim_hash, source_hash = revision_hashes(parsed.snapshot, parsed.artifact_bytes)
        body["binding"].update(claim_set_hash=claim_hash, source_hash=source_hash)
        bodies.append(IntakeRevisionBody.model_validate(body))
        with connection(service, workspace_id) as conn, conn.cursor() as cur:
            append_revision(cur, locked(cur, write_repository, revision - 1), body, operation_id=uuid4())
            # Read also works before caller commits.
            assert read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=revision).artifact_bytes == body["artifact_bytes"]
    views = []
    with connection(service, workspace_id, "omp_work_readonly") as conn, conn.cursor() as cur:
        for revision, original in enumerate(bodies):
            view = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=revision)
            views.append(view)
            assert view.revision == revision
            assert view.artifact_bytes == original.artifact_bytes
            cur.execute("SELECT claim_set_hash,source_hash FROM omp_work.intake_revisions WHERE revision=%s", (revision,))
            assert (view.claim_set_hash, view.source_hash) == cur.fetchone()
            assert view.rule_bundle_hash == original.binding.rule_bundle_hash
            assert view.schema_hash == original.binding.schema_hash
            assert view.snapshot.entities == original.snapshot.entities
            assert view.snapshot.ontology == original.snapshot.ontology
            claims = {claim.id: claim for claim in view.snapshot.claims}
            assert claims[CLAIM["id"]].value == original.snapshot.claims[0].value
            assert claims[CLAIM["id"]].evidence == sorted(
                original.snapshot.claims[0].evidence, key=lambda span: span.start_byte)
            assert claims[CLAIM["id"]].premise_ids == ["prior"]
            assert view.body.decisions[0].settled_claim_ids == [CLAIM["id"]]
            assert view.body.decisions[0].decision.answer == original.decisions[0].decision.answer
            assert view.body.criteria == original.criteria
            assert view.body.instructions == original.instructions
            assert view.body.questions == original.questions
            assert view.body.gaps == original.gaps
            assert set(view.body.conflicts[0].claim_ids) == set(original.conflicts[0].claim_ids)
            assert view.body.supersessions == original.supersessions
            assert view.body.derivations[0].premise_ids == ["prior"]
            assert view.body.derivations[0].conclusion_ids == [CLAIM["id"]]
            assert view.body.assessments[0].id == f"assessment-{revision}"
            assessment = view.body.assessments[0].assessment
            assert assessment.revision == 0
            assert assessment.claim_set_hash == bodies[0].binding.claim_set_hash
            assert assessment.derivation_receipt_ids == [view.body.derivations[0].id]
    assert views[0].claim_set_hash != views[1].claim_set_hash
    assert views[0].source_hash != views[1].source_hash


def test_read_rejects_missing_and_foreign_revision_keys_and_foreign_links(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_revision(cur, locked(cur, write_repository), revision_body(workspace_id), operation_id=uuid4())
        for scope in ((workspace_id, "run-1", 1), (workspace_id, "missing", 0), (uuid4(), "run-1", 0)):
            with pytest.raises(IntakeRepositoryError, match="revision_not_found"):
                read_revision(cur, workspace_id=scope[0], run_id=scope[1], revision=scope[2])
    other_workspace = uuid4()
    _workspace(service, other_workspace)
    with connection(service, other_workspace) as conn, conn.cursor() as cur:
        # Supplying the actual owning key still cannot bypass caller's RLS scope.
        with pytest.raises(IntakeRepositoryError, match="revision_not_found"):
            read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        with pytest.raises(IntakeRepositoryError, match="stale_revision"):
            lock_revision(cur, workspace_id=other_workspace, run_id="run-1", expected_revision=-1,
                          mission_id=mission_id, operation_id=uuid4())
        # Matching run/revision strings in another workspace cannot qualify a link.
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert(conn, "intake_assessments", dict(
                workspace_id=other_workspace, run_id="run-1", revision=0, assessed_revision=0,
                assessment_id="foreign", status="needs_spec", assessed_by="engine",
                created_at="2026-01-01T00:00:00Z", operation_id=uuid4(), nsi_version="nsi/v1"))
