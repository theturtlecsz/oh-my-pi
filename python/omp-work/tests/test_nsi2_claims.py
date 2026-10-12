"""Intake commands expose wire revisions without changing storage history."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest

from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import append_revision, lock_revision, read_revision, revision_hashes
from omp_work.v1.api_models import RecordIntakeClaimResult
from omp_work.v1.canonical import sha256
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


def evidence_payload(actor_id, artifact_kind, *, text="aé🙂", expected_revision=1,
                     artifact_id="artifact-added", uri="https://example.test/evidence",
                     revision="source-revision-7", content_hash=None):
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "run_id": "run-1",
        "expected_revision": expected_revision,
        "artifact": {
            "id": artifact_id,
            "uri": uri,
            "content_hash": digest if content_hash is None else content_hash,
            "revision": revision,
            "actor": {"id": str(actor_id), "kind": artifact_kind},
            "media_type": "text/plain",
        },
        "source": {"text": text, "sha256": digest},
    }


def evidence_command(actor_id, artifact_kind, **kwargs):
    return {"type": "register_intake_evidence", "payload": evidence_payload(actor_id, artifact_kind, **kwargs)}


def evidence_envelope(workspace_id, actor_id, artifact_kind, **kwargs):
    return CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
        command=evidence_command(actor_id, artifact_kind, **kwargs),
    ))


def test_evidence_without_matching_head_is_stale_before_lock(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError) as missing:
            execute_intake(cur, evidence_envelope(workspace_id, OWNER, "owner"), OWNER, "owner")
        assert missing.value.code == "stale_intake"
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        with pytest.raises(WorkStoreError) as ahead:
            execute_intake(cur, evidence_envelope(workspace_id, OWNER, "owner", expected_revision=2),
                           OWNER, "owner")
        assert ahead.value.code == "stale_intake"
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT revision FROM omp_work.intake_revisions").fetchall() == [(0,)]


def test_model_principal_with_owner_artifact_is_forbidden(service, http_intake):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    _, token = intake_capability(service, workspace_id, kind="model")
    status, body = _command(
        service, workspace_id, evidence_command(OWNER, "owner"), token=token,
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute(
            "SELECT event_type FROM omp_audit.domain_events WHERE workspace_id=%s", (workspace_id,),
        ).fetchall() == [("start_intake",)]


def test_bad_content_hash_is_forbidden(service, http_intake):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    status, body = _command(
        service, workspace_id, evidence_command(OWNER, "owner", content_hash="b" * 64),
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (1,)


def test_owner_evidence_get_preserves_unicode_hash_kind_and_source_revision(service, http_intake):
    workspace_id, mission_id = http_intake
    text = "aé🙂"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    command = evidence_command(OWNER, "owner", text=text, revision="source-revision-7")
    operation_id = uuid4()
    status, body = _command(service, workspace_id, command, operation_id=operation_id)
    assert status == 200
    assert body["result"] == {
        "type": "register_intake_evidence", "run_id": "run-1",
        "expected_revision": 1, "revision": 2,
    }
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/run-1", headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    view = response.json()
    artifact = view["snapshot"]["artifacts"][0]
    assert artifact["content_hash"] == view["provenance"]["source_sha256"] == digest
    assert artifact["actor"] == {"id": str(OWNER), "kind": "owner"}
    assert artifact["revision"] == "source-revision-7"
    assert view["snapshot"]["revision"] == 1
    status, replay = _command(service, workspace_id, command, operation_id=operation_id)
    assert status == 200
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == body["result"]
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT revision FROM omp_work.intake_revisions ORDER BY revision",
        ).fetchall() == [(0,), (1,)]
        events = conn.execute(
            "SELECT event_type,payload FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence",
            (workspace_id,),
        ).fetchall()
        assert [event_type for event_type, _payload in events] == ["start_intake", "register_intake_evidence"]
        assert set(events[1][1]) == {"type", "run_id", "expected_revision", "revision"}
        assert "mission_id" not in events[1][1]
        assert events[1][1] == body["result"]


def test_agent_evidence_stores_model_and_journals_agent(service, http_intake):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    actor_id, token = intake_capability(service, workspace_id, kind="agent")
    status, body = _command(service, workspace_id, evidence_command(actor_id, "model"), token=token)
    assert status == 200
    assert body["result"]["expected_revision"] == 1
    assert body["result"]["revision"] == 2
    response = service.client.get(
        f"/v1/workspaces/{workspace_id}/intake/run-1", headers=_owner_headers(workspace_id),
    )
    assert response.status_code == 200
    artifact = response.json()["snapshot"]["artifacts"][0]
    assert artifact["actor"] == {"id": str(actor_id), "kind": "model"}
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT event_type,actor_id,actor_kind FROM omp_audit.domain_events "
            "WHERE workspace_id=%s AND event_type='register_intake_evidence'",
            (workspace_id,),
        ).fetchone() == ("register_intake_evidence", actor_id, "agent")


def test_evidence_collision_journals_mission_on_both_events(service, http_intake):
    workspace_id, mission_id = http_intake
    actor_id, token = intake_capability(service, workspace_id, kind="agent")
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"),
                    token=token)[0] == 200
    operation_id = uuid4()
    command = evidence_command(actor_id, "model", text="first")
    assert _command(service, workspace_id, command, token=token, operation_id=operation_id)[0] == 200
    status, conflict = _command(
        service, workspace_id, evidence_command(actor_id, "model", text="second"),
        token=token, operation_id=operation_id,
    )
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT event_type,aggregate_type FROM omp_audit.domain_events "
            "WHERE workspace_id=%s AND operation_id=%s ORDER BY sequence",
            (workspace_id, operation_id),
        ).fetchall() == [
            ("register_intake_evidence", "mission"),
            ("idempotency_conflict", "mission"),
        ]


def test_reused_expected_revision_is_stale_after_head_advances(service, http_intake):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    status, body = _command(service, workspace_id, evidence_command(OWNER, "owner", text="first"))
    assert status == 200
    assert body["result"]["expected_revision"] == 1
    assert body["result"]["revision"] == 2
    status, stale = _command(
        service, workspace_id,
        evidence_command(OWNER, "owner", text="second", artifact_id="artifact-next"),
    )
    assert status == 409
    assert stale["error"]["code"] == "stale_intake"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)
        assert conn.execute(
            "SELECT revision FROM omp_work.intake_revisions ORDER BY revision",
        ).fetchall() == [(0,), (1,)]
        response = service.client.get(
            f"/v1/workspaces/{workspace_id}/intake/run-1", headers=_owner_headers(workspace_id),
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 2


def test_evidence_copies_records_recomputes_hashes_and_rejects_rebind(service, write_repository):
    workspace_id, mission_id = write_repository
    text = "copied-source"
    raw = text.encode("utf-8")
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        seeded = revision_body(workspace_id, revision=1)
        seeded["snapshot"]["state"] = "reasoning"
        seeded["binding"]["rule_bundle_hash"] = "1" * 64
        seeded["binding"]["schema_hash"] = "2" * 64
        seeded["binding"]["contract_hash"] = "3" * 64
        claim = seeded["snapshot"]["claims"][0]
        seeded["evidence_links"] = [dict(
            claim_id=claim["id"], relation="supports", **claim["evidence"][0],
        )]
        append_revision(cur, lock_revision(
            cur, workspace_id=workspace_id, run_id="run-1", expected_revision=0,
            mission_id=mission_id, operation_id=uuid4(),
        ), seeded, operation_id=uuid4())
        previous = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=1)
        assert previous.body.decisions and previous.body.derivations
        result, event = execute_intake(
            cur, evidence_envelope(workspace_id, OWNER, "owner", text=text, expected_revision=2),
            OWNER, "owner",
        )
        assert result == event == {
            "type": "register_intake_evidence", "run_id": "run-1",
            "expected_revision": 2, "revision": 3,
        }
        assert "mission_id" not in event
        stored = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=2)
        assert stored.snapshot.state == previous.snapshot.state == "reasoning"
        assert stored.snapshot.entities == previous.snapshot.entities
        assert stored.snapshot.ontology == previous.snapshot.ontology
        assert stored.snapshot.claims == previous.snapshot.claims
        assert stored.snapshot.artifacts[:-1] == previous.snapshot.artifacts
        added = stored.snapshot.artifacts[-1]
        assert added.revision == "source-revision-7"
        assert added.actor.kind == "owner"
        assert stored.artifact_bytes[added.id] == raw
        for artifact_id, data in previous.artifact_bytes.items():
            assert stored.artifact_bytes[artifact_id] == bytes(data)
        assert [(link.claim_id, link.artifact_id, link.start_byte, link.end_byte, link.relation)
                for link in stored.body.evidence_links] == [
            (link.claim_id, link.artifact_id, link.start_byte, link.end_byte, link.relation)
            for link in previous.body.evidence_links]
        assert any(link.relation == "supports" for link in stored.body.evidence_links)
        assert stored.body.decisions == stored.body.assessments == stored.body.derivations == []
        assert (stored.body.binding.rule_bundle_hash, stored.body.binding.schema_hash,
                stored.body.binding.contract_hash) == ("1" * 64, "2" * 64, "3" * 64)
        assert (stored.body.binding.claim_set_hash, stored.body.binding.source_hash) == revision_hashes(
            stored.snapshot, stored.artifact_bytes,
        )
        with pytest.raises(WorkStoreError) as duplicate:
            execute_intake(cur, evidence_envelope(
                workspace_id, OWNER, "owner", text="other-id", expected_revision=3,
                artifact_id=added.id, uri="https://example.test/other",
            ), OWNER, "owner")
        assert duplicate.value.code == "invalid_request"
        with pytest.raises(WorkStoreError) as rebound:
            execute_intake(cur, evidence_envelope(
                workspace_id, OWNER, "owner", text="other-bytes", expected_revision=3,
                artifact_id="artifact-rebind", uri=added.uri,
            ), OWNER, "owner")
        assert rebound.value.code == "invalid_request"
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (2,)


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


def claim_payload(
    actor_id,
    actor_kind,
    *,
    claim_id="claim-1",
    expected_revision=2,
    artifact_id="artifact-added",
    relation="supports",
    predicate_id="depends_on",
    evidence=None,
    evidence_links=None,
    premise_ids=None,
    subject=None,
    asserted_by=None,
    value=None,
):
    span = {"artifact_id": artifact_id, "start_byte": 0, "end_byte": 3, "quote": "aé🙂"}
    if evidence is None:
        evidence = [span]
    if evidence_links is None:
        evidence_links = [{"span": span, "relation": relation}]
    actual_asserted_by = (
        asserted_by if asserted_by is not None else {"id": str(actor_id), "kind": actor_kind}
    )
    origin = "owner_statement" if actual_asserted_by["kind"] == "owner" else "model_proposal"
    return {
        "run_id": "run-1",
        "expected_revision": expected_revision,
        "claim": {
            "id": claim_id,
            "category": "non_functional_qualities",
            "subject": subject if subject is not None else ENTITY,
            "predicate_id": predicate_id,
            "value": value if value is not None else {"type": "number", "value": 99.9},
            "origin": origin,
            "state": "proposed",
            "modality": "desired",
            "polarity": "positive",
            "truth": "unknown",
            "asserted_by": actual_asserted_by,
            "evidence": evidence,
            "premise_ids": [] if premise_ids is None else premise_ids,
        },
        "evidence_links": evidence_links,
    }


def claim_command(actor_id, actor_kind, **kwargs):
    return {"type": "record_intake_claim", "payload": claim_payload(actor_id, actor_kind, **kwargs)}


def claim_envelope(workspace_id, actor_id, actor_kind, **kwargs):
    return CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(),
        command=claim_command(actor_id, actor_kind, **kwargs),
    ))


def test_record_intake_claim_stale_invalid_forbidden_and_event(service, http_intake):
    workspace_id, mission_id = http_intake

    # no head and a mismatched current_revision are 409 stale_intake, rows unchanged
    status, body = _command(service, workspace_id, claim_command(OWNER, "owner", expected_revision=1))
    assert status == 409
    assert body["error"]["code"] == "stale_intake"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT count(*) FROM omp_work.intake_heads").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id=%s", (workspace_id,),
        ).fetchone() == (0,)

    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    assert _command(service, workspace_id,
                    evidence_command(OWNER, "owner", expected_revision=1))[0] == 200

    status, body = _command(service, workspace_id, claim_command(OWNER, "owner", expected_revision=1))
    assert status == 409
    assert body["error"]["code"] == "stale_intake"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM omp_work.intake_revisions").fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM omp_audit.domain_events WHERE workspace_id=%s", (workspace_id,),
        ).fetchone() == (2,)

    # missing evidence, premise, or subject entity is 400, head unchanged
    status, body = _command(
        service, workspace_id,
        claim_command(OWNER, "owner", expected_revision=2, evidence=[], evidence_links=[]),
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)

    status, body = _command(
        service, workspace_id,
        claim_command(OWNER, "owner", expected_revision=2, premise_ids=["missing-claim"]),
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)

    status, body = _command(
        service, workspace_id,
        claim_command(OWNER, "owner", expected_revision=2, subject={"id": "missing-entity", "type": "requirement"}),
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)

    # wrong new asserted_by is 403
    status, body = _command(
        service, workspace_id,
        claim_command(OWNER, "owner", expected_revision=2, asserted_by={"id": str(uuid4()), "kind": "owner"}),
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (1,)

    # result equals the four keys; the event adds evidence_status only. Do not assert max(ordinal)==38.
    status, body = _command(service, workspace_id, claim_command(OWNER, "owner", expected_revision=2))
    assert status == 200
    expected_result = {
        "type": "record_intake_claim",
        "run_id": "run-1",
        "expected_revision": 2,
        "revision": 3,
    }
    assert body["result"] == expected_result
    assert set(body["result"]) == {"type", "run_id", "expected_revision", "revision"}
    assert "evidence_status" not in body["result"]
    assert "mission_id" not in body["result"]

    with connection(service, workspace_id) as conn:
        assert conn.execute("SELECT current_revision FROM omp_work.intake_heads").fetchone() == (2,)
        events = conn.execute(
            "SELECT event_type,payload FROM omp_audit.domain_events WHERE workspace_id=%s ORDER BY sequence",
            (workspace_id,),
        ).fetchall()
        assert [event_type for event_type, _payload in events] == [
            "start_intake", "register_intake_evidence", "record_intake_claim",
        ]
        claim_event = events[2][1]
        assert claim_event == {**expected_result, "evidence_status": "supported"}
        assert set(claim_event) == {"type", "run_id", "expected_revision", "revision", "evidence_status"}
        assert "mission_id" not in claim_event

        view = read_intake_view(conn.cursor(), workspace_id, "run-1")
        assert view.revision == 3
        assert len(view.snapshot.claims) == 1
        assert view.snapshot.claims[0].id == "claim-1"
        assert len(view.evidence_links) == 1
        assert view.evidence_links[0].relation == "supports"


def test_execute_intake_record_claim_returns_four_key_dict_subclass(service, write_repository):
    workspace_id, mission_id = write_repository
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        execute_intake(cur, envelope(workspace_id, mission_id), OWNER, "owner")
        execute_intake(cur, evidence_envelope(workspace_id, OWNER, "owner", expected_revision=1), OWNER, "owner")
        result, event = execute_intake(
            cur, claim_envelope(workspace_id, OWNER, "owner", expected_revision=2), OWNER, "owner",
        )
        expected_four_keys = {
            "type": "record_intake_claim",
            "run_id": "run-1",
            "expected_revision": 2,
            "revision": 3,
        }
        assert result == expected_four_keys
        assert list(result.keys()) == ["type", "run_id", "expected_revision", "revision", "evidence_status"]
        assert "evidence_status" not in result
        assert result["evidence_status"] == "supported"
        assert list(result) == ["type", "run_id", "expected_revision", "revision", "evidence_status"]
        assert event == {**expected_four_keys, "evidence_status": "supported"}
        assert set(event.keys()) == {"type", "run_id", "expected_revision", "revision", "evidence_status"}
        assert "mission_id" not in result
        assert "mission_id" not in event
        assert json.loads(json.dumps(result)) == expected_four_keys
        assert RecordIntakeClaimResult.model_validate(result).model_dump(mode="json") == expected_four_keys
        assert sha256(result) == sha256(expected_four_keys)


def test_http_record_intake_claim_ontology_and_entity_validation(service, http_intake):
    workspace_id, mission_id = http_intake
    start_cmd = envelope(workspace_id, mission_id).command.model_dump(mode="json")
    entity_subject_mismatch = {"id": "entity-svc", "type": "service"}
    entity_target_mismatch = {"id": "entity-other", "type": "other"}
    start_cmd["payload"]["entities"] = [ENTITY, entity_subject_mismatch, entity_target_mismatch]
    start_cmd["payload"]["ontology"] = dict(predicates=[PREDICATE], dimensions=[])

    assert _command(service, workspace_id, start_cmd)[0] == 200
    assert _command(service, workspace_id, evidence_command(OWNER, "owner", expected_revision=1))[0] == 200

    with connection(service, workspace_id) as conn:
        baseline_heads = conn.execute(
            "SELECT workspace_id, run_id, current_revision, mission_id FROM omp_work.intake_heads WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        baseline_revisions = conn.execute(
            "SELECT workspace_id, run_id, revision FROM omp_work.intake_revisions WHERE workspace_id=%s ORDER BY revision",
            (workspace_id,),
        ).fetchall()

    cases = [
        # unknown predicate
        claim_command(
            OWNER,
            "owner",
            claim_id="claim-unknown-predicate",
            expected_revision=2,
            predicate_id="unknown_predicate",
            subject=ENTITY,
        ),
        # stored subject type outside subject_types with ref matching storage
        claim_command(
            OWNER,
            "owner",
            claim_id="claim-subject-type-outside",
            expected_revision=2,
            predicate_id="depends_on",
            subject=entity_subject_mismatch,
        ),
        # entity value missing target ID
        claim_command(
            OWNER,
            "owner",
            claim_id="claim-target-missing",
            expected_revision=2,
            predicate_id="depends_on",
            subject=ENTITY,
            value={"type": "entity", "value": {"id": "missing-entity", "type": "service"}},
        ),
        # stored target type outside object_types with ref matching storage
        claim_command(
            OWNER,
            "owner",
            claim_id="claim-target-type-outside",
            expected_revision=2,
            predicate_id="depends_on",
            subject=ENTITY,
            value={"type": "entity", "value": entity_target_mismatch},
        ),
    ]

    for command in cases:
        status, body = _command(service, workspace_id, command)
        assert status == 400
        assert body["error"]["code"] == "invalid_request"
        with connection(service, workspace_id) as conn:
            assert conn.execute(
                "SELECT workspace_id, run_id, current_revision, mission_id FROM omp_work.intake_heads WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchall() == baseline_heads
            assert conn.execute(
                "SELECT workspace_id, run_id, revision FROM omp_work.intake_revisions WHERE workspace_id=%s ORDER BY revision",
                (workspace_id,),
            ).fetchall() == baseline_revisions


@pytest.mark.parametrize("failure", [
    "unknown-artifact", "missing-link", "extra-link", "artifact-id-mismatch",
    "start-byte-mismatch", "end-byte-mismatch", "quote-mismatch",
    "duplicate-claim-span", "duplicate-link-span",
])
def test_http_claim_rejects_non_bijective_evidence_without_changing_rows(service, http_intake, failure):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    assert _command(service, workspace_id,
                    evidence_command(OWNER, "owner", text="alpha beta"))[0] == 200
    expected_revision = 2
    if failure == "artifact-id-mismatch":
        # Both IDs exist, so only span identity differs.
        assert _command(service, workspace_id, evidence_command(
            OWNER, "owner", text="alpha beta", artifact_id="artifact-other",
            uri="https://example.test/other", expected_revision=2,
        ))[0] == 200
        expected_revision = 3

    first = dict(artifact_id="artifact-added", start_byte=0, end_byte=5, quote="alpha")
    second = dict(artifact_id="artifact-added", start_byte=6, end_byte=10, quote="beta")
    evidence = [first]
    links = [dict(span=first, relation="supports")]
    if failure == "unknown-artifact":
        unknown = {**first, "artifact_id": "artifact-unknown"}
        evidence = [unknown]
        links = [dict(span=unknown, relation="supports")]
    elif failure == "missing-link":
        evidence = [first, second]
    elif failure == "extra-link":
        links.append(dict(span=second, relation="supports"))
    elif failure.endswith("-mismatch"):
        field, value = {
            "artifact-id-mismatch": ("artifact_id", "artifact-other"),
            "start-byte-mismatch": ("start_byte", 1),
            "end-byte-mismatch": ("end_byte", 4),
            "quote-mismatch": ("quote", "ALPHA"),
        }[failure]
        links = [dict(span={**first, field: value}, relation="supports")]
    elif failure == "duplicate-claim-span":
        evidence = [first, dict(first)]
    elif failure == "duplicate-link-span":
        # Relation does not distinguish two links to the same span.
        links.append(dict(span=dict(first), relation="contradicts"))

    with connection(service, workspace_id) as conn:
        baseline_heads = conn.execute(
            "SELECT * FROM omp_work.intake_heads WHERE workspace_id=%s", (workspace_id,),
        ).fetchall()
        baseline_revisions = conn.execute(
            "SELECT * FROM omp_work.intake_revisions WHERE workspace_id=%s ORDER BY revision", (workspace_id,),
        ).fetchall()

    command = claim_command(
        OWNER, "owner", claim_id=f"claim-evidence-{failure}", expected_revision=expected_revision,
        evidence=evidence, evidence_links=links,
    )
    # Validate shape separately: failures must come from evidence matching, not schemas.
    CommandEnvelope.model_validate(dict(
        api_version="work.omp.dev/v1", workspace_id=workspace_id,
        operation_id=uuid4(), request_id=uuid4(), correlation_id=uuid4(), command=command,
    ))
    status, body = _command(service, workspace_id, command)
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    with connection(service, workspace_id) as conn:
        assert conn.execute(
            "SELECT * FROM omp_work.intake_heads WHERE workspace_id=%s", (workspace_id,),
        ).fetchall() == baseline_heads
        assert conn.execute(
            "SELECT * FROM omp_work.intake_revisions WHERE workspace_id=%s ORDER BY revision", (workspace_id,),
        ).fetchall() == baseline_revisions


def test_http_claim_accepts_reversed_distinct_evidence_links_and_persists_quotes(service, http_intake):
    workspace_id, mission_id = http_intake
    assert _command(service, workspace_id,
                    envelope(workspace_id, mission_id).command.model_dump(mode="json"))[0] == 200
    assert _command(service, workspace_id,
                    evidence_command(OWNER, "owner", text="alpha beta"))[0] == 200
    first = dict(artifact_id="artifact-added", start_byte=0, end_byte=5, quote="alpha")
    second = dict(artifact_id="artifact-added", start_byte=6, end_byte=10, quote="beta")
    links = [dict(span=second, relation="contradicts"), dict(span=first, relation="supports")]
    claim_id = "claim-reversed-evidence"
    status, body = _command(service, workspace_id, claim_command(
        OWNER, "owner", claim_id=claim_id, evidence=[first, second], evidence_links=links,
    ))
    assert status == 200
    assert body["result"]["revision"] == 3
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s", (workspace_id,),
        ).fetchone() == (2,)
        stored = read_revision(cur, workspace_id=workspace_id, run_id="run-1", revision=2)
        assert stored.snapshot.claims[0].id == claim_id
        assert [span.model_dump() for span in stored.snapshot.claims[0].evidence] == [first, second]
        assert {
            (link.claim_id, link.artifact_id, link.start_byte, link.end_byte, link.quote, link.relation)
            for link in stored.body.evidence_links
        } == {
            (claim_id, "artifact-added", 0, 5, "alpha", "supports"),
            (claim_id, "artifact-added", 6, 10, "beta", "contradicts"),
        }
        assert len(stored.body.evidence_links) == 2
