"""OMP-521-s04: bounded intake drafts map onto NSI-1 without changing evaluation.

Failure mode defended: the compatibility adapter drops a claim, accepts a
snapshot or contract that fails NSI schema validation, or reports a different
blocking question, priority, deduplication key, or semantic hash than
``evaluate_bounded_intake`` on the golden corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omp_work.contracts.nsi.v1.compat import (
    bounded_to_nsi,
    intake_contract,
    nsi_to_bounded,
    verify_bounded_evaluation_parity,
)
from omp_work.contracts.nsi.v1.models import (
    Conflict,
    Gap,
    IntakeClaim,
    IntakeSnapshot,
)
from omp_work.contracts.nsi.v1.validate import content_hash, validate_instance
from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeBlockingQuestion,
    IntakeConstraint,
    IntakeUnknown,
    KnownIntakeValue,
    UnknownIntakeValue,
)
from omp_work.v1.semantics import (
    bounded_intake_semantic_sha256,
    evaluate_bounded_intake,
)
from pydantic import ValidationError

_GOLDEN = Path(__file__).parent / "fixtures" / "bounded_intake_golden.json"
_COMPAT = Path(__file__).parent / "fixtures" / "nsi_v1_bounded_compat.json"
_PRIORITY = {
    "contradictory_constraints": 0,
    "missing_verification_oracle": 1,
    "missing_consequential_authority_or_dependency": 2,
}


def _golden() -> list[dict]:
    data = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise AssertionError("golden corpus must be a JSON array")
    return data


def _valid_drafts() -> list[tuple[str, str, dict, BoundedIntakeDraft]]:
    rows: list[tuple[str, str, dict, BoundedIntakeDraft]] = []
    for fixture in _golden():
        for role in ("draft", "publish_draft"):
            raw = fixture["input"].get(role)
            if raw is None:
                continue
            try:
                draft = BoundedIntakeDraft.model_validate(raw)
            except ValidationError:
                continue
            rows.append((fixture["id"], role, fixture, draft))
    return rows


def _draft(golden_id: str, role: str = "draft") -> BoundedIntakeDraft:
    for fixture_id, fixture_role, _fixture, draft in _valid_drafts():
        if fixture_id == golden_id and fixture_role == role:
            return draft
    raise AssertionError(f"missing valid draft {golden_id}:{role}")


def _question_view(question: IntakeBlockingQuestion) -> dict[str, object]:
    return {
        "rule_class": question.rule_class,
        "deduplication_key": question.deduplication_key,
        "statement": question.statement,
        "priority": question.priority,
        "claim_ids": list(question.claim_ids),
    }


def _fixture_document() -> dict[str, object]:
    snapshots = []
    for golden_id, role, _fixture, draft in _valid_drafts():
        snapshot = bounded_to_nsi(draft)
        evaluation = verify_bounded_evaluation_parity(draft)
        contract = intake_contract(snapshot)
        snapshots.append(
            {
                "conflicts": [item.model_dump(mode="json") for item in evaluation.conflicts],
                "contract": contract.model_dump(mode="json"),
                "gaps": [gap.model_dump(mode="json") for gap in evaluation.gaps],
                "golden_id": golden_id,
                "issue_count": evaluation.issue_count,
                "questions": [item.model_dump(mode="json") for item in evaluation.questions],
                "role": role,
                "semantic_sha256": evaluation.semantic_sha256,
                "snapshot": snapshot.model_dump(mode="json"),
                "state": snapshot.state,
            }
        )
    return {
        "contract": "https://omp.dev/contracts/nsi/v1/bounded-compat",
        "snapshots": snapshots,
        "source": "python/omp-work/tests/fixtures/bounded_intake_golden.json",
    }


def _fixture_text() -> str:
    return json.dumps(_fixture_document(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _assert_schema(record: object) -> None:
    schema_name = getattr(record, "schema_name", None)
    assert isinstance(schema_name, str)
    validate_instance(schema_name, record)


def _claim(snapshot: IntakeSnapshot, claim_id: str) -> IntakeClaim:
    found = [claim for claim in snapshot.claims if claim.id == claim_id]
    assert len(found) == 1
    return found[0]


def _assert_preserved(draft: BoundedIntakeDraft, snapshot: IntakeSnapshot) -> None:
    goal = _claim(snapshot, draft.goal.id)
    assert goal.predicate_id == "bounded.goal"
    assert goal.value.type == "string"
    assert goal.value.value == draft.goal.statement
    assert tuple(goal.premise_ids) == draft.goal.source_span_ids

    for constraint in draft.constraints:
        claim = _claim(snapshot, constraint.id)
        assert claim.predicate_id == "bounded.constraint"
        assert claim.category == "constraints_and_tradeoffs"
        assert claim.polarity == constraint.polarity
        assert tuple(claim.premise_ids) == constraint.source_span_ids
        if isinstance(constraint.value, UnknownIntakeValue):
            assert claim.value.type == "null"
            assert claim.truth == "unknown"
        else:
            assert claim.truth == "true"
            assert claim.value.value == constraint.value.value
            assert type(claim.value.value) is type(constraint.value.value)
        annotation = _claim(snapshot, f"bounded.annotation.{constraint.id}")
        assert json.loads(annotation.value.value) == {
            "key": constraint.key,
            "statement": constraint.statement,
        }

    for unknown in draft.unknowns:
        claim = _claim(snapshot, unknown.id)
        assert claim.predicate_id == "bounded.unknown"
        assert claim.value.type == "string"
        assert claim.value.value == unknown.statement
        assert claim.truth == "unknown"
        assert tuple(claim.premise_ids) == unknown.source_span_ids
        if unknown.kind == "authority_or_dependency":
            assert claim.category == "integrations_and_external_dependencies"
        else:
            assert claim.category == "placeholders_and_ambiguities"
        assert (claim.modality == "required") is unknown.material

    for criterion in draft.acceptance_criteria:
        claim = _claim(snapshot, criterion.id)
        assert claim.predicate_id == "bounded.acceptance_criterion"
        assert claim.category == "completion_signals"
        assert json.loads(claim.value.value) == {
            "observable_outcome": criterion.observable_outcome,
            "oracle": criterion.oracle,
            "statement": criterion.statement,
        }
        assert tuple(claim.premise_ids) == criterion.source_span_ids
        assert (claim.truth == "unknown") is (criterion.oracle is None)

    for span in draft.source.spans:
        payload = json.loads(_claim(snapshot, f"bounded.span.{span.id}").value.value)
        assert payload == {
            "end": span.end,
            "exact_text_sha256": span.exact_text_sha256,
            "id": span.id,
            "start": span.start,
        }

    source = _claim(snapshot, "bounded.source")
    assert source.value.type == "string"
    assert source.value.value == draft.source.text
    assert snapshot.artifacts[0].content_hash == draft.source.sha256
    if draft.budget is None:
        assert all(claim.id != "bounded.budget" for claim in snapshot.claims)
    else:
        assert json.loads(_claim(snapshot, "bounded.budget").value.value) == draft.budget.model_dump(
            mode="json"
        )


_CASES = _valid_drafts()


@pytest.mark.parametrize(
    "golden_id,role,fixture,draft",
    _CASES,
    ids=[f"{golden_id}:{role}" for golden_id, role, _fixture, _draft in _CASES],
)
def test_golden_draft_maps_validates_and_matches_bounded_evaluation(
    golden_id: str, role: str, fixture: dict, draft: BoundedIntakeDraft
) -> None:
    del golden_id, role
    snapshot = bounded_to_nsi(draft)
    assert nsi_to_bounded(snapshot) == draft
    _assert_preserved(draft, snapshot)

    for artifact in snapshot.artifacts:
        _assert_schema(artifact)
    for predicate in snapshot.ontology.predicates:
        _assert_schema(predicate)
    for dimension in snapshot.ontology.dimensions:
        _assert_schema(dimension)
    for claim in snapshot.claims:
        _assert_schema(claim)

    contract = intake_contract(snapshot)
    _assert_schema(contract)
    assert [clause.claim_ids for clause in contract.requirements] == [
        [claim_id]
        for claim_id in (
            draft.goal.id,
            *(item.id for item in draft.constraints),
            *(item.id for item in draft.unknowns),
            *(item.id for item in draft.acceptance_criteria),
        )
    ]
    assert [clause.text for clause in contract.requirements] == [
        item.statement
        for item in (
            draft.goal,
            *draft.constraints,
            *draft.unknowns,
            *draft.acceptance_criteria,
        )
    ]
    assert [item.id for item in contract.acceptance_criteria] == [
        item.id for item in draft.acceptance_criteria
    ]
    assert [item.verification_method for item in contract.acceptance_criteria] == [
        item.oracle or "absent" for item in draft.acceptance_criteria
    ]
    assert contract.formalizations == []
    assert contract.waivers == []

    questions, issue_count = evaluate_bounded_intake(draft)
    evaluation = verify_bounded_evaluation_parity(draft)
    assert evaluation.issue_count == issue_count
    assert len(evaluation.gaps) == issue_count
    assert evaluation.semantic_sha256 == bounded_intake_semantic_sha256(draft)
    assert len(questions) == min(issue_count, 2)
    for question, gap, record in zip(questions, evaluation.gaps, evaluation.questions):
        assert gap.dimension_id == question.rule_class
        assert gap.id == f"gap:{question.deduplication_key}"
        assert _PRIORITY[gap.dimension_id] == question.priority
        assert record.question == question.statement
        assert gap.reason == question.statement
        assert record.id == f"question:{question.deduplication_key}"
        _assert_schema(gap)
        _assert_schema(record)
        if question.rule_class == "contradictory_constraints":
            conflict = next(
                item
                for item in evaluation.conflicts
                if item.id == f"conflict:{question.deduplication_key}"
            )
            _assert_schema(conflict)
            assert conflict.claim_ids == list(question.claim_ids)
        else:
            assert gap.premise_id == question.claim_ids[0]
            assert record.conflict_ids == []
    for conflict in evaluation.conflicts:
        _assert_schema(conflict)

    assert content_hash(snapshot) == content_hash(bounded_to_nsi(nsi_to_bounded(snapshot)))
    if fixture["level"] == "unit":
        expected = fixture["expected"]
        assert issue_count == expected["issue_count"]
        assert [_question_view(question) for question in questions] == expected["questions"]
        assert (issue_count == 0) is expected["ready"]
        assert (snapshot.state == "ready_for_ratification") is expected["ready"]


def test_reordered_corpus_keeps_one_semantic_hash_and_one_nsi_content_hash() -> None:
    ready = _draft("01-valid-ready")
    reordered = _draft("04-reordered-ready")
    assert ready != reordered
    assert bounded_intake_semantic_sha256(ready) == bounded_intake_semantic_sha256(reordered)
    assert evaluate_bounded_intake(ready) == evaluate_bounded_intake(reordered)
    left = bounded_to_nsi(ready)
    right = bounded_to_nsi(reordered)
    assert left.claims != right.claims
    assert content_hash(left) == content_hash(right)
    assert verify_bounded_evaluation_parity(ready).gaps == verify_bounded_evaluation_parity(reordered).gaps


@pytest.mark.parametrize(
    "golden_id",
    ["08-altered-source", "10-pre-edit-assessment"],
)
def test_edited_golden_draft_changes_bounded_and_nsi_hashes(golden_id: str) -> None:
    original = _draft(golden_id, "draft")
    edited = _draft(golden_id, "publish_draft")
    assert bounded_intake_semantic_sha256(original) != bounded_intake_semantic_sha256(edited)
    assert content_hash(bounded_to_nsi(original)) != content_hash(bounded_to_nsi(edited))
    verify_bounded_evaluation_parity(original)
    verify_bounded_evaluation_parity(edited)


def test_true_and_one_stay_distinct_typed_values() -> None:
    draft = _draft("05-true-versus-one")
    snapshot = bounded_to_nsi(draft)
    values = {
        claim.id: claim.value
        for claim in snapshot.claims
        if claim.predicate_id == "bounded.constraint"
    }
    assert values["c-bool"].type == "boolean"
    assert values["c-bool"].value is True
    assert values["c-int"].type == "integer"
    assert values["c-int"].value == 1
    evaluation = verify_bounded_evaluation_parity(draft)
    assert evaluation.issue_count == 1
    assert evaluation.gaps[0].dimension_id == "contradictory_constraints"
    assert evaluation.conflicts[0].claim_ids == ["c-bool", "c-int"]


def test_unknown_constraint_is_not_a_false_conflict() -> None:
    draft = _draft("06-unknown-versus-false")
    snapshot = bounded_to_nsi(draft)
    unknown = _claim(snapshot, "c-unknown")
    known_false = _claim(snapshot, "c-false")
    assert unknown.value.type == "null"
    assert unknown.truth == "unknown"
    assert known_false.value.type == "boolean"
    assert known_false.value.value is False
    assert known_false.truth == "true"
    evaluation = verify_bounded_evaluation_parity(draft)
    assert evaluation.gaps == ()
    assert snapshot.state == "ready_for_ratification"


def test_missing_oracle_is_unknown_and_blocks() -> None:
    draft = _draft("07-missing-oracle")
    snapshot = bounded_to_nsi(draft)
    claim = _claim(snapshot, "ac-none")
    assert claim.truth == "unknown"
    contract = intake_contract(snapshot)
    assert contract.acceptance_criteria[0].verification_method == "absent"
    evaluation = verify_bounded_evaluation_parity(draft)
    assert [gap.dimension_id for gap in evaluation.gaps] == ["missing_verification_oracle"]
    assert evaluation.gaps[0].id == "gap:oracle:ac-none"
    assert snapshot.state == "needs_spec"


def test_negative_polarity_casefold_and_question_cap_match() -> None:
    ready = _draft("01-valid-ready")
    opposed = ready.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="c-pos",
                    statement="Require the flag",
                    source_span_ids=(),
                    key="Use Flag",
                    value=KnownIntakeValue(value=True),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-neg",
                    statement="Forbid the flag",
                    source_span_ids=(),
                    key="use   flag",
                    value=KnownIntakeValue(value=True),
                    polarity="negative",
                ),
            )
        }
    )
    evaluation = verify_bounded_evaluation_parity(opposed)
    assert evaluation.issue_count == 1
    assert evaluation.gaps[0].id == "gap:constraint:use flag"
    assert evaluation.conflicts[0].claim_ids == ["c-neg", "c-pos"]
    assert bounded_to_nsi(opposed).state == "conflicted"

    capped = ready.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="c-1",
                    statement="Five seconds",
                    source_span_ids=(),
                    key="timeout_sec",
                    value=KnownIntakeValue(value=5),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-2",
                    statement="Ten seconds",
                    source_span_ids=(),
                    key="timeout_sec",
                    value=KnownIntakeValue(value=10),
                    polarity="positive",
                ),
            ),
            "acceptance_criteria": (
                IntakeAcceptanceCriterion(
                    id="crit-1",
                    statement="Outcome without oracle",
                    source_span_ids=(),
                    observable_outcome="Outcome",
                    oracle=None,
                ),
            ),
            "unknowns": (
                IntakeUnknown(
                    id="unk-1",
                    statement="Material authority",
                    source_span_ids=(),
                    kind="authority_or_dependency",
                    material=True,
                ),
                IntakeUnknown(
                    id="choice-1",
                    statement="Routine and material",
                    source_span_ids=(),
                    kind="routine_choice",
                    material=True,
                ),
            ),
        }
    )
    questions, issue_count = evaluate_bounded_intake(capped)
    evaluation = verify_bounded_evaluation_parity(capped)
    assert issue_count == 3
    assert len(questions) == 2
    assert [question.deduplication_key for question in questions] == [
        "constraint:timeout_sec",
        "oracle:crit-1",
    ]
    assert [gap.id for gap in evaluation.gaps] == [
        "gap:constraint:timeout_sec",
        "gap:oracle:crit-1",
        "gap:authority:unk-1",
    ]
    assert [gap.dimension_id for gap in evaluation.gaps] == [
        "contradictory_constraints",
        "missing_verification_oracle",
        "missing_consequential_authority_or_dependency",
    ]
    assert all(gap.premise_id != "choice-1" for gap in evaluation.gaps)
    assert nsi_to_bounded(bounded_to_nsi(capped)) == capped


def test_tampered_span_is_rejected_and_left_unmapped() -> None:
    fixture = next(item for item in _golden() if item["id"] == "09-tampered-span")
    with pytest.raises(ValidationError, match="exact_text_sha256 mismatch"):
        BoundedIntakeDraft.model_validate(fixture["input"]["draft"])
    document = json.loads(_COMPAT.read_text(encoding="utf-8"))
    assert all(row["golden_id"] != "09-tampered-span" for row in document["snapshots"])


def test_compat_fixture_covers_every_valid_golden_draft() -> None:
    skipped: list[tuple[str, str]] = []
    for fixture in _golden():
        for role in ("draft", "publish_draft"):
            if role not in fixture["input"]:
                continue
            try:
                BoundedIntakeDraft.model_validate(fixture["input"][role])
            except ValidationError:
                skipped.append((fixture["id"], role))
    assert skipped == [("09-tampered-span", "draft")]
    document = json.loads(_COMPAT.read_text(encoding="utf-8"))
    assert [
        (row["golden_id"], row["role"]) for row in document["snapshots"]
    ] == [(golden_id, role) for golden_id, role, _fixture, _draft in _CASES]
    for row in document["snapshots"]:
        snapshot = IntakeSnapshot.model_validate(row["snapshot"])
        assert snapshot == bounded_to_nsi(_draft(row["golden_id"], row["role"]))
        for gap in row["gaps"]:
            Gap.model_validate(gap)
        for conflict in row["conflicts"]:
            Conflict.model_validate(conflict)
        for question in row["questions"]:
            validate_instance("status#/$defs/ClarificationQuestion", question)
        validate_instance("contract", row["contract"])


def test_committed_compat_fixture_matches_the_mapping() -> None:
    assert _COMPAT.read_text(encoding="utf-8") == _fixture_text()
