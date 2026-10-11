"""Intake commands expose wire revisions without changing storage history."""
from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.v1.intake_commands import execute_intake, intake_actor, read_intake_view
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import connection, revision_body, write_repository  # noqa: F401
from test_nsi2_wire import PAYLOADS
from test_nsi_schemas import ENTITY, PREDICATE
from test_workflow_service import OWNER, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def envelope(workspace_id, mission_id=None, kind="start_intake"):
    payload = dict(PAYLOADS[kind])
    if kind == "start_intake":
        payload.update(mission_id=mission_id, entities=[ENTITY],
                       ontology=dict(predicates=[PREDICATE], dimensions=[]))
    return CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
        command=dict(type=kind, payload=payload),
    ))


def test_start_persists_storage_zero_and_returns_wire_one(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        result, event = execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        assert result == event == dict(type="start_intake", run_id="run-1",
                                       expected_revision=0, revision=1)
        for value in ("run-1", "run-1@1"):
            view = read_intake_view(cur, workspace_id, value)
            assert view.revision == view.provenance.revision == 1
            assert view.snapshot.revision == 0
            assert view.snapshot.state == "collecting"
            assert view.snapshot.entities[0].model_dump() == ENTITY
            assert view.snapshot.ontology.predicates[0].model_dump() == PREDICATE
            assert view.snapshot.claims == view.snapshot.artifacts == []
            assert (view.evidence_links, view.criterion_links, view.decisions,
                    view.issues, view.traces, view.assessments) == ((),) * 6
            assert view.provenance.source_sha256 == hashlib.sha256(b"").hexdigest()
        stored = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=0)
        assert (stored.body.binding.claim_set_hash, stored.body.binding.source_hash) == revision_hashes(stored.snapshot, {})
        assert (stored.body.binding.rule_bundle_hash, stored.body.binding.schema_hash,
                stored.body.binding.contract_hash) == (content_hash({}),) * 3
        assert conn.execute(
            "SELECT revision FROM omp_work.intake_revisions WHERE workspace_id=%s AND run_id='run-1'",
            (workspace_id,),
        ).fetchall() == [(0,)]


@pytest.mark.parametrize("foreign", [False, True])
def test_missing_or_foreign_mission_rejected_without_creating_head(service, write_repository, foreign):
    home_workspace, mission_id = write_repository
    workspace_id = uuid4() if foreign else home_workspace
    if not foreign:
        mission_id = uuid4()
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError) as exc:
            execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        assert exc.value.code == "invalid_request"
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)


def test_second_wire_zero_start_is_stale(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        with pytest.raises(WorkStoreError) as exc:
            execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        assert exc.value.code == "stale_intake"
        assert read_intake_view(cur, workspace_id, "run-1").revision == 1


def test_client_cannot_start_intake(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError) as exc:
            execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "client")
        assert exc.value.code == "forbidden"
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)


@pytest.mark.parametrize("kind,expected", [
    ("owner", "owner"), ("model", "model"), ("tool", "tool"),
    ("rule_engine", "rule_engine"), ("agent", "model"), ("task-agent", "model"),
])
def test_authenticated_actor_maps_to_nsi_authority(kind, expected):
    actor = intake_actor(kind, OWNER)
    assert actor.kind == expected
    assert actor.id == str(OWNER)


@pytest.mark.parametrize("kind", ["client", "service", "unknown"])
def test_untrusted_actor_kind_is_forbidden(kind):
    with pytest.raises(WorkStoreError) as exc:
        intake_actor(kind, OWNER)
    assert exc.value.code == "forbidden"


@pytest.mark.parametrize("kind", ["register_intake_evidence", "record_intake_claim"])
def test_follow_on_stubs_report_unavailable(service, write_repository, kind):
    workspace_id, _ = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError) as exc:
            execute_intake(cur, envelope(workspace_id, kind=kind), OWNER, "owner")
        assert exc.value.code == "unavailable"
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)


def test_head_and_pinned_reads_preserve_history_bytes_and_evidence(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        body = revision_body(workspace_id, revision=1)
        span = body["snapshot"]["claims"][0]["evidence"][0]
        body["evidence_links"] = [dict(claim_id=body["snapshot"]["claims"][0]["id"],
                                       relation="supports", **span)]
        locked = lock_revision(cur, workspace_id=workspace_id, run_id="run-1",
                               expected_revision=0, mission_id=mission_id, operation_id=uuid4())
        append_revision(cur, locked, body, operation_id=uuid4())
        head = read_intake_view(cur, workspace_id, "run-1")
        assert head.revision == head.provenance.revision == 2
        assert head.snapshot.revision == 1
        assert head.provenance.source_sha256 == hashlib.sha256(b"hello\x00\xff\r\n").hexdigest()
        assert any(link.span.model_dump() == span and link.relation == "supports"
                   for link in head.evidence_links)
        assert read_intake_view(cur, workspace_id, "run-1@2") == head
        assert read_intake_view(cur, workspace_id, "run-1@1").snapshot.artifacts == []


@pytest.mark.parametrize("value", ["missing", "missing@1", "run-1@0", "run-1@x", "run-1@2"])
def test_missing_or_invalid_revision_is_invalid_request(service, write_repository, value):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        with pytest.raises(WorkStoreError) as exc:
            read_intake_view(cur, workspace_id, value)
        assert exc.value.code == "invalid_request"
