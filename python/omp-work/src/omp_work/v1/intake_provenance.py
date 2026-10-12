"""Analyze bounded provenance without substituting generated contract records."""
from __future__ import annotations

from typing import NamedTuple, NoReturn

from psycopg import Cursor

from ..contracts.nsi.v1.compat import (
    NsiBoundedEvaluation,
    bounded_to_nsi,
    intake_contract,
    nsi_bounded_evaluation,
    nsi_to_bounded,
)
from ..contracts.nsi.v1.models import AcceptanceCriterion, IntakeContract, RequirementClause
from ..intake_repository import IntakeRevisionBody
from .intake_provenance_closure import ProvenanceClosure, resolve_provenance_closure
from .store_shared import WorkStoreError


class ProvenanceEvaluation(NamedTuple):
    findings: NsiBoundedEvaluation
    contract: IntakeContract


def _refuse(diagnostic: str) -> NoReturn:
    raise WorkStoreError("invalid_request", (diagnostic,))


def evaluate_provenance_projection(
    body: IntakeRevisionBody, resolved: ProvenanceClosure,
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
    return ProvenanceEvaluation(findings=findings, contract=contract)


def evaluate_intake_provenance(cur: Cursor, body: IntakeRevisionBody) -> ProvenanceEvaluation:
    """Resolve persisted proof and analyze the caller's locked body without writes."""
    return evaluate_provenance_projection(body, resolve_provenance_closure(cur, body))
