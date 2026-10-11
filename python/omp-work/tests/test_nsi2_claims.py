"""Intake commands expose wire revisions without changing storage history."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.v1.intake_commands import execute_intake, intake_actor, read_intake_view
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.service import WorkService
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import connection, revision_body, write_repository  # noqa: F401
from test_nsi2_wire import PAYLOADS
from test_nsi_schemas import ENTITY, PREDICATE
from test_project_store import _workspace
from test_workflow_service import OWNER, _command, _grant, _owner_headers, _seed_project, service  # noqa: F401

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


@pytest.fixture
def http_intake(service):
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _grant(service, workspace_id)
    _workspace(service, workspace_id)
    _seed_project(service, workspace_id, project_id)
    with connection(service, workspace_id) as conn:
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id,mission_id,project_id,objective,status) "
            "VALUES (%s,%s,%s,'Intake','draft')", (workspace_id, mission_id, project_id),
        )
    return workspace_id, mission_id


def intake_capability(service, workspace_id, *, kind="agent", scopes=("work.mutate", "work.read")):
    actor_id = uuid4()
    token = str(actor_id)
    capability = service.capabilities / f"{actor_id}.json"
    capability.write_text(json.dumps(dict(
        token=token, actor_id=str(actor_id), actor_kind=kind,
        workspaces=[str(workspace_id)], scopes=list(scopes),
    )))
    capability.chmod(0o600)
    return actor_id, token


def test_http_start_read_replay_and_conflict_preserve_mission_and_principal(service, http_intake):
    workspace_id, mission_id = http_intake
    actor_id, token = intake_capability(service, workspace_id)
    command = envelope(workspace_id, mission_id).command.model_dump(mode="json")
    operation_id = uuid4()
    status, body = _command(service, workspace_id, command, token=token, operation_id=operation_id)
    assert status == 200
    assert body["result"]["revision"] == 1
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/run-1", headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    view = response.json()
    assert view["mission_id"] == str(mission_id)
    assert view["revision"] == view["provenance"]["revision"] == 1
    assert view["snapshot"]["revision"] == 0
    status, replay = _command(service, workspace_id, command, token=token, operation_id=operation_id)
    assert status == 200
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == body["result"]
    changed = {**command, "payload": {**command["payload"], "entities": []}}
    status, conflict = _command(service, workspace_id, changed, token=token, operation_id=operation_id)
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT revision FROM omp_work.intake_revisions").fetchall() == [(0,)]
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchall() == [(0,)]
        assert conn.execute(
            "SELECT event_type,aggregate_type,aggregate_id,actor_id,actor_kind,payload "
            "FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence", (workspace_id,),
        ).fetchall() == [
            ("start_intake", "mission", mission_id, actor_id, "agent", body["result"]),
            ("idempotency_conflict", "mission", mission_id, actor_id, "agent", {"code": "idempotency_conflict"}),
        ]


def test_http_foreign_mission_is_invalid_without_head(service, http_intake):
    workspace_id, _ = http_intake
    foreign_workspace, project_id, foreign_mission = uuid4(), uuid4(), uuid4()
    _grant(service, foreign_workspace)
    _workspace(service, foreign_workspace)
    _seed_project(service, foreign_workspace, project_id)
    with connection(service, foreign_workspace) as conn:
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id,mission_id,project_id,objective,status) "
            "VALUES (%s,%s,%s,'Foreign','draft')", (foreign_workspace, foreign_mission, project_id),
        )
    status, body = _command(service, workspace_id,
                            envelope(workspace_id, foreign_mission).command.model_dump(mode="json"))
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)


def test_http_second_start_is_stale(service, http_intake):
    workspace_id, mission_id = http_intake
    command = envelope(workspace_id, mission_id).command.model_dump(mode="json")
    assert _command(service, workspace_id, command)[0] == 200
    status, body = _command(service, workspace_id, command)
    assert status == 409
    assert body["error"]["code"] == "stale_intake"


@pytest.mark.parametrize("kind,scopes,foreign_workspace", [
    ("unknown", ("work.mutate", "work.read"), False),
    ("agent", ("work.read",), False),
    ("agent", ("work.mutate", "work.read"), True),
])
def test_http_start_refuses_untrusted_actor_scope_or_workspace(service, http_intake, kind, scopes, foreign_workspace):
    workspace_id, mission_id = http_intake
    _, token = intake_capability(service, uuid4() if foreign_workspace else workspace_id,
                                kind=kind, scopes=scopes)
    status, body = _command(service, workspace_id,
                            envelope(workspace_id, mission_id).command.model_dump(mode="json"), token=token)
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)


@pytest.mark.parametrize("foreign_workspace", [False, True])
def test_http_get_requires_read_and_workspace(service, http_intake, foreign_workspace):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    _, token = intake_capability(
        service, uuid4() if foreign_workspace else workspace_id,
        scopes=("work.read",) if foreign_workspace else ("work.mutate",),
    )
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/run-1",
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_http_supersede_requires_approval_scope(service, http_intake):
    workspace_id, _ = http_intake
    _, token = intake_capability(service, workspace_id, kind="owner", scopes=("work.mutate",))
    status, body = _command(service, workspace_id,
                            envelope(workspace_id, kind="supersede_intake_claim").command.model_dump(mode="json"),
                            token=token)
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert WorkService._scopes["supersede_intake_claim"] == "work.approve"


@pytest.mark.parametrize("kind", ["register_intake_evidence", "record_intake_claim"])
@pytest.mark.parametrize("conflict", [False, True])
def test_follow_on_journal_uses_head_mission_for_events_and_conflicts(service, http_intake, kind, conflict):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    command = envelope(workspace_id, kind=kind)
    event_type = "idempotency_conflict" if conflict else kind
    event = {"code": "idempotency_conflict"} if conflict else {
        "type": kind, "mission_id": str(uuid4()), "run_id": "run-1", "revision": 2,
    }
    actor_id = uuid4()
    store = PostgresWorkStore(service.config)
    with store._transaction(workspace_id, actor_id) as cur:
        store._record_event(cur, command, actor_id, "agent", event, event_type=event_type)
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT aggregate_type,aggregate_id,actor_id,actor_kind,payload "
            "FROM omp_audit.domain_events WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, command.operation_id),
        ).fetchone() == ("mission", mission_id, actor_id, "agent", event)


def test_follow_on_journal_rejects_missing_head_instead_of_using_result_mission(service, http_intake):
    workspace_id, mission_id = http_intake
    command = envelope(workspace_id, kind="record_intake_claim")
    store = PostgresWorkStore(service.config)
    with pytest.raises(WorkStoreError) as exc:
        with store._transaction(workspace_id, OWNER) as cur:
            store._record_event(cur, command, OWNER, "owner", {"mission_id": str(mission_id)})
    assert exc.value.code == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id=%s", (workspace_id,),
        ).fetchone() == (0,)
