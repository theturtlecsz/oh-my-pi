"""NSI-2 provenance closure keeps original refs and does not coerce premise truth."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from omp_work.contracts.nsi.v1.compat import bounded_to_nsi, intake_contract
from omp_work.contracts.nsi.v1.models import (
    AcceptanceCriterion, ActorRef, Binding, ClarificationQuestion, EntityRef, EvidenceArtifact,
    EvidenceSpan, Gap, IntakeClaim, OwnerDecision, OwnerRef, PredicateDefinition, RequirementClause,
    TypedBooleanValue, TypedStringValue,
)
from omp_work.contracts.nsi.v1.validate import content_hash
from omp_work.intake_repository import (
    DecisionHistory, DecisionRecord, EvidenceLinkRecord, InstructionProvenance as StoredInstruction,
    IntakeRevisionBody, append_revision, read_revision, revision_hashes,
)
from omp_work.v1.api_models import IntakeCriterionLink, IntakeDecisionRecord, RelayProvenance
from omp_work.v1.intake_provenance_closure import resolve_provenance_closure
from omp_work.v1.models import BoundedIntakeDraft, OwnerInstruction, RelayedInstruction
from omp_work.v1.store_shared import WorkStoreError
from test_nsi2_repository import connection, locked, write_repository  # noqa: F401
from test_workflow_service import OWNER, service  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

FIXTURE = Path(__file__).parent / "fixtures" / "bounded_intake_golden.json"
RUN = "run-1"
TEXT = "  Keep café ≥ 99.9%.\r\nDo NOT trim.\t🙂  "
NOTE = "side observation"
SOURCE_URI = "https://127.0.0.1:9/not-fetched"
RECEIVED = "2026-10-11T00:00:00Z"
SOURCE_ACTOR = ActorRef(id="source-owner", kind="owner")
DECISION_ACTOR = OwnerRef(id="decision-owner", kind="owner")


def _draft(*, drop_unknowns: bool) -> BoundedIntakeDraft:
    cases = json.loads(FIXTURE.read_text())
    raw = next(case["input"]["draft"] for case in cases if case["id"] == "01-valid-ready")
    if drop_unknowns:
        raw = {**raw, "unknowns": []}
    return BoundedIntakeDraft.model_validate(raw)


def _annotation(claim_id, premises, *, state="admitted", truth="true", asserted_by=None, admitted_by=None, evidence=None):
    actor = asserted_by or ActorRef(id="bounded-intake", kind="owner")
    fields = dict(
        id=claim_id, category="terminology", subject=EntityRef(id=claim_id, type="annotation"),
        predicate_id="bounded.annotation", value=TypedStringValue(type="string", value=claim_id),
        origin="owner_statement", state=state, modality="observed", polarity="positive", truth=truth,
        asserted_by=actor, evidence=list(evidence or []), premise_ids=list(premises),
    )
    if admitted_by is not None:
        fields["admitted_by"] = admitted_by
    elif state == "admitted":
        fields["admitted_by"] = OwnerRef(id=actor.id, kind="owner")
    return IntakeClaim(**fields)


def _retarget(claims):
    retargeted = []
    for claim in claims:
        if claim.predicate_id != "bounded.source_span":
            retargeted.append(claim)
            continue
        span_id = json.loads(claim.value.value)["id"]
        retargeted.append(claim.model_copy(update={
            "id": span_id, "subject": EntityRef(id=span_id, type=claim.subject.type),
        }))
    return retargeted


def _criterion(criterion_id, requirement_id, description, method, claim_ids):
    requirement = RequirementClause(
        id=requirement_id, category="completion_signals", text=description,
        claim_ids=list(claim_ids), dimension_ids=["missing_verification_oracle"],
    )
    criterion = AcceptanceCriterion(
        id=criterion_id, requirement_ids=[requirement_id], description=description,
        verification_method=method, expected_result=TypedStringValue(type="string", value=description),
    )
    return criterion, IntakeCriterionLink(criterion=criterion, requirements=(requirement,))


def _entities(claims):
    entities, seen = [], set()
    for claim in claims:
        if claim.subject.id in seen:
            continue
        seen.add(claim.subject.id)
        entities.append(claim.subject)
    return entities


def make_body(workspace_id, *, drop_unknowns=True, relay=True) -> IntakeRevisionBody:
    draft = _draft(drop_unknowns=drop_unknowns)
    snapshot = bounded_to_nsi(draft).model_copy(update={"run_id": RUN, "revision": 0})
    source_bytes = draft.source.text.encode()
    artifacts = []
    for artifact in snapshot.artifacts:
        if artifact.id == "bounded-source":
            artifact = artifact.model_copy(update={"uri": SOURCE_URI, "revision": "source-rev-7", "actor": SOURCE_ACTOR})
        artifacts.append(artifact)
    instruction_bytes = TEXT.encode()
    artifacts.append(EvidenceArtifact(
        id="instruction:decision-1", uri=f"intake:{RUN}/instruction/decision-1",
        content_hash=hashlib.sha256(instruction_bytes).hexdigest(), revision="instr-rev-0",
        actor=ActorRef(id="decision-owner", kind="owner"), media_type="text/plain",
    ))
    claims = _retarget(list(snapshot.claims))
    claims.extend((
        _annotation("ancestor", ["span-export"], evidence=[EvidenceSpan(
            artifact_id="instruction:decision-1", start_byte=0, end_byte=len(instruction_bytes), quote=TEXT,
        )]),
        _annotation("mid", ["ancestor"]),
        _annotation("sibling", ["mid", "ancestor"]),
        _annotation("leaf", ["mid"]),
        _annotation("late", ["mid"], state="proposed", truth="unknown", asserted_by=ActorRef(id="assert-owner", kind="owner")),
        _annotation("retired", [], state="superseded", truth="true", admitted_by=OwnerRef(id="bounded-intake", kind="owner")),
    ))
    export, export_link = _criterion(
        "ac-export", "req-ac-export", "JSON export is checked by a test", "automated_test", ["ac-export", "goal-ready"],
    )
    table, table_link = _criterion(
        "ac-table", "req-ac-table", "The human table is still present", "static_check", ["ac-table", "goal-ready"],
    )
    instruction = OwnerInstruction.model_validate({
        "text": TEXT,
        "provenance": {
            "channel": "relay" if relay else "owner-chat",
            "message_ref": "msg:exact",
            "received_at": RECEIVED,
        },
    })
    decision = OwnerDecision(
        id="decision-1", run_id=RUN, revision=0, actor=DECISION_ACTOR, question_id="question-1",
        answer=TypedBooleanValue(type="boolean", value=True), reason="Record the owner words.",
        created_at="2026-10-10T00:00:00Z",
    )
    record = IntakeDecisionRecord(
        decision=decision, claim_ids=("goal-ready",), instruction=instruction,
        verified_relay_operation_id=uuid4() if relay else None,
    )
    empty = content_hash({})
    snapshot = snapshot.model_copy(update={"artifacts": artifacts, "claims": claims, "entities": _entities(claims)})
    return IntakeRevisionBody(
        workspace_id=workspace_id, snapshot=snapshot,
        binding=Binding(
            run_id=RUN, revision=0, contract_hash=empty, claim_set_hash=empty, rule_bundle_hash=empty,
            schema_hash=empty, source_hash=empty,
        ),
        artifact_bytes={"bounded-source": source_bytes, "instruction:decision-1": instruction_bytes},
        gaps=[Gap(
            id="gap-1", dimension_id="missing_verification_oracle", premise_id="goal-ready",
            reason="The owner must confirm the export.", materiality="critical", waiver_policy="never",
        )],
        questions=[ClarificationQuestion(
            id="question-1", question="Confirm the owner words?", gap_ids=["gap-1"], conflict_ids=[],
            category="completion_signals",
        )],
        decisions=[DecisionRecord(decision=decision.model_copy(deep=True), settled_claim_ids=["goal-ready"])],
        instructions=[StoredInstruction(
            id="instruction-1", decision_id="decision-1", claim_id="ancestor",
            artifact_id="instruction:decision-1", instruction=TEXT,
        )],
        criteria=[export, table],
        supersessions={"ancestor": ["retired"]},
        history=DecisionHistory(
            decisions=[record], criteria=[export_link, table_link],
            instruction_hashes={"decision-1": hashlib.sha256(instruction_bytes).hexdigest()},
            claim_supersession_decisions={"retired": "decision-1"},
            relays={"decision-1": {"instruction": {"text": "WRONG COPY"}}},
        ),
    )


def _replace(body, claim_id, **changes):
    body.snapshot.claims = [
        claim.model_copy(update=changes) if claim.id == claim_id else claim
        for claim in body.snapshot.claims
    ]
    return body


def sync_links(body):
    return body.model_copy(update={"evidence_links": [
        EvidenceLinkRecord(
            claim_id=claim.id, artifact_id=span.artifact_id, start_byte=span.start_byte,
            end_byte=span.end_byte, quote=span.quote, relation="supports",
        )
        for claim in body.snapshot.claims for span in claim.evidence
    ]})


def seal(body):
    claim_hash, source_hash = revision_hashes(body.snapshot, body.artifact_bytes)
    return body.model_copy(update={"binding": body.binding.model_copy(update={
        "run_id": body.snapshot.run_id, "revision": body.snapshot.revision,
        "claim_set_hash": claim_hash, "source_hash": source_hash,
    })})


def advance(body, revision):
    cloned = body.model_copy(deep=True)
    cloned.snapshot.revision = revision
    cloned.binding.revision = revision
    for item in cloned.decisions:
        item.decision.revision = revision
    return cloned


def insert_event(cur, workspace_id, mission_id, operation_id, payload, event_type):
    event_id = uuid4()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cur.execute(
        "INSERT INTO omp_audit.domain_events("
        "event_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,actor_id,actor_kind,"
        "capability_id,request_id,correlation_id,operation_id,causation_id,event_type,outcome,payload,"
        "payload_sha256,previous_event_sha256,event_sha256"
        ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            event_id, workspace_id, "mission", mission_id, 1, OWNER, "owner", operation_id, uuid4(),
            uuid4(), operation_id, operation_id, event_type, "applied", Jsonb(payload), digest, None, digest,
        ),
    )
    return event_id


def insert_relay(cur, workspace_id, mission_id, operation_id, relay):
    cur.execute(
        "INSERT INTO omp_control.idempotent_commands("
        "workspace_id,operation_id,request_id,correlation_id,command_type,required_scope,request_sha256,state,response"
        ") VALUES(%s,%s,%s,%s,'relay_owner_intent','work.mutate',%s,'applied',%s)",
        (
            workspace_id, operation_id, uuid4(), uuid4(), "ab" * 32,
            Jsonb({"type": "relay_owner_intent", "relay": relay}),
        ),
    )
    return insert_event(cur, workspace_id, mission_id, operation_id, {"relay": relay}, "relay_owner_intent")


def relay_json(instruction, text=None):
    return RelayProvenance(
        instruction=RelayedInstruction(
            text=instruction.text if text is None else text,
            source_message_ref=instruction.provenance.message_ref,
            owner_authored=True, received_at=instruction.provenance.received_at,
        ),
        relayed_by=OWNER, signed=True,
    ).model_dump(mode="json")


def append_body(cur, repository, body, operation_id, expected):
    body = seal(sync_links(body))
    workspace_id, mission_id = repository
    append_revision(cur, locked(cur, repository, expected=expected, operation_id=operation_id), body, operation_id=operation_id)
    event_id = insert_event(cur, workspace_id, mission_id, operation_id, {"type": "intake_revision"}, "intake_revision")
    return body, event_id


def mutate_dangling(body):
    link = body.history.criteria[0]
    requirement = link.requirements[0].model_copy(update={"claim_ids": ["missing-claim"]})
    criteria = list(body.history.criteria)
    criteria[0] = link.model_copy(update={"requirements": (requirement,)})
    body.history.criteria = criteria
    return body


def mutate_cycle(body):
    _replace(body, "leaf", premise_ids=["sibling"])
    return _replace(body, "sibling", premise_ids=["leaf"])


def mutate_unadmitted(body):
    return _replace(body, "leaf", premise_ids=["late"])


def mutate_superseded(body):
    _replace(body, "ancestor", state="superseded")
    replacement = _annotation("ancestor-next", [])
    body.snapshot.claims.append(replacement)
    body.snapshot.entities.append(replacement.subject)
    body.supersessions["ancestor-next"] = ["ancestor"]
    body.history.claim_supersession_decisions["ancestor"] = "decision-1"
    return body


def mutate_span(body):
    goal = next(claim for claim in body.snapshot.claims if claim.id == "goal-ready")
    evidence = list(goal.evidence)
    evidence[0] = evidence[0].model_copy(update={"end_byte": 1_000_000})
    return _replace(body, "goal-ready", evidence=evidence)


def mutate_quote(body):
    span = next(claim for claim in body.snapshot.claims if claim.id == "span-export")
    evidence = list(span.evidence)
    evidence[0] = evidence[0].model_copy(update={"quote": "not the source slice"})
    return _replace(body, "span-export", evidence=evidence)


def mutate_hash(body):
    body.snapshot.artifacts = [
        artifact.model_copy(update={"content_hash": "b" * 64}) if artifact.id == "bounded-source" else artifact
        for artifact in body.snapshot.artifacts
    ]
    return body


def mutate_missing_trace(body):
    body.history.criteria = []
    body.criteria = list(intake_contract(body.snapshot).acceptance_criteria)
    return body


def mutate_foreign(body):
    body.snapshot.ontology.predicates.append(PredicateDefinition(
        id="foreign.custom", title="Foreign predicate", subject_types=["widget"], object_types=["value"],
        cardinality="one", exclusivity=False, transitivity=False, authority=["owner"],
    ))
    claim = IntakeClaim(
        id="foreign-claim", category="terminology", subject=EntityRef(id="foreign-claim", type="widget"),
        predicate_id="foreign.custom", value=TypedStringValue(type="string", value="foreign"),
        origin="owner_statement", state="admitted", modality="observed", polarity="positive", truth="true",
        asserted_by=ActorRef(id="foreign-owner", kind="owner"),
        admitted_by=OwnerRef(id="foreign-owner", kind="owner"), evidence=[], premise_ids=[],
    )
    body.snapshot.claims.append(claim)
    body.snapshot.entities.append(claim.subject)
    return body


def mutate_false(body):
    return _replace(body, "c-json", truth="false")


def _store_and_resolve(cur, repository, body, *, journal):
    workspace_id, mission_id = repository
    body, _event = append_body(cur, repository, body, uuid4(), -1)
    instruction = body.history.decisions[0].instruction
    relay_id = body.history.decisions[0].verified_relay_operation_id
    if journal == "mismatch":
        relay = relay_json(instruction, text="trimmed words")
        insert_relay(cur, workspace_id, mission_id, relay_id, relay)
    stored = read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0)
    return resolve_provenance_closure(cur, stored.body)


@pytest.mark.parametrize(("diagnostic", "mutate", "journal", "drop_unknowns"), [
    ("dangling reference", mutate_dangling, "none", True),
    ("cycle", mutate_cycle, "none", True),
    ("unadmitted reference", mutate_unadmitted, "none", True),
    ("superseded reference", mutate_superseded, "none", True),
    ("unknown span", mutate_span, "none", True),
    ("inconsistent quote", mutate_quote, "none", True),
    ("inconsistent hash", mutate_hash, "none", True),
    ("missing criterion trace", mutate_missing_trace, "none", True),
    ("foreign predicate", mutate_foreign, "none", True),
    ("unresolved truth", None, "none", False),
    ("false premise", mutate_false, "none", True),
    ("dangling reference", None, "none", True),
    ("inconsistent relay", None, "mismatch", True),
])
def test_invalid_proof_refuses(service, write_repository, diagnostic, mutate, journal, drop_unknowns):
    body = make_body(write_repository[0], drop_unknowns=drop_unknowns, relay=True)
    if mutate is not None:
        body = mutate(body)
    with connection(service, write_repository[0]) as conn, conn.cursor() as cur:
        with pytest.raises(WorkStoreError) as caught:
            _store_and_resolve(cur, write_repository, body, journal=journal)
    assert caught.value.code == "invalid_request"
    assert caught.value.diagnostics[0] == diagnostic


def test_unknown_and_false_truth_stay_explicit(service, write_repository):
    workspace_id = write_repository[0]
    unknown = make_body(workspace_id, drop_unknowns=False, relay=False)
    unknown.snapshot.state = "needs_spec"
    false_body = make_body(workspace_id, drop_unknowns=True, relay=False)
    false_body.snapshot.state = "needs_spec"
    mutate_false(false_body)
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, unknown, uuid4(), -1)
        unknown_result = resolve_provenance_closure(
            cur, read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body,
        )
        conn.rollback()
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        append_body(cur, write_repository, false_body, uuid4(), -1)
        false_result = resolve_provenance_closure(
            cur, read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=0).body,
        )
    unknown_truths = dict(unknown_result.truths)
    false_truths = dict(false_result.truths)
    assert unknown_truths["choice-name"] == "unknown"
    assert unknown_truths["choice-sort"] == "unknown"
    assert unknown_result.ready is False
    assert false_truths["c-json"] == "false"
    assert false_result.ready is False
    assert "true" in unknown_truths.values()
    assert unknown_truths["goal-ready"] == "true"


def _operations(result):
    refs = [source.operation for source in result.closure.sources]
    refs.extend(item.operation for item in result.closure.admissions)
    refs.extend(item.operation for item in result.closure.decisions)
    refs.extend(item.operation for item in result.closure.supersessions)
    refs.extend(item.operation for item in result.closure.relays)
    refs.extend(item.operation for item in result.assertions)
    return refs


def test_closure_lists_consumed_ancestry_and_persisted_refs(service, write_repository):
    workspace_id, mission_id = write_repository
    draft = _draft(drop_unknowns=True)
    body = make_body(workspace_id, drop_unknowns=True, relay=True)
    relay_id = body.history.decisions[0].verified_relay_operation_id
    op_assert, op_admit, op_copy = uuid4(), uuid4(), uuid4()
    with connection(service, workspace_id) as conn, conn.cursor() as cur:
        events = {}
        body, events[op_assert] = append_body(cur, write_repository, body, op_assert, -1)
        body = advance(body, 1)
        claims = []
        for claim in body.snapshot.claims:
            if claim.id == "late":
                claim = claim.model_copy(update={
                    "state": "admitted", "truth": "true",
                    "admitted_by": OwnerRef(id="admit-owner", kind="owner"),
                })
            elif claim.id == "leaf":
                claim = claim.model_copy(update={"premise_ids": ["late"]})
            claims.append(claim)
        body.snapshot.claims = claims
        note = NOTE.encode()
        body.snapshot.artifacts.append(EvidenceArtifact(
            id="extra-note", uri="https://127.0.0.1:9/extra-note",
            content_hash=hashlib.sha256(note).hexdigest(), revision="note-rev-1",
            actor=ActorRef(id="note-owner", kind="owner"), media_type="text/plain",
        ))
        body.artifact_bytes["extra-note"] = note
        body, events[op_admit] = append_body(cur, write_repository, body, op_admit, 0)
        body = advance(body, 2)
        body, events[op_copy] = append_body(cur, write_repository, body, op_copy, 1)
        relay_event = insert_relay(cur, workspace_id, mission_id, relay_id, relay_json(body.history.decisions[0].instruction))
        result = resolve_provenance_closure(
            cur, read_revision(cur, workspace_id=workspace_id, run_id=RUN, revision=2).body,
        )

    closure = result.closure
    assert closure.run_id == RUN
    assert closure.revision == 2
    assert result.ready is True
    assert {value for _claim_id, value in result.truths} == {"true"}
    assert {claim_id for claim_id, _value in result.truths} == {item.claim.id for item in closure.claims}
    claim_ids = [item.claim.id for item in closure.claims]
    assert len(claim_ids) == len(set(claim_ids))
    for required in (
        "goal-ready", "span-export", "span-table", "span-test", "ancestor", "mid", "sibling", "leaf",
        "late", "ac-export", "ac-table", "bounded.source", "bounded.archetype",
    ):
        assert required in claim_ids
    assert "retired" not in claim_ids
    assert "choice-name" not in claim_ids
    assert "choice-sort" not in claim_ids
    by_id = {item.claim.id: item for item in closure.claims}
    assert by_id["sibling"].ancestry_ids.count("ancestor") == 1
    assert by_id["sibling"].ancestry_ids.count("mid") == 1
    assert "span-export" in by_id["sibling"].ancestry_ids
    for ancestor_id in ("late", "mid", "ancestor", "span-export"):
        assert by_id["leaf"].ancestry_ids.count(ancestor_id) == 1
    assert by_id["leaf"].evidence_links["ancestor"][0].span.quote == TEXT
    assert by_id["span-export"].evidence_links["span-export"][0].span.quote == draft.source.text.encode()[:28].decode()
    assert by_id["ancestor"].evidence_links["ancestor"][0].span.quote == TEXT

    source = next(item for item in closure.sources if item.artifact.id == "bounded-source")
    instruction = next(item for item in closure.sources if item.artifact.id == "instruction:decision-1")
    note_source = next(item for item in closure.sources if item.artifact.id == "extra-note")
    assert source.artifact.uri == SOURCE_URI
    assert source.artifact.revision == "source-rev-7"
    assert source.artifact.actor.id == "source-owner"
    assert source.artifact.actor.kind == "owner"
    assert source.source.text == draft.source.text
    assert source.source.sha256 == draft.source.sha256
    assert source.operation.operation_id == op_assert
    assert source.operation.event_id == events[op_assert]
    assert instruction.source.text == TEXT
    assert instruction.artifact.revision == "instr-rev-0"
    assert instruction.artifact.actor.id == "decision-owner"
    assert instruction.operation.operation_id == op_assert
    assert note_source.source.text == NOTE
    assert note_source.artifact.revision == "note-rev-1"
    assert note_source.artifact.actor.id == "note-owner"
    assert note_source.operation.operation_id == op_admit
    assert note_source.operation.event_id == events[op_admit]
    assert closure.source_sha256 == draft.source.sha256
    assert NOTE not in result.projection.source.text
    assert result.projection.source.text == draft.source.text
    assert result.projection.source.sha256 == draft.source.sha256
    assert result.projection.goal.id == "goal-ready"
    assert set(result.projection.goal.source_span_ids) == {"span-export", "span-table"}
    assert {span.id for span in result.projection.source.spans} == {"span-export", "span-table", "span-test"}
    assert result.projection.unknowns == ()
    assert {item.id for item in result.projection.acceptance_criteria} == {"ac-export", "ac-table"}

    decision = closure.decisions[0]
    assert decision.record.decision.revision == 0
    assert decision.record.instruction.text == TEXT
    assert decision.record.instruction.provenance.message_ref == "msg:exact"
    assert decision.operation.operation_id == op_assert
    assert decision.operation.event_id == events[op_assert]
    relay = closure.relays[0]
    assert relay.relay.instruction.text == TEXT
    assert relay.relay.instruction.text != "WRONG COPY"
    assert relay.relay.instruction.source_message_ref == "msg:exact"
    assert relay.relay.signed is True
    assert relay.relay.relayed_by == OWNER
    assert relay.operation.operation_id == relay_id
    assert relay.operation.event_id == relay_event
    assertions = {item.claim_id: item.operation for item in result.assertions}
    admissions = {item.claim_id: item.operation for item in closure.admissions}
    assert assertions["late"].operation_id == op_assert
    assert assertions["late"].event_id == events[op_assert]
    assert admissions["late"].operation_id == op_admit
    assert admissions["late"].event_id == events[op_admit]
    assert admissions["late"].operation_id != assertions["late"].operation_id
    assert next(item.admitted_by.id for item in closure.admissions if item.claim_id == "late") == "admit-owner"
    supersession = next(item for item in closure.supersessions if item.old_claim_id == "retired")
    assert supersession.replacement_claim_id == "ancestor"
    assert supersession.decision_id == "decision-1"
    assert supersession.operation.operation_id == op_assert
    traces = {item.criterion_id: item for item in closure.traces}
    assert set(traces) == {"ac-export", "ac-table"}
    assert traces["ac-export"].requirement_ids == ("req-ac-export",)
    assert "requirement:ac-export" not in traces["ac-export"].requirement_ids
    assert traces["ac-export"].claim_ids == ("ac-export", "goal-ready")
    operations = _operations(result)
    assert op_copy not in {item.operation_id for item in operations}
    assert events[op_copy] not in {item.event_id for item in operations}
