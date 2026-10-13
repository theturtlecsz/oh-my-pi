"""Persist typed intake fixtures through the production repository."""
import json
from uuid import uuid4

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, intake_contract
from omp_work.contracts.nsi.v1.models import Binding
from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import (
    DecisionHistory, EvidenceLinkRecord, IntakeRevisionBody, append_assessment,
    append_revision, lock_revision, read_revision, revision_hashes,
)
from omp_work.jobs.store import NativeJobStore
from omp_work.orchestrator.intake_gate import cursor, read_head
from omp_work.v1.api_models import IntakeAssessmentView, IntakeCriterionLink
from omp_work.v1.intake_provenance import evaluate_provenance


def seed_typed_intake(store, *, workspace_id, actor_id, mission_id, run_id, draft, mode="pass"):
    store = _ops(store)
    snapshot = bounded_to_nsi(draft).model_copy(update={"run_id": run_id})
    # Closure references source spans by their bounded IDs.
    snapshot.claims = [
        claim.model_copy(update={
            "id": json.loads(claim.value.value)["id"],
            "subject": claim.subject.model_copy(update={"id": json.loads(claim.value.value)["id"]}),
        }) if claim.predicate_id == "bounded.source_span" else claim
        for claim in snapshot.claims
    ]
    snapshot.entities = list({claim.subject.id: claim.subject for claim in snapshot.claims}.values())
    contract = intake_contract(snapshot)
    claim_hash, source_hash = revision_hashes(snapshot, {"bounded-source": draft.source.text.encode()})
    body = IntakeRevisionBody(
        workspace_id=workspace_id, snapshot=snapshot,
        binding=Binding(
            run_id=run_id, revision=0, contract_hash=content_hash({}),
            claim_set_hash=claim_hash, source_hash=source_hash,
            rule_bundle_hash=content_hash({}), schema_hash=content_hash({}),
        ),
        artifact_bytes={"bounded-source": draft.source.text.encode()},
        evidence_links=[EvidenceLinkRecord(
            claim_id=claim.id, artifact_id=span.artifact_id, start_byte=span.start_byte,
            end_byte=span.end_byte, quote=span.quote, relation="supports",
        ) for claim in snapshot.claims for span in claim.evidence],
        criteria=list(contract.acceptance_criteria),
        history=DecisionHistory(criteria=[IntakeCriterionLink(
            criterion=criterion, requirements=tuple(
                requirement for requirement in contract.requirements if requirement.id in criterion.requirement_ids
            ),
        ) for criterion in contract.acceptance_criteria]),
    )
    with cursor(store, workspace_id=workspace_id, actor_id=actor_id) as cur:
        cur.execute(
            "INSERT INTO omp_work.project_missions(workspace_id,mission_id,project_id,objective,status) "
            "SELECT %s,%s,project_id,%s,'draft' FROM omp_work.projects WHERE workspace_id=%s "
            "ORDER BY project_id LIMIT 1 ON CONFLICT DO NOTHING",
            (workspace_id, mission_id, draft.goal.statement, workspace_id),
        )
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id,
                               run_id=run_id, expected_revision=-1, operation_id=operation_id)
        append_revision(cur, locked, body, operation_id=operation_id)
        return _assessment(cur, workspace_id, mission_id, run_id, mode)


def _assessment(cur, workspace_id, mission_id, run_id, mode):
    if mode not in ("pass", "fail"):
        raise ValueError(mode)
    head = read_head(cur, workspace_id=workspace_id, mission_id=mission_id)
    assert head is not None and head.run_id == run_id
    operation_id = uuid4()
    locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id, run_id=run_id,
                           expected_revision=head.revision, operation_id=operation_id)
    body = read_revision(cur, workspace_id=workspace_id, run_id=run_id, revision=head.revision).body
    evaluation = evaluate_provenance(cur, body)
    assert evaluation.assessment is not None and evaluation.binding is not None and evaluation.closure is not None
    assessment_id = f"assessment:{operation_id}"
    receipts = tuple(receipt.model_copy(update={"id": f"{receipt.id}:{operation_id}"})
                     for receipt in evaluation.receipts)
    append_assessment(cur, locked, IntakeAssessmentView(
        id=assessment_id, binding=evaluation.binding,
        assessment=evaluation.assessment.model_copy(update={
            "status": "READY_FOR_RATIFICATION" if mode == "pass" else "CONFLICTED",
            "derivation_receipt_ids": [receipt.id for receipt in receipts],
        }),
        receipts=receipts, provenance=evaluation.closure.closure,
    ), operation_id)
    return assessment_id


def advance_revision(store, *, workspace_id, actor_id, mission_id):
    store = _ops(store)
    with cursor(store, workspace_id=workspace_id, actor_id=actor_id) as cur:
        head = read_head(cur, workspace_id=workspace_id, mission_id=mission_id)
        assert head is not None
        body = read_revision(cur, workspace_id=workspace_id, run_id=head.run_id, revision=head.revision).body
        body.snapshot.revision += 1
        body.binding.revision += 1
        body.assessments = []
        body.derivations = []
        operation_id = uuid4()
        locked = lock_revision(cur, workspace_id=workspace_id, mission_id=mission_id, run_id=head.run_id,
                               expected_revision=head.revision, operation_id=operation_id)
        return append_revision(cur, locked, body, operation_id=operation_id)


def record_pass(store, *, workspace_id, actor_id, mission_id):
    store = _ops(store)
    with cursor(store, workspace_id=workspace_id, actor_id=actor_id) as cur:
        head = read_head(cur, workspace_id=workspace_id, mission_id=mission_id)
        assert head is not None
        return _assessment(cur, workspace_id, mission_id, head.run_id, "pass")


def forge_receipt():
    return f"assessment:{uuid4()}"


def _ops(store):
    return store if isinstance(store, NativeJobStore) else NativeJobStore(store._config)
