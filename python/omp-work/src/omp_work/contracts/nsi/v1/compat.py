"""Map today's bounded intake draft onto NSI-1 records and back.

Claim ids are preserved. Constraint values use NSI typed values so boolean and
integer stay distinct, and an unknown value stays unknown rather than false.
Source-span order is kept on premise ids for the roundtrip. Evidence spans are
sorted by byte offset so the NSI content hash follows claim identity, matching
the order-independent bounded semantic hash. Blocking questions are derived
again from those claims as gaps, conflicts, and clarification questions.
"""

from __future__ import annotations

import json
from typing import Literal, NamedTuple

from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    IntakeUnknown,
    ItemBudget,
    KnownIntakeValue,
    UnknownIntakeValue,
)
from omp_work.v1.semantics import (
    bounded_intake_semantic_sha256,
    evaluate_bounded_intake,
    typed_value,
)

from .models import (
    AcceptanceCriterion,
    ActorRef,
    Applicability,
    ClaimModality,
    ClarificationQuestion,
    Conflict,
    EntityRef,
    EvidenceArtifact,
    EvidenceSpan,
    Gap,
    IntakeCategory,
    IntakeClaim,
    IntakeContract,
    IntakeOntology,
    IntakeRunState,
    IntakeSnapshot,
    OwnerRef,
    Polarity,
    PredicateDefinition,
    RequirementClause,
    RequirementDimensionDefinition,
    TruthValue,
    TypedBooleanValue,
    TypedIntegerValue,
    TypedNullValue,
    TypedStringValue,
    TypedValue,
)

OracleName = Literal[
    "automated_test", "static_check", "manual_inspection", "external_receipt"
]

_OWNER_ID = "bounded-intake"
_ACTOR = ActorRef(id=_OWNER_ID, kind="owner")
_OWNER = OwnerRef(id=_OWNER_ID, kind="owner")
_SOURCE_ARTIFACT_ID = "bounded-source"
_SOURCE_URI = "https://omp.dev/contracts/nsi/v1/bounded-intake/source"

_PREDICATE_GOAL = "bounded.goal"
_PREDICATE_CONSTRAINT = "bounded.constraint"
_PREDICATE_ANNOTATION = "bounded.annotation"
_PREDICATE_UNKNOWN = "bounded.unknown"
_PREDICATE_ACCEPTANCE = "bounded.acceptance_criterion"
_PREDICATE_SOURCE = "bounded.source_text"
_PREDICATE_SPAN = "bounded.source_span"
_PREDICATE_ARCHETYPE = "bounded.archetype"
_PREDICATE_BUDGET = "bounded.budget"

_PRIORITY = {
    "contradictory_constraints": 0,
    "missing_verification_oracle": 1,
    "missing_consequential_authority_or_dependency": 2,
}
_QUESTION_CATEGORY: dict[str, IntakeCategory] = {
    "contradictory_constraints": "constraints_and_tradeoffs",
    "missing_verification_oracle": "completion_signals",
    "missing_consequential_authority_or_dependency": "integrations_and_external_dependencies",
}
def _oracle_name(value: object, claim_id: str) -> OracleName | None:
    if value is None:
        return None
    if value == "automated_test":
        return "automated_test"
    if value == "static_check":
        return "static_check"
    if value == "manual_inspection":
        return "manual_inspection"
    if value == "external_receipt":
        return "external_receipt"
    raise ValueError(f"{claim_id} oracle is not a bounded verification method")


class _Issue(NamedTuple):
    rule_class: str
    deduplication_key: str
    statement: str
    priority: int
    claim_ids: tuple[str, ...]


class NsiBoundedEvaluation(NamedTuple):
    gaps: tuple[Gap, ...]
    conflicts: tuple[Conflict, ...]
    questions: tuple[ClarificationQuestion, ...]
    issue_count: int
    semantic_sha256: str


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _loads(text: str, claim_id: str) -> dict[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{claim_id} is not a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{claim_id} is not a JSON object")
    return payload


def _string_field(payload: dict[str, object], field: str, claim_id: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{claim_id} {field} must be a string")
    return value


def _claim(
    *,
    claim_id: str,
    category: IntakeCategory,
    subject_type: str,
    predicate_id: str,
    value: TypedValue,
    modality: ClaimModality,
    polarity: Polarity,
    truth: TruthValue,
    evidence: list[EvidenceSpan] | None = None,
    premise_ids: list[str] | None = None,
) -> IntakeClaim:
    return IntakeClaim(
        id=claim_id,
        category=category,
        subject=EntityRef(id=claim_id, type=subject_type),
        predicate_id=predicate_id,
        value=value,
        origin="owner_statement",
        state="admitted",
        modality=modality,
        polarity=polarity,
        truth=truth,
        asserted_by=_ACTOR,
        admitted_by=_OWNER,
        evidence=evidence or [],
        premise_ids=premise_ids or [],
    )


def _ontology() -> IntakeOntology:
    def predicate(
        predicate_id: str,
        title: str,
        subject_type: str,
        *,
        cardinality: Literal["one", "many"],
        exclusivity: bool,
    ) -> PredicateDefinition:
        return PredicateDefinition(
            id=predicate_id,
            title=title,
            subject_types=[subject_type],
            object_types=["value"],
            cardinality=cardinality,
            exclusivity=exclusivity,
            transitivity=False,
            authority=["owner"],
        )

    def dimension(
        dimension_id: str,
        title: str,
        category: IntakeCategory,
        subject_type: str,
        predicate_id: str,
    ) -> RequirementDimensionDefinition:
        return RequirementDimensionDefinition(
            id=dimension_id,
            title=title,
            category=category,
            applicability=Applicability(
                entity_types=[subject_type], predicate_ids=[predicate_id]
            ),
            required_premises=[predicate_id],
            materiality="critical",
            waiver_policy="never",
        )

    return IntakeOntology(
        predicates=[
            predicate(_PREDICATE_ARCHETYPE, "Bounded intake archetype", "intake", cardinality="one", exclusivity=True),
            predicate(_PREDICATE_SOURCE, "Bounded source text", "source", cardinality="one", exclusivity=True),
            predicate(_PREDICATE_SPAN, "Bounded source span", "source_span", cardinality="one", exclusivity=True),
            predicate(_PREDICATE_GOAL, "Bounded intake goal", "goal", cardinality="one", exclusivity=True),
            predicate(_PREDICATE_CONSTRAINT, "Bounded intake constraint", "constraint", cardinality="many", exclusivity=False),
            predicate(_PREDICATE_ANNOTATION, "Bounded constraint annotation", "annotation", cardinality="many", exclusivity=False),
            predicate(_PREDICATE_UNKNOWN, "Bounded intake unknown", "unknown", cardinality="one", exclusivity=False),
            predicate(_PREDICATE_ACCEPTANCE, "Bounded acceptance criterion", "acceptance_criterion", cardinality="one", exclusivity=False),
            predicate(_PREDICATE_BUDGET, "Bounded intake budget", "budget", cardinality="one", exclusivity=True),
        ],
        dimensions=[
            dimension(
                "contradictory_constraints",
                "Contradictory constraints",
                "constraints_and_tradeoffs",
                "constraint",
                _PREDICATE_CONSTRAINT,
            ),
            dimension(
                "missing_verification_oracle",
                "Missing verification oracle",
                "completion_signals",
                "acceptance_criterion",
                _PREDICATE_ACCEPTANCE,
            ),
            dimension(
                "missing_consequential_authority_or_dependency",
                "Missing consequential authority or dependency",
                "integrations_and_external_dependencies",
                "unknown",
                _PREDICATE_UNKNOWN,
            ),
        ],
    )


def _reject_reserved(identifier: str, label: str) -> None:
    if identifier.startswith("bounded."):
        raise ValueError(f"{label} collides with the compatibility namespace: {identifier}")


def _typed_constraint_value(value: KnownIntakeValue | UnknownIntakeValue) -> TypedValue:
    if isinstance(value, UnknownIntakeValue):
        return TypedNullValue(type="null", value=None)
    raw = value.value
    if isinstance(raw, bool):
        return TypedBooleanValue(type="boolean", value=raw)
    if isinstance(raw, int):
        return TypedIntegerValue(type="integer", value=raw)
    return TypedStringValue(type="string", value=raw)


def _evidence(draft: BoundedIntakeDraft, span_ids: tuple[str, ...]) -> list[EvidenceSpan]:
    spans = {span.id: span for span in draft.source.spans}
    raw = draft.source.text.encode("utf-8")
    evidence: list[EvidenceSpan] = []
    for span_id in span_ids:
        span = spans[span_id]
        quote = raw[span.start : span.end].decode("utf-8")
        evidence.append(
            EvidenceSpan(
                artifact_id=_SOURCE_ARTIFACT_ID,
                start_byte=span.start,
                end_byte=span.end,
                quote=quote,
            )
        )
    # Byte order, not source order: content hashes then follow span identity.
    evidence.sort(key=lambda item: (item.start_byte, item.end_byte, item.quote or ""))
    return evidence


def _bounded_claims(draft: BoundedIntakeDraft) -> list[IntakeClaim]:
    for claim in (
        draft.goal,
        *draft.constraints,
        *draft.unknowns,
        *draft.acceptance_criteria,
    ):
        _reject_reserved(claim.id, "claim id")
    for span in draft.source.spans:
        _reject_reserved(span.id, "span id")

    claims = [
        _claim(
            claim_id="bounded.archetype",
            category="functional_scope_and_behavior",
            subject_type="intake",
            predicate_id=_PREDICATE_ARCHETYPE,
            value=TypedStringValue(type="string", value=draft.archetype),
            modality="observed",
            polarity="positive",
            truth="true",
        ),
        _claim(
            claim_id="bounded.source",
            category="functional_scope_and_behavior",
            subject_type="source",
            predicate_id=_PREDICATE_SOURCE,
            value=TypedStringValue(type="string", value=draft.source.text),
            modality="observed",
            polarity="positive",
            truth="true",
        ),
    ]
    for span in draft.source.spans:
        claims.append(
            _claim(
                claim_id=f"bounded.span.{span.id}",
                category="scope_boundaries",
                subject_type="source_span",
                predicate_id=_PREDICATE_SPAN,
                value=TypedStringValue(
                    type="string",
                    value=_json_text(
                        {
                            "end": span.end,
                            "exact_text_sha256": span.exact_text_sha256,
                            "id": span.id,
                            "start": span.start,
                        }
                    ),
                ),
                modality="observed",
                polarity="positive",
                truth="true",
                evidence=_evidence(draft, (span.id,)),
            )
        )
    claims.append(
        _claim(
            claim_id=draft.goal.id,
            category="functional_scope_and_behavior",
            subject_type="goal",
            predicate_id=_PREDICATE_GOAL,
            value=TypedStringValue(type="string", value=draft.goal.statement),
            modality="required",
            polarity="positive",
            truth="true",
            evidence=_evidence(draft, draft.goal.source_span_ids),
            premise_ids=list(draft.goal.source_span_ids),
        )
    )
    for constraint in draft.constraints:
        unknown = isinstance(constraint.value, UnknownIntakeValue)
        claims.append(
            _claim(
                claim_id=constraint.id,
                category="constraints_and_tradeoffs",
                subject_type="constraint",
                predicate_id=_PREDICATE_CONSTRAINT,
                value=_typed_constraint_value(constraint.value),
                modality="prohibited" if constraint.polarity == "negative" else "required",
                polarity=constraint.polarity,
                truth="unknown" if unknown else "true",
                evidence=_evidence(draft, constraint.source_span_ids),
                premise_ids=list(constraint.source_span_ids),
            )
        )
        claims.append(
            _claim(
                claim_id=f"bounded.annotation.{constraint.id}",
                category="terminology",
                subject_type="annotation",
                predicate_id=_PREDICATE_ANNOTATION,
                value=TypedStringValue(
                    type="string",
                    value=_json_text(
                        {"key": constraint.key, "statement": constraint.statement}
                    ),
                ),
                modality="observed",
                polarity="positive",
                truth="true",
                premise_ids=[constraint.id],
            )
        )
    for unknown in draft.unknowns:
        claims.append(
            _claim(
                claim_id=unknown.id,
                category=(
                    "integrations_and_external_dependencies"
                    if unknown.kind == "authority_or_dependency"
                    else "placeholders_and_ambiguities"
                ),
                subject_type="unknown",
                predicate_id=_PREDICATE_UNKNOWN,
                value=TypedStringValue(type="string", value=unknown.statement),
                modality="required" if unknown.material else "possible",
                polarity="positive",
                truth="unknown",
                evidence=_evidence(draft, unknown.source_span_ids),
                premise_ids=list(unknown.source_span_ids),
            )
        )
    for criterion in draft.acceptance_criteria:
        claims.append(
            _claim(
                claim_id=criterion.id,
                category="completion_signals",
                subject_type="acceptance_criterion",
                predicate_id=_PREDICATE_ACCEPTANCE,
                value=TypedStringValue(
                    type="string",
                    value=_json_text(
                        {
                            "observable_outcome": criterion.observable_outcome,
                            "oracle": criterion.oracle,
                            "statement": criterion.statement,
                        }
                    ),
                ),
                modality="unknown" if criterion.oracle is None else "required",
                polarity="positive",
                truth="unknown" if criterion.oracle is None else "true",
                evidence=_evidence(draft, criterion.source_span_ids),
                premise_ids=list(criterion.source_span_ids),
            )
        )
    if draft.budget is not None:
        claims.append(
            _claim(
                claim_id="bounded.budget",
                category="constraints_and_tradeoffs",
                subject_type="budget",
                predicate_id=_PREDICATE_BUDGET,
                value=TypedStringValue(
                    type="string",
                    value=_json_text(draft.budget.model_dump(mode="json")),
                ),
                modality="required",
                polarity="positive",
                truth="true",
            )
        )
    ids = [claim.id for claim in claims]
    if len(ids) != len(set(ids)):
        raise ValueError("bounded intake mapping produced duplicate claim ids")
    return claims


def _entities(claims: list[IntakeClaim]) -> list[EntityRef]:
    entities: list[EntityRef] = []
    seen: set[str] = set()
    for claim in claims:
        if claim.subject.id in seen:
            continue
        seen.add(claim.subject.id)
        entities.append(claim.subject)
    return entities


def _state_for(issues: list[_Issue]) -> IntakeRunState:
    if not issues:
        return "ready_for_ratification"
    if any(issue.rule_class == "contradictory_constraints" for issue in issues):
        return "conflicted"
    return "needs_spec"


def _by_predicate(claims: list[IntakeClaim], predicate_id: str) -> list[IntakeClaim]:
    return [claim for claim in claims if claim.predicate_id == predicate_id]


def _require_one(claims: list[IntakeClaim], predicate_id: str) -> IntakeClaim:
    found = _by_predicate(claims, predicate_id)
    if len(found) != 1:
        raise ValueError(f"expected one {predicate_id} claim, found {len(found)}")
    return found[0]


def _annotation(claims: list[IntakeClaim], claim_id: str) -> dict[str, object]:
    annotation_id = f"bounded.annotation.{claim_id}"
    found = [claim for claim in claims if claim.id == annotation_id]
    if len(found) != 1 or found[0].predicate_id != _PREDICATE_ANNOTATION:
        raise ValueError(f"missing constraint annotation for {claim_id}")
    if found[0].value.type != "string":
        raise ValueError(f"{annotation_id} value must be a string")
    payload = _loads(found[0].value.value, annotation_id)
    if set(payload) != {"key", "statement"}:
        raise ValueError(f"{annotation_id} must carry key and statement")
    return payload


def _known_typed(claim: IntakeClaim) -> tuple[str, object] | None:
    if claim.value.type == "null" or claim.truth == "unknown":
        if claim.value.type != "null" or claim.truth != "unknown":
            raise ValueError(f"{claim.id} mixes an unknown constraint value with a known truth")
        return None
    if claim.value.type == "boolean":
        return typed_value(claim.value.value)
    if claim.value.type == "integer":
        return typed_value(claim.value.value)
    if claim.value.type == "string":
        return typed_value(claim.value.value)
    raise ValueError(f"{claim.id} has an unsupported constraint value")


def _issues_from_claims(claims: list[IntakeClaim]) -> list[_Issue]:
    raw: list[_Issue] = []
    grouped: dict[str, list[IntakeClaim]] = {}
    for claim in _by_predicate(claims, _PREDICATE_CONSTRAINT):
        key = _string_field(_annotation(claims, claim.id), "key", claim.id)
        norm_key = " ".join(key.casefold().split())
        grouped.setdefault(norm_key, []).append(claim)

    for norm_key, group in grouped.items():
        known_positives = []
        known_negatives = []
        for claim in group:
            typed = _known_typed(claim)
            if typed is None:
                continue
            if claim.polarity == "positive":
                known_positives.append((claim, typed))
            elif claim.polarity == "negative":
                known_negatives.append((claim, typed))
        pos_typed = {typed for _, typed in known_positives}
        neg_typed = {typed for _, typed in known_negatives}
        conflicting: set[str] = set()
        has_issue = False
        if len(pos_typed) > 1:
            has_issue = True
            conflicting.update(claim.id for claim, _ in known_positives)
        overlap = pos_typed & neg_typed
        if overlap:
            has_issue = True
            conflicting.update(claim.id for claim, typed in known_positives if typed in overlap)
            conflicting.update(claim.id for claim, typed in known_negatives if typed in overlap)
        if has_issue:
            dedup_key = f"constraint:{norm_key}"
            raw.append(
                _Issue(
                    rule_class="contradictory_constraints",
                    deduplication_key=dedup_key,
                    statement=f"contradictory_constraints:{dedup_key}",
                    priority=_PRIORITY["contradictory_constraints"],
                    claim_ids=tuple(sorted(conflicting)),
                )
            )

    for claim in _by_predicate(claims, _PREDICATE_ACCEPTANCE):
        if claim.truth == "unknown":
            dedup_key = f"oracle:{claim.id}"
            raw.append(
                _Issue(
                    rule_class="missing_verification_oracle",
                    deduplication_key=dedup_key,
                    statement=f"missing_verification_oracle:{dedup_key}",
                    priority=_PRIORITY["missing_verification_oracle"],
                    claim_ids=(claim.id,),
                )
            )

    for claim in _by_predicate(claims, _PREDICATE_UNKNOWN):
        if (
            claim.category == "integrations_and_external_dependencies"
            and claim.modality == "required"
        ):
            dedup_key = f"authority:{claim.id}"
            raw.append(
                _Issue(
                    rule_class="missing_consequential_authority_or_dependency",
                    deduplication_key=dedup_key,
                    statement=f"missing_consequential_authority_or_dependency:{dedup_key}",
                    priority=_PRIORITY["missing_consequential_authority_or_dependency"],
                    claim_ids=(claim.id,),
                )
            )

    seen: set[tuple[str, str]] = set()
    deduped: list[_Issue] = []
    for issue in raw:
        identity = (issue.rule_class, issue.deduplication_key)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(issue)
    deduped.sort(key=lambda issue: (issue.priority, issue.deduplication_key))
    return deduped


def _records(issues: list[_Issue]) -> tuple[tuple[Gap, ...], tuple[Conflict, ...], tuple[ClarificationQuestion, ...]]:
    gaps: list[Gap] = []
    conflicts: list[Conflict] = []
    questions: list[ClarificationQuestion] = []
    for issue in issues:
        gap_id = f"gap:{issue.deduplication_key}"
        conflict_ids: list[str] = []
        if issue.rule_class == "contradictory_constraints":
            conflict_id = f"conflict:{issue.deduplication_key}"
            conflicts.append(
                Conflict(
                    id=conflict_id,
                    claim_ids=list(issue.claim_ids),
                    predicate_id=_PREDICATE_CONSTRAINT,
                    reason=issue.statement,
                )
            )
            conflict_ids.append(conflict_id)
        gaps.append(
            Gap(
                id=gap_id,
                dimension_id=issue.rule_class,
                premise_id=issue.claim_ids[0],
                reason=issue.statement,
                materiality="critical",
                waiver_policy="never",
            )
        )
        questions.append(
            ClarificationQuestion(
                id=f"question:{issue.deduplication_key}",
                question=issue.statement,
                gap_ids=[gap_id],
                conflict_ids=conflict_ids,
                category=_QUESTION_CATEGORY[issue.rule_class],
            )
        )
    return tuple(gaps), tuple(conflicts), tuple(questions)


def bounded_to_nsi(draft: BoundedIntakeDraft) -> IntakeSnapshot:
    """Convert a bounded intake draft into an NSI-1 snapshot.

    The snapshot carries the source artifact, every goal, constraint, unknown,
    and acceptance criterion, and the bounded vocabulary. ``intake_contract``
    projects the requirements and acceptance criteria from that snapshot.
    """
    claims = _bounded_claims(draft)
    issues = _issues_from_claims(claims)
    return IntakeSnapshot(
        run_id=f"bounded:{bounded_intake_semantic_sha256(draft)}",
        revision=0,
        state=_state_for(issues),
        artifacts=[
            EvidenceArtifact(
                id=_SOURCE_ARTIFACT_ID,
                uri=_SOURCE_URI,
                content_hash=draft.source.sha256,
                revision="0",
                actor=_ACTOR,
                media_type="text/plain",
            )
        ],
        entities=_entities(claims),
        claims=claims,
        ontology=_ontology(),
    )


def _intake_value(claim: IntakeClaim) -> KnownIntakeValue | UnknownIntakeValue:
    typed = _known_typed(claim)
    if typed is None:
        return UnknownIntakeValue()
    if claim.value.type == "boolean" and isinstance(claim.value.value, bool):
        return KnownIntakeValue(value=claim.value.value)
    if claim.value.type == "integer" and type(claim.value.value) is int:
        return KnownIntakeValue(value=claim.value.value)
    if claim.value.type == "string" and isinstance(claim.value.value, str):
        return KnownIntakeValue(value=claim.value.value)
    raise ValueError(f"{claim.id} constraint value does not roundtrip")


def _span_from_claim(claim: IntakeClaim) -> IntakeSourceSpan:
    if claim.value.type != "string":
        raise ValueError(f"{claim.id} span value must be a string")
    payload = _loads(claim.value.value, claim.id)
    if set(payload) != {"end", "exact_text_sha256", "id", "start"}:
        raise ValueError(f"{claim.id} span record is incomplete")
    span_id = _string_field(payload, "id", claim.id)
    start = payload["start"]
    end = payload["end"]
    digest = _string_field(payload, "exact_text_sha256", claim.id)
    if type(start) is not int or type(end) is not int:
        raise ValueError(f"{claim.id} span bounds must be integers")
    if claim.evidence:
        evidence = claim.evidence[0]
        if evidence.start_byte != start or evidence.end_byte != end:
            raise ValueError(f"{claim.id} evidence does not match the span record")
    return IntakeSourceSpan(id=span_id, start=start, end=end, exact_text_sha256=digest)


def nsi_to_bounded(snapshot: IntakeSnapshot) -> BoundedIntakeDraft:
    """Rebuild the bounded draft from a snapshot produced by ``bounded_to_nsi``."""
    if snapshot.ontology != _ontology():
        raise ValueError("snapshot ontology is not the bounded intake vocabulary")
    if len(snapshot.artifacts) != 1 or snapshot.artifacts[0].id != _SOURCE_ARTIFACT_ID:
        raise ValueError("snapshot is missing the bounded source artifact")
    claims = list(snapshot.claims)
    known = {
        _PREDICATE_ARCHETYPE,
        _PREDICATE_SOURCE,
        _PREDICATE_SPAN,
        _PREDICATE_GOAL,
        _PREDICATE_CONSTRAINT,
        _PREDICATE_ANNOTATION,
        _PREDICATE_UNKNOWN,
        _PREDICATE_ACCEPTANCE,
        _PREDICATE_BUDGET,
    }
    foreign = {claim.predicate_id for claim in claims} - known
    if foreign:
        raise ValueError(f"snapshot contains non-bounded claims: {sorted(foreign)}")

    archetype = _require_one(claims, _PREDICATE_ARCHETYPE)
    source_claim = _require_one(claims, _PREDICATE_SOURCE)
    goal = _require_one(claims, _PREDICATE_GOAL)
    if archetype.value.type != "string" or source_claim.value.type != "string" or goal.value.type != "string":
        raise ValueError("archetype, source, and goal must be strings")
    if archetype.value.value != "small_code_change":
        raise ValueError("archetype is not small_code_change")
    spans = tuple(_span_from_claim(claim) for claim in _by_predicate(claims, _PREDICATE_SPAN))
    constraints: list[IntakeConstraint] = []
    for claim in _by_predicate(claims, _PREDICATE_CONSTRAINT):
        payload = _annotation(claims, claim.id)
        constraints.append(
            IntakeConstraint(
                id=claim.id,
                statement=_string_field(payload, "statement", claim.id),
                source_span_ids=tuple(claim.premise_ids),
                key=_string_field(payload, "key", claim.id),
                value=_intake_value(claim),
                polarity=claim.polarity,
            )
        )
    unknowns: list[IntakeUnknown] = []
    for claim in _by_predicate(claims, _PREDICATE_UNKNOWN):
        if claim.value.type != "string":
            raise ValueError(f"{claim.id} unknown statement must be a string")
        kind: Literal["authority_or_dependency", "routine_choice"]
        if claim.category == "integrations_and_external_dependencies":
            kind = "authority_or_dependency"
        elif claim.category == "placeholders_and_ambiguities":
            kind = "routine_choice"
        else:
            raise ValueError(f"{claim.id} is not a bounded unknown category")
        unknowns.append(
            IntakeUnknown(
                id=claim.id,
                statement=claim.value.value,
                source_span_ids=tuple(claim.premise_ids),
                kind=kind,
                material=claim.modality == "required",
            )
        )
    criteria: list[IntakeAcceptanceCriterion] = []
    for claim in _by_predicate(claims, _PREDICATE_ACCEPTANCE):
        if claim.value.type != "string":
            raise ValueError(f"{claim.id} acceptance record must be a string")
        payload = _loads(claim.value.value, claim.id)
        if set(payload) != {"observable_outcome", "oracle", "statement"}:
            raise ValueError(f"{claim.id} acceptance record is incomplete")
        oracle = _oracle_name(payload["oracle"], claim.id)
        if (oracle is None) != (claim.truth == "unknown"):
            raise ValueError(f"{claim.id} oracle presence does not match claim truth")
        criteria.append(
            IntakeAcceptanceCriterion(
                id=claim.id,
                statement=_string_field(payload, "statement", claim.id),
                source_span_ids=tuple(claim.premise_ids),
                observable_outcome=_string_field(payload, "observable_outcome", claim.id),
                oracle=oracle,
            )
        )
    budgets = _by_predicate(claims, _PREDICATE_BUDGET)
    if len(budgets) > 1:
        raise ValueError("snapshot contains more than one budget")
    budget = None
    if budgets:
        if budgets[0].value.type != "string":
            raise ValueError("budget value must be a string")
        budget = ItemBudget.model_validate(_loads(budgets[0].value.value, budgets[0].id))
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=IntakeSource(
            text=source_claim.value.value,
            sha256=snapshot.artifacts[0].content_hash,
            spans=spans,
        ),
        goal=IntakeGoal(
            id=goal.id,
            statement=goal.value.value,
            source_span_ids=tuple(goal.premise_ids),
        ),
        constraints=tuple(constraints),
        unknowns=tuple(unknowns),
        acceptance_criteria=tuple(criteria),
        budget=budget,
    )


def _requirement_text(text: str, claim_id: str) -> str:
    stripped = text.strip()
    return stripped if stripped else claim_id


def intake_contract(snapshot: IntakeSnapshot) -> IntakeContract:
    """Project requirements and acceptance criteria from a bounded NSI snapshot."""
    requirements: list[RequirementClause] = []
    criteria: list[AcceptanceCriterion] = []
    for claim in snapshot.claims:
        if claim.predicate_id == _PREDICATE_GOAL:
            if claim.value.type != "string":
                raise ValueError(f"{claim.id} goal must be a string")
            requirements.append(
                RequirementClause(
                    id=f"requirement:{claim.id}",
                    category=claim.category,
                    text=_requirement_text(claim.value.value, claim.id),
                    claim_ids=[claim.id],
                    dimension_ids=[],
                )
            )
        elif claim.predicate_id == _PREDICATE_CONSTRAINT:
            payload = _annotation(list(snapshot.claims), claim.id)
            requirements.append(
                RequirementClause(
                    id=f"requirement:{claim.id}",
                    category=claim.category,
                    text=_requirement_text(
                        _string_field(payload, "statement", claim.id), claim.id
                    ),
                    claim_ids=[claim.id],
                    dimension_ids=["contradictory_constraints"],
                )
            )
        elif claim.predicate_id == _PREDICATE_UNKNOWN:
            if claim.value.type != "string":
                raise ValueError(f"{claim.id} unknown statement must be a string")
            dimension_ids = (
                ["missing_consequential_authority_or_dependency"]
                if claim.category == "integrations_and_external_dependencies"
                else []
            )
            requirements.append(
                RequirementClause(
                    id=f"requirement:{claim.id}",
                    category=claim.category,
                    text=_requirement_text(claim.value.value, claim.id),
                    claim_ids=[claim.id],
                    dimension_ids=dimension_ids,
                )
            )
        elif claim.predicate_id == _PREDICATE_ACCEPTANCE:
            if claim.value.type != "string":
                raise ValueError(f"{claim.id} acceptance record must be a string")
            payload = _loads(claim.value.value, claim.id)
            statement = _string_field(payload, "statement", claim.id)
            outcome = _string_field(payload, "observable_outcome", claim.id)
            oracle = payload.get("oracle")
            requirements.append(
                RequirementClause(
                    id=f"requirement:{claim.id}",
                    category=claim.category,
                    text=_requirement_text(statement, claim.id),
                    claim_ids=[claim.id],
                    dimension_ids=["missing_verification_oracle"],
                )
            )
            criteria.append(
                AcceptanceCriterion(
                    id=claim.id,
                    requirement_ids=[f"requirement:{claim.id}"],
                    description=_requirement_text(outcome or statement, claim.id),
                    verification_method=oracle if isinstance(oracle, str) and oracle else "absent",
                    expected_result=TypedStringValue(type="string", value=outcome),
                )
            )
    return IntakeContract(
        requirements=requirements,
        acceptance_criteria=criteria,
        formalizations=[],
        waivers=[],
    )


def nsi_bounded_evaluation(snapshot: IntakeSnapshot) -> NsiBoundedEvaluation:
    """Derive gaps and clarification questions from a bounded NSI snapshot."""
    issues = _issues_from_claims(list(snapshot.claims))
    gaps, conflicts, questions = _records(issues)
    return NsiBoundedEvaluation(
        gaps=gaps,
        conflicts=conflicts,
        questions=questions,
        issue_count=len(issues),
        semantic_sha256=bounded_intake_semantic_sha256(nsi_to_bounded(snapshot)),
    )


def verify_bounded_evaluation_parity(draft: BoundedIntakeDraft) -> NsiBoundedEvaluation:
    """Check NSI gaps and questions against ``evaluate_bounded_intake``.

    The capped blocking questions, their priorities, deduplication keys, claim
    ids, and the bounded semantic hash must match the records derived from the
    NSI snapshot. Gaps keep every issue, including those past the two-question cap.
    """
    questions, issue_count = evaluate_bounded_intake(draft)
    snapshot = bounded_to_nsi(draft)
    evaluation = nsi_bounded_evaluation(snapshot)
    mismatches: list[str] = []
    if evaluation.issue_count != issue_count:
        mismatches.append(
            f"issue count {evaluation.issue_count} != {issue_count}"
        )
    semantic = bounded_intake_semantic_sha256(draft)
    if evaluation.semantic_sha256 != semantic:
        mismatches.append("semantic hash mismatch")
    if snapshot.run_id != f"bounded:{semantic}":
        mismatches.append("snapshot run id is not the bounded semantic hash")
    if snapshot.state != _state_for(list(_issues_from_claims(list(snapshot.claims)))):
        mismatches.append(f"snapshot state {snapshot.state} does not match derived issues")
    if len(evaluation.questions) != len(evaluation.gaps):
        mismatches.append("gap and question counts differ")
    order = [(_PRIORITY[gap.dimension_id], gap.id) for gap in evaluation.gaps]
    if order != sorted(order):
        mismatches.append("gaps are not ordered by priority and deduplication key")
    if len(questions) != min(issue_count, 2):
        mismatches.append("bounded question cap is not 2")
    for index, question in enumerate(questions):
        gap = evaluation.gaps[index]
        record = evaluation.questions[index]
        if question.rule_class != gap.dimension_id:
            mismatches.append(f"{question.deduplication_key} rule class mismatch")
        if question.deduplication_key != gap.id.removeprefix("gap:"):
            mismatches.append(f"{question.deduplication_key} deduplication key mismatch")
        if question.priority != _PRIORITY[gap.dimension_id]:
            mismatches.append(f"{question.deduplication_key} priority mismatch")
        if question.statement != record.question or question.statement != gap.reason:
            mismatches.append(f"{question.deduplication_key} statement mismatch")
        if question.rule_class == "contradictory_constraints":
            conflict_id = f"conflict:{question.deduplication_key}"
            conflict = next(
                (item for item in evaluation.conflicts if item.id == conflict_id), None
            )
            actual_ids = list(conflict.claim_ids) if conflict is not None else []
            if record.conflict_ids != [conflict_id]:
                mismatches.append(f"{question.deduplication_key} conflict link mismatch")
        else:
            actual_ids = [gap.premise_id]
            if record.conflict_ids:
                mismatches.append(f"{question.deduplication_key} unexpected conflict")
        if actual_ids != list(question.claim_ids):
            mismatches.append(
                f"{question.deduplication_key} claim ids {actual_ids} != {list(question.claim_ids)}"
            )
        if record.gap_ids != [gap.id]:
            mismatches.append(f"{question.deduplication_key} gap link mismatch")
        if record.category != _QUESTION_CATEGORY[question.rule_class]:
            mismatches.append(f"{question.deduplication_key} category mismatch")
    if mismatches:
        raise ValueError(
            "bounded intake evaluation does not match NSI-1 gaps and questions: "
            + "; ".join(mismatches)
        )
    return evaluation


__all__ = [
    "NsiBoundedEvaluation",
    "bounded_to_nsi",
    "intake_contract",
    "nsi_bounded_evaluation",
    "nsi_to_bounded",
    "verify_bounded_evaluation_parity",
]
