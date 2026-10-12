"""Analyze bounded provenance without substituting generated contract records."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn
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
from ..intake_repository import DecisionRecord, IntakeRevisionBody
from . import api_models, intake_decisions, intake_provenance_closure, models, semantics
from .api_models import IntakeDecisionRecord, IntakeIssueRecord
from .intake_decisions import _latest_findings, unresolved_findings
from .intake_provenance_closure import ProvenanceClosure, resolve_provenance_closure
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

    persisted_findings, persisted_gap_links = _latest_findings(body)

    derived_gaps = {g.id: g for g in derived.gaps}
    derived_conflicts = {c.id: c for c in derived.conflicts}
    derived_questions = {q.id: q for q in derived.questions}

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


def settle_intake_provenance(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceSettlement:
    """Resolve persisted proof and settle findings on a deep evaluation copy."""
    return settle_provenance(body, resolve_provenance_closure(cur, body))
