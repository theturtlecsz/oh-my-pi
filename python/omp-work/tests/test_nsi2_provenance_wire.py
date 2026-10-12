"""NSI-2 API JSON retains assessment closure and its audit ancestry."""
from copy import deepcopy
from pathlib import Path
import json
from uuid import UUID

from omp_work import generate_api_schema
from omp_work.v1.canonical import text_sha256
from omp_work.v1.api_models import IntakeRecordView
from test_nsi_schemas import (
    ARTIFACT, ASSESSMENT, BINDING, CLAIM, CONFLICT, DECISION, ENTITY, GAP,
    QUESTION, RECEIPT, REQUIREMENT, CRITERION,
)


OPERATION = {
    "operation_id": "00000000-0000-0000-0000-000000000003",
    "event_id": "00000000-0000-0000-0000-000000000004",
}


def empty_view():
    return {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "mission_id": "00000000-0000-0000-0000-000000000002",
        "run_id": "run-1", "revision": 1,
        "snapshot": {
            "run_id": "run-1", "revision": 0, "state": "collecting",
            "artifacts": [], "entities": [], "claims": [],
            "ontology": {"predicates": [], "dimensions": []},
        },
        "evidence_links": [], "criterion_links": [], "decisions": [],
        "issues": [], "traces": [], "assessments": [],
        "provenance": {"run_id": "run-1", "revision": 1, "source_sha256": "a" * 64},
    }


def test_record_json_retains_assessment_binding_ancestry_and_audit_refs():
    payload = empty_view()
    claim = deepcopy(CLAIM)
    claim.update(premise_ids=["prior-claim"], value={"type": "null", "value": None})
    claim["evidence"] = [{"artifact_id": ARTIFACT["id"], "start_byte": 0, "end_byte": 5}]
    evidence_link = {"span": claim["evidence"][0], "relation": "supports"}
    instruction = {
        "text": "Use owner target",
        "provenance": {"channel": "chat", "message_ref": "message-1",
                       "received_at": "2026-10-10T00:00:00Z"},
    }
    decision = {
        "decision": DECISION, "claim_ids": [claim["id"]], "instruction": instruction,
        "superseded_decision_id": "prior-decision",
        "verified_relay_operation_id": OPERATION["operation_id"],
    }
    trace = {"criterion_id": CRITERION["id"], "requirement_ids": [REQUIREMENT["id"]],
             "claim_ids": [claim["id"], "prior-claim"]}
    provenance = payload["provenance"] | {
        "sources": [{"artifact": ARTIFACT, "source": {"text": "hello", "sha256": text_sha256("hello")},
                     "operation": OPERATION}],
        "claims": [{"claim": claim, "ancestry_ids": ["prior-claim", "original-claim"],
                    "evidence_links": {claim["id"]: [evidence_link], "prior-claim": []},
                    "artifacts": [ARTIFACT, ARTIFACT | {"id": "extra-artifact"}]}],
        "admissions": [{"claim_id": claim["id"], "admitted_by": {"id": "owner-1", "kind": "owner"},
                        "operation": OPERATION}],
        "decisions": [{"record": decision, "operation": OPERATION}],
        "supersessions": [{"old_claim_id": "prior-claim", "replacement_claim_id": claim["id"],
                           "decision_id": DECISION["id"], "operation": OPERATION}],
        "relays": [{"relay": {
            "instruction": {"text": "Use owner target", "source_message_ref": "message-1",
                            "owner_authored": True, "received_at": "2026-10-10T00:00:00Z"},
            "relayed_by": payload["workspace_id"], "signed": True,
        }, "operation": OPERATION}],
        "traces": [trace],
    }
    issues = [{"issue": CONFLICT, "gaps": [GAP["id"]]},
              {"issue": QUESTION, "gaps": [GAP["id"]]}]
    assessment = {
        "id": "assessment-1", "assessment": ASSESSMENT | {"status": "NEEDS_SPEC"},
        "binding": BINDING, "receipts": [RECEIPT], "unresolved_issues": issues,
        "provenance": provenance,
    }
    payload.update(provenance=provenance, assessments=[assessment], issues=issues, traces=[trace],
                   decisions=[decision], criterion_links=[{"criterion": CRITERION, "requirements": [REQUIREMENT]}])
    payload["snapshot"].update(artifacts=[ARTIFACT], entities=[ENTITY], claims=[claim])

    view = IntakeRecordView.model_validate_json(json.dumps(payload))
    wire = view.model_dump_json()
    parsed = IntakeRecordView.model_validate_json(wire)
    assert parsed == view
    assert parsed.revision == parsed.assessments[0].binding.revision + 1
    assert json.loads(wire)["assessments"][0]["binding"] == BINDING
    assert json.loads(wire)["assessments"][0]["assessment"] == assessment["assessment"]
    assert json.loads(wire)["assessments"][0]["receipts"] == [RECEIPT]
    assert parsed.assessments[0].unresolved_issues == parsed.issues
    # Exact declared closure survives; NSI omitted fields stay absent, typed null stays explicit.
    emitted = json.loads(wire)["provenance"]
    assert emitted["claims"] == provenance["claims"]
    assert emitted["sources"][0]["artifact"] == ARTIFACT
    assert parsed.provenance.sources[0].source.text == "hello"
    for name in ("admissions", "decisions", "supersessions", "relays"):
        assert getattr(parsed.provenance, name)[0].operation.event_id == UUID(OPERATION["event_id"])
        assert getattr(parsed.provenance, name)[0].operation.operation_id == UUID(OPERATION["operation_id"])
    assert parsed.provenance.decisions[0].record == parsed.decisions[0]
    assert parsed.provenance.supersessions[0].old_claim_id == "prior-claim"
    assert parsed.provenance.relays[0].relay.instruction.text == "Use owner target"
    assert parsed.provenance.admissions[0].admitted_by.id == "owner-1"
    assert parsed.provenance.traces == parsed.traces
    assert parsed.assessments[0].provenance == parsed.provenance


def test_existing_empty_record_json_still_parses_without_new_provenance_fields():
    view = IntakeRecordView.model_validate_json(json.dumps(empty_view()))
    parsed = IntakeRecordView.model_validate_json(view.model_dump_json())
    assert parsed == view
    assert parsed.assessments == ()
    for name in ("sources", "claims", "admissions", "decisions", "supersessions", "relays", "traces"):
        assert getattr(parsed.provenance, name) == ()


def test_committed_api_schema_exposes_generated_provenance_types():
    schema_path = Path(__file__).parents[1] / "src/omp_work/contracts/v1/api-schema.json"
    assert json.loads(schema_path.read_text()) == generate_api_schema()
