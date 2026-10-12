"""NSI-2 lost-response recovery and bounded mission intake coexistence."""
from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    IntakeUnknown,
)
from omp_work.v1.server import create_app
from omp_work.v1.store import PostgresWorkStore
from test_mission_intake_store import _intake, _scope
from test_project_store import _workspace
from test_workflow_service import (
    OWNER, _command, _grant, _owner_headers, _seed_project, service,  # noqa: F401
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


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


@pytest.fixture
def mission(service):
    workspace_id, project_id, mission_id = uuid4(), uuid4(), uuid4()
    _grant(service, workspace_id)
    _workspace(service, workspace_id)
    _seed_project(service, workspace_id, project_id)
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        conn.execute(
            "INSERT INTO omp_work.project_missions (workspace_id, mission_id, project_id, objective, status)"
            " VALUES (%s, %s, %s, 'Intake', 'draft')",
            (workspace_id, mission_id, project_id),
        )
    return workspace_id, project_id, mission_id


def start_intake(service, workspace_id, mission_id):
    return _command(service, workspace_id, {
        "type": "start_intake",
        "payload": {
            "run_id": "run-1", "expected_revision": 0,
            "mission_id": str(mission_id),
            "ontology": {"predicates": [], "dimensions": []}, "entities": [],
        },
    })


def test_ac2_nsi2_lost_response_reconciliation_and_replay(service, mission):
    workspace_id, _, mission_id = mission
    status, body = start_intake(service, workspace_id, mission_id)
    assert status == 200, body
    operation_id = uuid4()
    source = "Owner requires bounded memory growth."
    envelope = {
        "api_version": "work.omp.dev/v1", "workspace_id": str(workspace_id),
        "operation_id": str(operation_id), "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {
            "type": "register_intake_evidence",
            "payload": {
                "run_id": "run-1", "expected_revision": 1,
                "artifact": {
                    "id": "source", "uri": "https://example.test/requirements",
                    "content_hash": text_sha256(source), "revision": "1",
                    "actor": {"id": str(OWNER), "kind": "owner"},
                    "media_type": "text/plain",
                },
                "source": {"text": source, "sha256": text_sha256(source)},
            },
        },
    }
    headers = _owner_headers(workspace_id)
    # CommandResponse validation runs only after service.execute commits.
    with patch("omp_work.v1.server.CommandResponse.model_validate",
               side_effect=RuntimeError("lost response after commit")) as validate:
        lost = service.client.post("/v1/commands", headers=headers, json=envelope)
    assert validate.call_count == 1
    assert lost.status_code == 503
    error = lost.json()["error"]
    assert error["code"] == "unavailable"
    assert any("outcome unknown, reconcile by operation_id" in diagnostic
               for diagnostic in error["diagnostics"])

    reconciled = service.client.get(f"/v1/operations/{operation_id}", headers=headers)
    assert reconciled.status_code == 200, reconciled.json()
    stored = reconciled.json()
    assert stored["receipt"]["state"] == "applied"
    assert stored["result"] == {
        "type": "register_intake_evidence", "run_id": "run-1",
        "expected_revision": 1, "revision": 2,
    }
    assert stored["receipt"]["result_sha256"] == sha256(stored["result"])

    replay = service.client.post("/v1/commands", headers=headers, json=envelope)
    assert replay.status_code == 200, replay.json()
    replayed = replay.json()
    assert replayed["receipt"]["state"] == "replayed"
    assert replayed["result"] == stored["result"]
    assert replayed["receipt"]["result_sha256"] == stored["receipt"]["result_sha256"]

    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        assert conn.execute(
            "SELECT count(*) FROM omp_work.intake_revisions WHERE workspace_id=%s AND run_id='run-1'",
            (workspace_id,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, operation_id),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT current_revision FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id='run-1'",
            (workspace_id,),
        ).fetchone() == (1,)


def test_ac3_bounded_mission_intake_coexistence(service, mission):
    workspace_id, project_id, mission_id = mission
    _, token = intake_capability(
        service, workspace_id, kind="client", scopes=("work.client", "work.mutate", "work.read"),
    )
    response = service.client.post(
        f"/v1/workspaces/{workspace_id}/client/mission-intake",
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
        json={
            "request_id": str(uuid4()),
            "payload": {
                "mission_id": str(mission_id), "base_revision": None,
                "intake": _intake(), "scope": _scope(project_id),
            },
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["contract"] == "client.omp.dev/v1"
    assert body["outcome"] == "applied"
    assert body["operation"] == "mission.intake"
    assert body["result"]["type"] == "draft_mission_intake"
    status, body = start_intake(service, workspace_id, mission_id)
    assert status == 200, body


def test_ac1_nsi2_lifecycle_provenance_and_persistence(service, mission):
    workspace_id, project_id, mission_id = mission
    headers = _owner_headers(workspace_id)

    # 1. Non-ASCII draft and bounded_to_nsi
    source_text = "Pin the stats café export to JSON. Keep the résumé table. An automated test must pass."
    raw_source = source_text.encode("utf-8")
    quote1 = "Pin the stats café export to JSON."
    start1 = 0
    end1 = len(quote1.encode("utf-8"))
    quote2 = "Keep the résumé table."
    start2 = raw_source.index(quote2.encode("utf-8"))
    end2 = start2 + len(quote2.encode("utf-8"))

    # Assumption: span bytes decode to the quote
    assert raw_source[start1:end1].decode("utf-8") == quote1
    assert raw_source[start2:end2].decode("utf-8") == quote2

    span1_hash = hashlib.sha256(raw_source[start1:end1]).hexdigest()
    span2_hash = hashlib.sha256(raw_source[start2:end2]).hexdigest()
    source_hash = hashlib.sha256(raw_source).hexdigest()

    span1 = IntakeSourceSpan(id="span-1", start=start1, end=end1, exact_text_sha256=span1_hash)
    span2 = IntakeSourceSpan(id="span-2", start=start2, end=end2, exact_text_sha256=span2_hash)
    goal = IntakeGoal(id="goal-1", statement="Pin the stats export.", source_span_ids=("span-1",))
    ac1 = IntakeAcceptanceCriterion(
        id="ac-1",
        statement="Pass test.",
        observable_outcome="Passed.",
        oracle="automated_test",
        source_span_ids=("span-2",),
    )
    ac2 = IntakeAcceptanceCriterion(
        id="ac-2",
        statement="Keep latency low.",
        observable_outcome="Latency is low.",
        oracle="automated_test",
        source_span_ids=("span-1",),
    )
    u_old = IntakeUnknown(
        id="claim-old",
        statement="Old claim to supersede",
        kind="routine_choice",
        material=False,
        source_span_ids=("span-1",),
    )
    u_model = IntakeUnknown(
        id="claim-model",
        statement="Model proposed claim",
        kind="routine_choice",
        material=False,
        source_span_ids=("span-1",),
    )

    draft = BoundedIntakeDraft(
        archetype="small_code_change",
        source=IntakeSource(text=source_text, sha256=source_hash, spans=[span1, span2]),
        goal=goal,
        acceptance_criteria=[ac1, ac2],
        constraints=[],
        unknowns=[u_old, u_model],
    )
    draft_snapshot = bounded_to_nsi(draft)

    # Command 1: start_intake (@1, no claims)
    status, res = _command(service, workspace_id, {
        "type": "start_intake",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 0,
            "mission_id": str(mission_id),
            "ontology": draft_snapshot.ontology.model_dump(mode="json"),
            "entities": [e.model_dump(mode="json") for e in draft_snapshot.entities],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 1

    r1 = service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=1", headers=headers)
    assert r1.status_code == 200, r1.json()
    get_1_initial = r1.json()
    assert get_1_initial["revision"] == 1
    assert len(get_1_initial["snapshot"]["claims"]) == 0

    # Command 2: register_intake_evidence (artifact id bounded-source)
    evidence_artifact = {
        "id": "bounded-source",
        "uri": "intake:run-1/source/bounded-source",
        "content_hash": source_hash,
        "revision": "1",
        "actor": {"id": str(OWNER), "kind": "owner"},
        "media_type": "text/plain",
    }
    evidence_source = {
        "text": source_text,
        "sha256": source_hash,
    }
    # Assumption: UTF-8 sha256 equals content_hash and source.sha256
    assert source_hash == evidence_artifact["content_hash"] == evidence_source["sha256"]

    status, res = _command(service, workspace_id, {
        "type": "register_intake_evidence",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 1,
            "artifact": evidence_artifact,
            "source": evidence_source,
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 2

    # Command 3: owner propose (@3, that claim proposed by second owner)
    proposer_id, proposer_token = intake_capability(service, workspace_id, kind="owner")
    claim_admitted_span = {
        "artifact_id": "bounded-source",
        "start_byte": start1,
        "end_byte": end1,
        "quote": quote1,
    }
    claim_admitted_payload = {
        "id": "ac-2",
        "category": "completion_signals",
        "subject": {"id": "ac-2", "type": "acceptance_criterion"},
        "predicate_id": "bounded.acceptance_criterion",
        "value": {
            "type": "string",
            "value": json.dumps({
                "observable_outcome": "Latency is low.",
                "oracle": "automated_test",
                "statement": "Keep latency low.",
            }),
        },
        "origin": "owner_statement",
        "state": "proposed",
        "modality": "required",
        "polarity": "positive",
        "truth": "unknown",
        "asserted_by": {"id": str(proposer_id), "kind": "owner"},
        "evidence": [claim_admitted_span],
        "premise_ids": [],
    }
    proposal_op_id = uuid4()
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 2,
            "claim": claim_admitted_payload,
            "evidence_links": [{"span": claim_admitted_span, "relation": "supports"}],
        },
    }, token=proposer_token, operation_id=proposal_op_id)
    assert status == 200, res
    assert res["result"]["revision"] == 3

    r3 = service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=3", headers=headers)
    assert r3.status_code == 200, r3.json()
    get_3_initial = r3.json()
    assert get_3_initial["revision"] == 3
    claims_at_3 = {c["id"]: c for c in get_3_initial["snapshot"]["claims"]}
    assert "ac-2" in claims_at_3
    assert claims_at_3["ac-2"]["state"] == "proposed"
    assert claims_at_3["ac-2"]["asserted_by"]["id"] == str(proposer_id)

    # Command 4: owner admit (@4, admitted by fixture OWNER)
    claim_admitted_payload_admitted = dict(
        claim_admitted_payload,
        state="admitted",
        truth="true",
        admitted_by={"id": str(OWNER), "kind": "owner"},
    )
    admission_op_id = uuid4()
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 3,
            "claim": claim_admitted_payload_admitted,
            "evidence_links": [{"span": claim_admitted_span, "relation": "supports"}],
        },
    }, operation_id=admission_op_id)
    assert status == 200, res
    assert res["result"]["revision"] == 4

    r4 = service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=4", headers=headers)
    assert r4.status_code == 200, r4.json()
    get_4_initial = r4.json()
    assert get_4_initial["revision"] == 4
    claims_at_4 = {c["id"]: c for c in get_4_initial["snapshot"]["claims"]}
    assert "ac-2" in claims_at_4
    assert claims_at_4["ac-2"]["state"] == "admitted"
    assert claims_at_4["ac-2"]["asserted_by"]["id"] == str(proposer_id)
    assert claims_at_4["ac-2"]["admitted_by"]["id"] == str(OWNER)

    # Command 5: replacement claim (the old claim to be superseded)
    claim_old_span = {
        "artifact_id": "bounded-source",
        "start_byte": start1,
        "end_byte": end1,
        "quote": quote1,
    }
    claim_old_payload = {
        "id": "claim-old",
        "category": "placeholders_and_ambiguities",
        "subject": {"id": "claim-old", "type": "unknown"},
        "predicate_id": "bounded.unknown",
        "value": {"type": "string", "value": "Old claim to supersede"},
        "origin": "owner_statement",
        "state": "admitted",
        "modality": "possible",
        "polarity": "positive",
        "truth": "true",
        "asserted_by": {"id": str(OWNER), "kind": "owner"},
        "admitted_by": {"id": str(OWNER), "kind": "owner"},
        "evidence": [claim_old_span],
        "premise_ids": [],
    }
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 4,
            "claim": claim_old_payload,
            "evidence_links": [{"span": claim_old_span, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 5

    # Command 6: issue
    status, res = _command(service, workspace_id, {
        "type": "record_intake_issue",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 5,
            "issue": {
                "id": "question-1",
                "question": "Which claim should be used?",
                "gap_ids": [],
                "conflict_ids": [],
                "category": "constraints_and_tradeoffs",
            },
            "gaps": [],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 6

    # Command 7: first decision (@7, exactly one decision)
    status, res = _command(service, workspace_id, {
        "type": "record_intake_decision",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 6,
            "decision": {
                "id": "decision-1",
                "run_id": "run-1",
                "revision": 6,
                "actor": {"id": str(OWNER), "kind": "owner"},
                "question_id": "question-1",
                "answer": {"type": "boolean", "value": True},
                "reason": "Settled by owner.",
                "created_at": "2026-10-12T00:00:00Z",
            },
            "claim_ids": ["claim-old", "ac-2"],
            "instruction": {
                "text": "Keep admitted claim.",
                "provenance": {
                    "channel": "owner-chat",
                    "message_ref": "msg:exact1",
                    "received_at": "2026-10-12T00:00:00Z",
                },
            },
            "superseded_decision_id": None,
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 7

    r7 = service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=7", headers=headers)
    assert r7.status_code == 200, r7.json()
    get_7_initial = r7.json()
    assert get_7_initial["revision"] == 7
    assert len(get_7_initial["decisions"]) == 1
    assert get_7_initial["decisions"][0]["decision"]["id"] == "decision-1"
    # Assumption: decision.revision equals that command's expected_revision
    assert get_7_initial["decisions"][0]["decision"]["revision"] == 6

    # Command 8: supersede_intake_claim
    status, res = _command(service, workspace_id, {
        "type": "supersede_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 7,
            "old_claim_id": "claim-old",
            "replacement_claim_id": "ac-2",
            "decision_id": "decision-1",
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 8

    # Command 9: superseding decision
    status, res = _command(service, workspace_id, {
        "type": "record_intake_decision",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 8,
            "decision": {
                "id": "decision-2",
                "run_id": "run-1",
                "revision": 8,
                "actor": {"id": str(OWNER), "kind": "owner"},
                "question_id": "question-1",
                "answer": {"type": "boolean", "value": True},
                "reason": "Superseding decision by owner.",
                "created_at": "2026-10-12T00:00:00Z",
            },
            "claim_ids": ["claim-old", "ac-2"],
            "instruction": {
                "text": "Confirmed superseding instruction.",
                "provenance": {
                    "channel": "owner-chat",
                    "message_ref": "msg:exact2",
                    "received_at": "2026-10-12T00:00:00Z",
                },
            },
            "superseded_decision_id": "decision-1",
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 9

    # Command 10: link_intake_criterion (for ac-2)
    status, res = _command(service, workspace_id, {
        "type": "link_intake_criterion",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 9,
            "criterion": {
                "id": "ac-2",
                "requirement_ids": ["req-ac-2"],
                "description": "Keep latency low.",
                "verification_method": "automated_test",
                "expected_result": {"type": "string", "value": "Latency is low."},
            },
            "requirements": [
                {
                    "id": "req-ac-2",
                    "category": "completion_signals",
                    "text": "Keep latency low.",
                    "claim_ids": ["ac-2"],
                    "dimension_ids": ["missing_verification_oracle"],
                }
            ],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 10

    # Command 11: admitted bounded claims in sequence
    # 11.1 bounded.source
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 10,
            "claim": {
                "id": "bounded.source",
                "category": "functional_scope_and_behavior",
                "subject": {"id": "bounded.source", "type": "source"},
                "predicate_id": "bounded.source_text",
                "value": {"type": "string", "value": source_text},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "observed",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": 0, "end_byte": len(raw_source), "quote": source_text}],
                "premise_ids": [],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": 0, "end_byte": len(raw_source), "quote": source_text}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 11

    # 11.2 bounded.span.span-1
    span1_payload = {
        "id": "span-1", "start": start1, "end": end1, "exact_text_sha256": span1_hash,
    }
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 11,
            "claim": {
                "id": "bounded.span.span-1",
                "category": "scope_boundaries",
                "subject": {"id": "bounded.span.span-1", "type": "source_span"},
                "predicate_id": "bounded.source_span",
                "value": {"type": "string", "value": json.dumps(span1_payload)},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "observed",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": start1, "end_byte": end1, "quote": quote1}],
                "premise_ids": [],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": start1, "end_byte": end1, "quote": quote1}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 12

    # 11.3 bounded.span.span-2
    span2_payload = {
        "id": "span-2", "start": start2, "end": end2, "exact_text_sha256": span2_hash,
    }
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 12,
            "claim": {
                "id": "bounded.span.span-2",
                "category": "scope_boundaries",
                "subject": {"id": "bounded.span.span-2", "type": "source_span"},
                "predicate_id": "bounded.source_span",
                "value": {"type": "string", "value": json.dumps(span2_payload)},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "observed",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": start2, "end_byte": end2, "quote": quote2}],
                "premise_ids": [],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": start2, "end_byte": end2, "quote": quote2}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 13

    # 11.4 goal-1
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 13,
            "claim": {
                "id": "goal-1",
                "category": "functional_scope_and_behavior",
                "subject": {"id": "goal-1", "type": "goal"},
                "predicate_id": "bounded.goal",
                "value": {"type": "string", "value": "Pin the stats export."},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "required",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": start1, "end_byte": end1, "quote": quote1}],
                "premise_ids": ["bounded.span.span-1"],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": start1, "end_byte": end1, "quote": quote1}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 14

    # 11.5 ac-1
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 14,
            "claim": {
                "id": "ac-1",
                "category": "completion_signals",
                "subject": {"id": "ac-1", "type": "acceptance_criterion"},
                "predicate_id": "bounded.acceptance_criterion",
                "value": {"type": "string", "value": json.dumps({"observable_outcome": "Passed.", "oracle": "automated_test", "statement": "Pass test."})},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "required",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": start2, "end_byte": end2, "quote": quote2}],
                "premise_ids": ["bounded.span.span-2"],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": start2, "end_byte": end2, "quote": quote2}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 15

    # 11.6 bounded.archetype
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 15,
            "claim": {
                "id": "bounded.archetype",
                "category": "functional_scope_and_behavior",
                "subject": {"id": "bounded.archetype", "type": "intake"},
                "predicate_id": "bounded.archetype",
                "value": {"type": "string", "value": "small_code_change"},
                "origin": "owner_statement",
                "state": "admitted",
                "modality": "observed",
                "polarity": "positive",
                "truth": "true",
                "asserted_by": {"id": str(OWNER), "kind": "owner"},
                "admitted_by": {"id": str(OWNER), "kind": "owner"},
                "evidence": [{"artifact_id": "bounded-source", "start_byte": 0, "end_byte": len(raw_source), "quote": source_text}],
                "premise_ids": [],
            },
            "evidence_links": [{"span": {"artifact_id": "bounded-source", "start_byte": 0, "end_byte": len(raw_source), "quote": source_text}, "relation": "supports"}],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 16

    # Command 12: model proposal
    model_id, model_token = intake_capability(service, workspace_id, kind="model")
    claim_model_span = {
        "artifact_id": "bounded-source",
        "start_byte": start1,
        "end_byte": end1,
        "quote": quote1,
    }
    model_op_id = uuid4()
    status, res = _command(service, workspace_id, {
        "type": "record_intake_claim",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 16,
            "claim": {
                "id": "claim-model",
                "category": "placeholders_and_ambiguities",
                "subject": {"id": "claim-model", "type": "unknown"},
                "predicate_id": "bounded.unknown",
                "value": {"type": "string", "value": "Model proposed claim"},
                "origin": "model_proposal",
                "state": "proposed",
                "modality": "possible",
                "polarity": "positive",
                "truth": "unknown",
                "asserted_by": {"id": str(model_id), "kind": "model"},
                "evidence": [claim_model_span],
                "premise_ids": [],
            },
            "evidence_links": [{"span": claim_model_span, "relation": "supports"}],
        },
    }, token=model_token, operation_id=model_op_id)
    assert status == 200, res
    assert res["result"]["revision"] == 17

    # Command 13: acceptance link (for ac-1)
    status, res = _command(service, workspace_id, {
        "type": "link_intake_criterion",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 17,
            "criterion": {
                "id": "ac-1",
                "requirement_ids": ["req-ac-1"],
                "description": "Pass test.",
                "verification_method": "automated_test",
                "expected_result": {"type": "string", "value": "Passed."},
            },
            "requirements": [
                {
                    "id": "req-ac-1",
                    "category": "completion_signals",
                    "text": "Pass test.",
                    "claim_ids": ["ac-1"],
                    "dimension_ids": ["missing_verification_oracle"],
                }
            ],
        },
    })
    assert status == 200, res
    assert res["result"]["revision"] == 18

    # Command 14: assess_intake
    status, res = _command(service, workspace_id, {
        "type": "assess_intake",
        "payload": {
            "run_id": "run-1",
            "expected_revision": 18,
        },
    })
    assert status == 200, res

    # Retrieve latest view
    r_latest = service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1", headers=headers)
    assert r_latest.status_code == 200, r_latest.json()
    latest_view = r_latest.json()
    assert latest_view["assessments"][-1]["assessment"]["status"] == "READY_FOR_RATIFICATION"
    prov = latest_view["provenance"]

    # Assumptions verifications:
    # 1. Admitted bounded.source_text equals the full source text
    source_claim = next(c for c in latest_view["snapshot"]["claims"] if c["id"] == "bounded.source")
    assert source_claim["value"]["value"] == source_text

    # 2. Provenance source text equals submitted text
    assert any(s["source"]["text"] == source_text for s in prov["sources"])

    # 3. Model call assertions:
    # Snapshot claim uses model id and kind
    model_snapshot_claim = next(c for c in latest_view["snapshot"]["claims"] if c["id"] == "claim-model")
    assert model_snapshot_claim["asserted_by"] == {"id": str(model_id), "kind": "model"}
    assert model_snapshot_claim["state"] == "proposed"
    # Event uses model id and kind
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        model_event = conn.execute(
            "SELECT actor_id, actor_kind FROM omp_audit.domain_events WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, model_op_id),
        ).fetchone()
        assert model_event == (model_id, "model")

        # Proposer and admission event actors
        prop_event = conn.execute(
            "SELECT actor_id, actor_kind FROM omp_audit.domain_events WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, proposal_op_id),
        ).fetchone()
        assert prop_event == (proposer_id, "owner")

        adm_event = conn.execute(
            "SELECT actor_id, actor_kind FROM omp_audit.domain_events WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, admission_op_id),
        ).fetchone()
        assert adm_event == (OWNER, "owner")

    # Claim stays proposed and is absent from provenance.claims
    assert all(c["claim"]["id"] != "claim-model" for c in prov["claims"])

    # 4. Proposer and admission assertions:
    # Provenance asserted_by is the proposer and admitted_by is OWNER
    admitted_prov = next(c for c in prov["claims"] if c["claim"]["id"] == "ac-2")
    assert admitted_prov["claim"]["asserted_by"]["id"] == str(proposer_id)
    assert admitted_prov["claim"]["admitted_by"]["id"] == str(OWNER)
    # Admission admitted_by is OWNER
    admission_prov = next(a for a in prov["admissions"] if a["claim_id"] == "ac-2")
    assert admission_prov["admitted_by"]["id"] == str(OWNER)

    # 5. Provenance lists both instruction texts and settled claim ids
    inst_texts = [d["record"]["instruction"]["text"] for d in prov["decisions"]]
    assert "Keep admitted claim." in inst_texts
    assert "Confirmed superseding instruction." in inst_texts
    settled_ids = [cid for d in prov["decisions"] for cid in d["record"]["claim_ids"]]
    assert "claim-old" in settled_ids
    assert "ac-2" in settled_ids

    # 6. Supersession names the old claim, replacement, and decision id
    assert len(prov["supersessions"]) >= 1
    supersession = next(s for s in prov["supersessions"] if s["old_claim_id"] == "claim-old")
    assert supersession["replacement_claim_id"] == "ac-2"
    assert supersession["decision_id"] == "decision-1"

    # 7. No admitted premise points at a superseded id
    active_claims = [c["claim"] for c in prov["claims"]]
    for claim in active_claims:
        for p in claim.get("premise_ids", []):
            assert p != "claim-old"

    # 8. Traces equal the link and evidence spans are the submitted span
    trace_ac2 = next(t for t in prov["traces"] if t["criterion_id"] == "ac-2")
    assert trace_ac2["requirement_ids"] == ["req-ac-2"]
    assert trace_ac2["claim_ids"] == ["ac-2"]

    trace_ac1 = next(t for t in prov["traces"] if t["criterion_id"] == "ac-1")
    assert trace_ac1["requirement_ids"] == ["req-ac-1"]
    assert trace_ac1["claim_ids"] == ["ac-1"]

    evidence_spans = [s["span"] for s in latest_view["evidence_links"] if s["span"]["artifact_id"] == "bounded-source"]
    assert any(s["start_byte"] == start1 and s["end_byte"] == end1 and s["quote"] == quote1 for s in evidence_spans)
    assert any(s["start_byte"] == start2 and s["end_byte"] == end2 and s["quote"] == quote2 for s in evidence_spans)

    # 9. Each bounded.acceptance_criterion id present at assess time has a history link with that same id
    history_criterion_ids = [l["criterion"]["id"] for l in latest_view["criterion_links"]]
    assert "ac-1" in history_criterion_ids
    assert "ac-2" in history_criterion_ids

    # 10. Historical GETs stay on those snapshots after later commands
    assert service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=1", headers=headers).json() == get_1_initial
    assert service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=3", headers=headers).json() == get_3_initial
    assert service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=4", headers=headers).json() == get_4_initial
    assert service.client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=7", headers=headers).json() == get_7_initial

    # 11. Persistence: A new PostgresWorkStore and TestClient on the same database return same latest, @1, @4, @7 JSON, with status READY_FOR_RATIFICATION
    new_store = PostgresWorkStore(service.config)
    new_app = create_app(service.config, capabilities_dir=service.capabilities, store=new_store)
    new_client = TestClient(new_app)

    new_latest = new_client.get(f"/v1/workspaces/{workspace_id}/intake/run-1", headers=headers)
    assert new_latest.status_code == 200
    new_latest_json = new_latest.json()
    assert new_latest_json["assessments"][-1]["assessment"]["status"] == "READY_FOR_RATIFICATION"
    assert new_latest_json == latest_view

    assert new_client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=1", headers=headers).json() == get_1_initial
    assert new_client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=4", headers=headers).json() == get_4_initial
    assert new_client.get(f"/v1/workspaces/{workspace_id}/intake/run-1?revision=7", headers=headers).json() == get_7_initial

