"""Analyze bounded provenance without substituting generated contract records."""
from __future__ import annotations

import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, NoReturn, Sequence
from uuid import UUID
from urllib.parse import urljoin

from psycopg import Cursor

from ..contracts.nsi.v1 import authority, compat, models as nsi, validate
from ..contracts.nsi.v1.compat import (
    NsiBoundedEvaluation,
    bounded_to_nsi,
    intake_contract,
    nsi_bounded_evaluation,
    nsi_to_bounded,
)
from ..contracts.nsi.v1.models import AcceptanceCriterion, IntakeContract, RequirementClause
from ..intake_repository import (
    DecisionRecord, IntakeRepositoryError, IntakeRevisionBody, append_assessment, lock_revision, read_revision,
)
from . import api_models, intake_decisions, intake_provenance_closure, models, semantics
from .api_models import AssessIntakeResult, IntakeAssessmentView, IntakeDecisionRecord, IntakeIssueRecord
from .intake_decisions import _latest_findings, open_issues, unresolved_findings
from .intake_provenance_closure import ProvenanceClosure, resolve_provenance_closure
from .models import AssessIntakeCommand, BoundedIntakeDraft, CommandEnvelope
from .store_shared import WorkStoreError


class ProvenanceSettlement(NamedTuple):
    settlement: IntakeRevisionBody
    links: dict[str, tuple[str, ...]]
    unresolved: tuple[nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion, ...]

    @property
    def copy(self) -> IntakeRevisionBody:
        return self.settlement

    @property
    def body(self) -> IntakeRevisionBody:
        return self.settlement

    @property
    def gap_links(self) -> dict[str, tuple[str, ...]]:
        return self.links

    @property
    def unresolved_findings(
        self,
    ) -> tuple[nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion, ...]:
        return self.unresolved


class ProvenanceEvaluation(NamedTuple):
    findings: NsiBoundedEvaluation
    contract: IntakeContract
    settlement: ProvenanceSettlement | None = None
    binding: nsi.Binding | None = None
    closure: ProvenanceClosure | None = None
    assessment: nsi.IntakeReadinessAssessment | None = None
    receipts: tuple[nsi.DerivationReceipt, ...] = ()
    unresolved: tuple[nsi.Gap | nsi.Conflict | nsi.ClarificationQuestion, ...] = ()

    @property
    def projection(self) -> BoundedIntakeDraft | None:
        return None if self.closure is None else self.closure.projection


def helper_sources() -> dict[str, str]:
    """Read complete loaded helper modules, including private rules and readiness.

    Module names identify the bundle; locations and mtimes are not hash inputs.
    Read bytes on every call so a prior evaluation cannot cache stale content.
    """
    modules = (
        compat, validate, authority, nsi, semantics, models, api_models,
        sys.modules[__name__], intake_provenance_closure, intake_decisions,
    )
    return {module.__name__: Path(inspect.getfile(module)).read_bytes().hex() for module in modules}


def referenced_schemas() -> dict[str, dict[str, object]]:
    """Load the contract/status schema closure through the offline bundled loader."""
    documents: dict[str, dict[str, object]] = {}
    base = "https://omp.dev/contracts/nsi/v1/"

    def load(name: str) -> None:
        if name in documents:
            return
        document = validate.load_schema(name)
        documents[name] = document
        walk(document, str(document["$id"]))

    def walk(node: object, location: str) -> None:
        if isinstance(node, dict):
            if "$id" in node:
                location = urljoin(location, str(node["$id"]))
            ref = node.get("$ref")
            if isinstance(ref, str):
                target = urljoin(location, ref).partition("#")[0]
                if not target.startswith(base):
                    raise ValueError(f"Unknown NSI schema reference: {ref}")
                load(target.removeprefix(base).removesuffix(".schema.json"))
            for value in node.values():
                walk(value, location)
        elif isinstance(node, list):
            for value in node:
                walk(value, location)

    load("contract")
    load("status")
    return documents


def build_provenance_binding(
    body: IntakeRevisionBody,
    resolved: ProvenanceClosure,
    contract: IntakeContract,
    settlement: ProvenanceSettlement,
) -> nsi.Binding:
    """Bind snapshot identity, complete proof, implementation, and schema content."""
    proof_hash = validate.closure_hash(closure_document(body, resolved, contract, settlement))
    return nsi.Binding(
        run_id=body.snapshot.run_id,
        revision=body.snapshot.revision,
        contract_hash=validate.contract_hash(contract),
        claim_set_hash=proof_hash,
        source_hash=proof_hash,
        rule_bundle_hash=validate.content_hash(helper_sources()),
        schema_hash=validate.content_hash(referenced_schemas()),
    )


def closure_document(
    body: IntakeRevisionBody,
    resolved: ProvenanceClosure,
    contract: IntakeContract,
    settlement: ProvenanceSettlement,
) -> dict[str, object]:
    """Bind the complete proof and original history, independent of prior assessments.

    ``closure_hash(document)`` supplies both claim-set and source binding slots;
    ``contract_hash(contract)`` supplies the contract slot. Revision hashes and
    placeholder bindings are deliberately not inputs to either digest.
    """
    excluded = {"binding", "artifact_bytes", "assessments", "derivations"}
    return {
        "body": body.model_dump(mode="json", exclude=excluded),
        # Sources contain only decodable text. Bind all bytes independently,
        # including unreferenced binary artifacts, without a lossy text decode.
        "artifact_bytes_hex": {
            artifact_id: payload.hex() for artifact_id, payload in body.artifact_bytes.items()
        },
        "provenance": resolved.closure.model_dump(mode="json"),
        "assertions": [
            {"claim_id": assertion.claim_id, "operation": assertion.operation.model_dump(mode="json")}
            for assertion in resolved.assertions
        ],
        "truths": [list(item) for item in resolved.truths],
        "ready": resolved.ready,
        "contract": contract.model_dump(mode="json"),
        "settlement": {
            "body": settlement.body.model_dump(mode="json", exclude=excluded),
            "conflict_gap_links": {key: list(value) for key, value in settlement.links.items()},
            "unresolved": [item.model_dump(mode="json") for item in settlement.unresolved],
        },
    }


def _refuse(diagnostic: str) -> NoReturn:
    raise WorkStoreError("invalid_request", (diagnostic,))


def _is_bounded_derived_id(finding_id: str) -> bool:
    return finding_id.startswith((
        "gap:constraint:",
        "gap:oracle:",
        "gap:authority:",
        "conflict:constraint:",
        "question:constraint:",
        "question:oracle:",
        "question:authority:",
    ))


def settle_provenance(
    body: IntakeRevisionBody,
    resolved: ProvenanceClosure,
    evaluation: ProvenanceEvaluation | NsiBoundedEvaluation | None = None,
) -> ProvenanceSettlement:
    """Merge derived/persisted findings, reconcile decisions, and settle a deep evaluation copy."""
    if evaluation is None:
        evaluation = evaluate_provenance_projection(body, resolved, settle=False)
    if isinstance(evaluation, ProvenanceEvaluation):
        derived = evaluation.findings
    else:
        derived = evaluation

    return settle_provenance_records(body, derived.gaps, derived.conflicts, derived.questions)


def settle_provenance_records(
    body: IntakeRevisionBody, gaps: Sequence[nsi.Gap], conflicts: Sequence[nsi.Conflict],
    questions: Sequence[nsi.ClarificationQuestion],
) -> ProvenanceSettlement:
    """Settle concrete computed or stored records through the same membership rules."""
    persisted_findings, persisted_gap_links = _latest_findings(body)

    derived_gaps = {g.id: g for g in gaps}
    derived_conflicts = {c.id: c for c in conflicts}
    derived_questions = {q.id: q for q in questions}

    # 1. Gaps: refresh derived, remove obsolete bounded-derived, keep unrelated persisted.
    merged_gaps: dict[str, nsi.Gap] = {}
    for gid, gap in derived_gaps.items():
        merged_gaps[gid] = gap.model_copy(deep=True)
    for fid, finding in persisted_findings.items():
        if isinstance(finding, nsi.Gap) and fid not in merged_gaps:
            if not _is_bounded_derived_id(fid):
                merged_gaps[fid] = finding.model_copy(deep=True)

    # 2. Conflicts: refresh derived, remove obsolete bounded-derived, keep unrelated persisted.
    merged_conflicts: dict[str, nsi.Conflict] = {}
    for cid, conflict in derived_conflicts.items():
        merged_conflicts[cid] = conflict.model_copy(deep=True)
    for fid, finding in persisted_findings.items():
        if isinstance(finding, nsi.Conflict) and fid not in merged_conflicts:
            if not _is_bounded_derived_id(fid):
                merged_conflicts[fid] = finding.model_copy(deep=True)

    # 3. Questions:
    # Preserve surviving question conflict links; regenerated questions cannot erase live conflicts.
    # Trim only removed targets.
    merged_questions: dict[str, nsi.ClarificationQuestion] = {}
    for qid, q in derived_questions.items():
        persisted_q = persisted_findings.get(qid)
        persisted_conflicts = (
            list(persisted_q.conflict_ids)
            if isinstance(persisted_q, nsi.ClarificationQuestion)
            else []
        )
        combined_conflicts = list(dict.fromkeys([*q.conflict_ids, *persisted_conflicts]))
        surviving_conflicts = [cid for cid in combined_conflicts if cid in merged_conflicts]
        surviving_gaps = [gid for gid in q.gap_ids if gid in merged_gaps]
        merged_questions[qid] = q.model_copy(deep=True, update={
            "conflict_ids": surviving_conflicts,
            "gap_ids": surviving_gaps,
        })
    for fid, finding in persisted_findings.items():
        if isinstance(finding, nsi.ClarificationQuestion) and fid not in merged_questions:
            if not _is_bounded_derived_id(fid):
                surviving_conflicts = [cid for cid in finding.conflict_ids if cid in merged_conflicts]
                surviving_gaps = [gid for gid in finding.gap_ids if gid in merged_gaps]
                merged_questions[fid] = finding.model_copy(deep=True, update={
                    "conflict_ids": surviving_conflicts,
                    "gap_ids": surviving_gaps,
                })

    # 4. Conflict-to-gap links:
    # Preserve latest persisted conflict-to-gap links, including empty overrides.
    # Trim only removed targets.
    links: dict[str, tuple[str, ...]] = {}
    for cid in merged_conflicts:
        if cid in persisted_gap_links:
            links[cid] = tuple(gid for gid in persisted_gap_links[cid] if gid in merged_gaps)
        else:
            links[cid] = ()

    # 5. Build deep evaluation copy:
    settlement_copy = body.model_copy(deep=True)
    settlement_copy.gaps = list(merged_gaps.values())
    settlement_copy.conflicts = list(merged_conflicts.values())
    settlement_copy.questions = list(merged_questions.values())

    settlement_copy.history.issues = [
        IntakeIssueRecord(issue=conflict.model_copy(deep=True), gaps=links.get(conflict.id, ()))
        for conflict in settlement_copy.conflicts
    ] + [
        IntakeIssueRecord(issue=question.model_copy(deep=True), gaps=tuple(question.gap_ids))
        for question in settlement_copy.questions
    ]

    # 6. Reconcile body.decisions AND history.decisions with current question/claim membership:
    # Exclude obsolete targets from this copy, retain original owner coverage and supersession semantics for survivors.
    # Never broaden coverage or mutate history.
    current_question_ids = set(merged_questions.keys())
    snapshot_claim_ids = {c.id for c in settlement_copy.snapshot.claims}

    # Identify original history decisions and supersessions before filtering
    original_history_by_id = {d.decision.id: d for d in settlement_copy.history.decisions}
    original_superseded = {
        d.superseded_decision_id
        for d in settlement_copy.history.decisions
        if d.superseded_decision_id is not None
    }

    # Step 1: Filter history decisions with current question/claim membership
    reconciled_history_decisions: list[IntakeDecisionRecord] = []
    for d in settlement_copy.history.decisions:
        if d.decision.question_id in current_question_ids and set(d.claim_ids) <= snapshot_claim_ids:
            reconciled_history_decisions.append(d.model_copy(deep=True))

    # Preserve supersession exclusions from original history:
    # A decision that was superseded in original history must not reactivate as an active survivor.
    # It can only remain in history if a surviving decision still supersedes it.
    while True:
        surviving_superseded_ids = {
            d.superseded_decision_id
            for d in reconciled_history_decisions
            if d.superseded_decision_id is not None
        }
        pruned = [
            d for d in reconciled_history_decisions
            if d.decision.id not in original_superseded or d.decision.id in surviving_superseded_ids
        ]
        if len(pruned) == len(reconciled_history_decisions):
            break
        reconciled_history_decisions = pruned

    settlement_copy.history.decisions = reconciled_history_decisions

    # Step 2: Reconcile body.decisions.
    # Original history has precedence over body.decisions.
    # If a decision existed in original history:
    # - If its original history coverage is obsolete (question or claims removed),
    #   it is obsolete and must NOT fall back to broader body.decisions coverage.
    # - If it was superseded in original history, it must NOT be an active decision in body.decisions.
    # - If it survives, its coverage must match original history (never broadened).
    # If a decision was NOT in original history (snapshot-only):
    # - It survives if its question and claims are in current membership.
    reconciled_body_decisions: list[DecisionRecord] = []
    for d in settlement_copy.decisions:
        if d.decision.question_id not in current_question_ids:
            continue
        if d.decision.id in original_history_by_id:
            orig = original_history_by_id[d.decision.id]
            if orig.decision.id in original_superseded:
                continue
            if orig.decision.question_id not in current_question_ids:
                continue
            if not (set(orig.claim_ids) <= snapshot_claim_ids):
                continue
            reconciled_body_decisions.append(
                d.model_copy(deep=True, update={"settled_claim_ids": list(orig.claim_ids)})
            )
        else:
            if set(d.settled_claim_ids) <= snapshot_claim_ids:
                reconciled_body_decisions.append(d.model_copy(deep=True))
    settlement_copy.decisions = reconciled_body_decisions

    unresolved = unresolved_findings(settlement_copy)
    return ProvenanceSettlement(settlement=settlement_copy, links=links, unresolved=unresolved)


settle_provenance_findings = settle_provenance


def evaluate_provenance_projection(
    body: IntakeRevisionBody,
    resolved: ProvenanceClosure,
    *,
    settle: bool = True,
) -> ProvenanceEvaluation:
    """Return findings and the actual traced contract for a locked revision.

    Reconstructed claim truths are only an analysis input. Certification must
    use ``resolved.truths`` and ``resolved.ready``, never reconstructed truths.
    Stored records are retained intact so contract hashing sees their IDs,
    wording, and typed expected results.
    """
    if (resolved.closure.run_id, resolved.closure.revision) != (body.snapshot.run_id, body.snapshot.revision):
        _refuse("inconsistent source")
    try:
        snapshot = bounded_to_nsi(resolved.projection)
        nsi_to_bounded(snapshot)
        findings = nsi_bounded_evaluation(snapshot)
        generated = intake_contract(snapshot)
    except ValueError as exc:
        raise WorkStoreError("invalid_request", ("unsupported projection",)) from exc

    criteria: dict[str, AcceptanceCriterion] = {}
    for criterion in body.criteria:
        if criterion.id in criteria:
            _refuse("inconsistent criterion")
        criteria[criterion.id] = criterion
    links = {link.criterion.id: link for link in body.history.criteria}
    traces = {trace.criterion_id: trace for trace in resolved.closure.traces}
    projected = {criterion.id: criterion for criterion in generated.acceptance_criteria}
    if len(links) != len(body.history.criteria) or len(traces) != len(resolved.closure.traces):
        _refuse("inconsistent criterion")
    if set(criteria) != set(links) or set(criteria) != set(traces) or set(criteria) != set(projected):
        _refuse("missing criterion trace")

    requirements: dict[str, RequirementClause] = {}
    for criterion in criteria.values():
        link, trace = links[criterion.id], traces[criterion.id]
        if criterion.model_dump(mode="json") != link.criterion.model_dump(mode="json"):
            _refuse("inconsistent criterion")
        if criterion.verification_method != projected[criterion.id].verification_method:
            _refuse("incompatible verification method")
        if (
            not criterion.requirement_ids
            or len(set(criterion.requirement_ids)) != len(criterion.requirement_ids)
            or criterion.requirement_ids != [item.id for item in link.requirements]
            or tuple(criterion.requirement_ids) != trace.requirement_ids
        ):
            _refuse("missing criterion trace")
        claim_ids = tuple(dict.fromkeys(claim_id for item in link.requirements for claim_id in item.claim_ids))
        if claim_ids != trace.claim_ids or any(not item.claim_ids for item in link.requirements):
            _refuse("missing criterion trace")
        for requirement in link.requirements:
            previous = requirements.get(requirement.id)
            if previous is not None and previous.model_dump(mode="json") != requirement.model_dump(mode="json"):
                _refuse("inconsistent requirement")
            requirements[requirement.id] = requirement

    replaced_ids = {requirement_id for criterion in generated.acceptance_criteria for requirement_id in criterion.requirement_ids}
    for requirement in generated.requirements:
        if requirement.id in replaced_ids:
            continue
        previous = requirements.get(requirement.id)
        if previous is not None and previous.model_dump(mode="json") != requirement.model_dump(mode="json"):
            _refuse("inconsistent requirement")
        if previous is None:
            requirements[requirement.id] = requirement
    contract = generated.model_copy(deep=True, update={
        "requirements": [item.model_copy(deep=True) for item in requirements.values()],
        "acceptance_criteria": [item.model_copy(deep=True) for item in criteria.values()],
    })
    settlement = settle_provenance(body, resolved, findings) if settle else None
    return ProvenanceEvaluation(findings=findings, contract=contract, settlement=settlement)


def evaluate_intake_provenance(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceEvaluation:
    """Resolve persisted proof and analyze the caller's locked body without writes."""
    return evaluate_provenance_projection(body, resolve_provenance_closure(cur, body))


def evaluate_provenance(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceEvaluation:
    """Issue pinned analysis receipts without writing or advancing the revision.

    Projection is diagnostic only. Original admitted truths and complete consumed
    ancestry govern certification, even when owner settlement clears every issue.
    Receipt IDs bind the proof and exact derivation links, excluding issue time.
    """
    resolved = resolve_provenance_closure(cur, body)
    evaluation = evaluate_provenance_projection(body, resolved, settle=False)
    settlement = settle_provenance(body, resolved, evaluation)
    binding = build_provenance_binding(body, resolved, evaluation.contract, settlement)
    claims = {item.claim.id: item for item in resolved.closure.claims}
    truths = dict(resolved.truths)
    consumed = set(claims)
    consumed.update(truths)
    consumed.update(ancestor for item in claims.values() for ancestor in item.ancestry_ids)
    consumed.update(claim_id for trace in resolved.closure.traces for claim_id in trace.claim_ids)
    complete = resolved.ready and bool(consumed) and all(
        claim_id in claims and truths.get(claim_id) == "true" for claim_id in consumed
    )
    conflicts = [item for item in settlement.unresolved if isinstance(item, nsi.Conflict)]
    gaps = [item for item in settlement.unresolved if isinstance(item, nsi.Gap)]
    if conflicts:
        status = "CONFLICTED"
    elif settlement.unresolved or not complete:
        status = "NEEDS_SPEC"
    else:
        status = "READY_FOR_RATIFICATION"

    engine = nsi.RuleEngineRef(id="omp-work:nsi2-provenance", kind="rule_engine")
    created_at = datetime.now(timezone.utc).isoformat()
    bound = binding.model_dump()
    receipts: list[nsi.DerivationReceipt] = []

    def receipt(rule_id: str, premise_ids: list[str], conclusion_ids: list[str]) -> None:
        identity = validate.content_hash({
            "binding": bound, "rule_id": rule_id, "premise_ids": premise_ids,
            "conclusion_ids": conclusion_ids, "derived_by": engine.model_dump(),
        })
        receipts.append(nsi.DerivationReceipt(
            id=f"derivation:{identity}", **bound, rule_id=rule_id,
            premise_ids=premise_ids, conclusion_ids=conclusion_ids,
            derived_by=engine, created_at=created_at,
        ))

    for item in resolved.closure.claims:
        receipt("nsi2.provenance.claim", list(item.claim.premise_ids), [item.claim.id])
    for requirement in evaluation.contract.requirements:
        receipt("nsi2.provenance.requirement", list(requirement.claim_ids), [requirement.id])
    for trace in resolved.closure.traces:
        receipt("nsi2.provenance.criterion", list(trace.requirement_ids), [trace.criterion_id])
    assessment = nsi.IntakeReadinessAssessment(
        **bound, status=status, gaps=gaps, conflicts=conflicts,
        derivation_receipt_ids=[item.id for item in receipts],
        assessed_by=engine, created_at=created_at,
    )
    return evaluation._replace(
        settlement=settlement, binding=binding, closure=resolved,
        assessment=assessment, receipts=tuple(receipts), unresolved=settlement.unresolved,
    )


def settle_intake_provenance(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceSettlement:
    """Resolve persisted proof and settle findings on a deep evaluation copy."""
    return settle_provenance(body, resolve_provenance_closure(cur, body))


def assess_intake(
    cur: Cursor, envelope: CommandEnvelope, actor_id: UUID, actor_kind: str,
) -> dict[str, object]:
    """Compute proof under the semantic head lock in the caller transaction."""
    if actor_kind not in ("owner", "rule_engine"):
        raise WorkStoreError("forbidden")
    if not isinstance(envelope.command, AssessIntakeCommand):
        raise WorkStoreError("invalid_request")
    payload = envelope.command.payload
    cur.execute(
        "SELECT mission_id FROM omp_work.intake_heads WHERE workspace_id=%s AND run_id=%s",
        (envelope.workspace_id, payload.run_id),
    )
    head = cur.fetchone()
    if head is None:
        raise WorkStoreError("stale_intake")
    try:
        locked = lock_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
            expected_revision=payload.expected_revision - 1, mission_id=head[0],
            operation_id=envelope.operation_id,
        )
        stored = read_revision(
            cur, workspace_id=envelope.workspace_id, run_id=payload.run_id,
            revision=locked.current_revision,
        )
        evaluation = evaluate_provenance(cur, stored.body)
        if evaluation.assessment is None or evaluation.binding is None:
            raise AssertionError
        if evaluation.closure is None or evaluation.settlement is None:
            raise AssertionError
        append_assessment(cur, locked, IntakeAssessmentView(
            id=f"assessment:{envelope.operation_id}", assessment=evaluation.assessment,
            binding=evaluation.binding, receipts=evaluation.receipts,
            unresolved_issues=open_issues(evaluation.settlement.body),
            provenance=evaluation.closure.closure,
        ), envelope.operation_id)
    except IntakeRepositoryError as exc:
        raise WorkStoreError(
            "stale_intake" if str(exc) == "stale_revision" else "invalid_request",
        ) from exc
    return AssessIntakeResult(
        type="assess_intake", run_id=payload.run_id, revision=payload.expected_revision,
        assessed_revision=payload.expected_revision,
    ).model_dump(mode="json")
