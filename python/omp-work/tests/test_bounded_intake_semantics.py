from __future__ import annotations

import pytest
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.models import (
    BoundedIntakeDraft,
    IntakeAcceptanceCriterion,
    IntakeBlockingQuestion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    IntakeUnknown,
    KnownIntakeValue,
    UnknownIntakeValue,
)
from omp_work.v1.semantics import (
    BOUNDED_INTAKE_RULE_BUNDLE_SHA256,
    bounded_intake_semantic_sha256,
    evaluate_bounded_intake,
    typed_value,
)


def _make_source(
    text: str = "Test source text for intake.",
    spans: tuple[IntakeSourceSpan, ...] = (),
) -> IntakeSource:
    return IntakeSource(
        text=text,
        sha256=text_sha256(text),
        spans=spans,
    )


def _make_ready_draft() -> BoundedIntakeDraft:
    text = "Optimize cache eviction policy to avoid unbounded memory growth."
    span1_text = "Optimize cache eviction policy"
    span1 = IntakeSourceSpan(
        id="span-1",
        start=0,
        end=len(span1_text.encode("utf-8")),
        exact_text_sha256=text_sha256(span1_text),
    )
    source = _make_source(text, (span1,))
    return BoundedIntakeDraft(
        archetype="small_code_change",
        source=source,
        goal=IntakeGoal(
            id="claim-goal-1",
            statement="Bound memory growth",
            source_span_ids=("span-1",),
        ),
        constraints=(
            IntakeConstraint(
                id="claim-c-1",
                statement="Max latency 5ms",
                source_span_ids=("span-1",),
                key="max_latency_ms",
                value=KnownIntakeValue(value=5),
                polarity="positive",
            ),
        ),
        unknowns=(
            IntakeUnknown(
                id="claim-u-1",
                statement="Choice between LRU and 2Q",
                source_span_ids=(),
                kind="routine_choice",
                material=False,
            ),
        ),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="claim-ac-1",
                statement="RSS remains bounded",
                source_span_ids=(),
                observable_outcome="RSS <= 256MB",
                oracle="automated_test",
            ),
        ),
    )


def test_rule_bundle_sha256() -> None:
    expected = sha256(
        {
            "contract": "work.omp.dev/v1/bounded-intake",
            "rules": (
                "contradictory_constraints/v1",
                "missing_verification_oracle/v1",
                "missing_consequential_authority_or_dependency/v1",
            ),
            "question_limit": 2,
        }
    )
    assert BOUNDED_INTAKE_RULE_BUNDLE_SHA256 == expected
    assert (
        BOUNDED_INTAKE_RULE_BUNDLE_SHA256
        == "92d55ccb8c95c7bd77c166540218a7ebd72792b1f44c07b95152ae49c228102e"
    )


def test_typed_value_distinguishes_bool_int_str() -> None:
    assert typed_value(True) == ("bool", True)
    assert typed_value(False) == ("bool", False)
    assert typed_value(1) == ("int", 1)
    assert typed_value(0) == ("int", 0)
    assert typed_value("1") == ("str", "1")
    assert typed_value("True") == ("str", "True")

    assert typed_value(True) != typed_value(1)
    assert typed_value(False) != typed_value(0)
    assert typed_value(1) != typed_value("1")


def test_ready_draft_produces_no_questions() -> None:
    draft = _make_ready_draft()
    questions, issue_count = evaluate_bounded_intake(draft)
    assert questions == ()
    assert issue_count == 0


def test_routine_choice_never_blocks() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "unknowns": (
                IntakeUnknown(
                    id="claim-u-1",
                    statement="Routine choice non-material",
                    source_span_ids=(),
                    kind="routine_choice",
                    material=False,
                ),
                IntakeUnknown(
                    id="claim-u-2",
                    statement="Routine choice marked material",
                    source_span_ids=(),
                    kind="routine_choice",
                    material=True,
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert questions == ()
    assert issue_count == 0


def test_material_authority_blocks() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "unknowns": (
                IntakeUnknown(
                    id="claim-auth-1",
                    statement="Need infra sign-off",
                    source_span_ids=(),
                    kind="authority_or_dependency",
                    material=True,
                ),
                IntakeUnknown(
                    id="claim-auth-nonmaterial",
                    statement="Optional recommendation",
                    source_span_ids=(),
                    kind="authority_or_dependency",
                    material=False,
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 1
    assert len(questions) == 1
    q = questions[0]
    assert q.rule_class == "missing_consequential_authority_or_dependency"
    assert q.deduplication_key == "authority:claim-auth-1"
    assert (
        q.statement
        == "missing_consequential_authority_or_dependency:authority:claim-auth-1"
    )
    assert q.priority == 2
    assert q.claim_ids == ("claim-auth-1",)


def test_conflicting_positives_block() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="claim-c-1",
                    statement="Max latency 5ms",
                    source_span_ids=(),
                    key="max_latency_ms",
                    value=KnownIntakeValue(value=5),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="claim-c-2",
                    statement="Max latency 10ms",
                    source_span_ids=(),
                    key="MAX_LATENCY_MS",
                    value=KnownIntakeValue(value=10),
                    polarity="positive",
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 1
    assert len(questions) == 1
    q = questions[0]
    assert q.rule_class == "contradictory_constraints"
    assert q.deduplication_key == "constraint:max_latency_ms"
    assert q.statement == "contradictory_constraints:constraint:max_latency_ms"
    assert q.priority == 0
    assert q.claim_ids == ("claim-c-1", "claim-c-2")


def test_positive_and_negative_same_value_block() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="claim-c-pos",
                    statement="Require redis",
                    source_span_ids=(),
                    key="use_redis",
                    value=KnownIntakeValue(value=True),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="claim-c-neg",
                    statement="Forbid redis",
                    source_span_ids=(),
                    key="use_redis",
                    value=KnownIntakeValue(value=True),
                    polarity="negative",
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 1
    assert len(questions) == 1
    q = questions[0]
    assert q.rule_class == "contradictory_constraints"
    assert q.deduplication_key == "constraint:use_redis"
    assert q.statement == "contradictory_constraints:constraint:use_redis"
    assert q.priority == 0
    assert q.claim_ids == ("claim-c-neg", "claim-c-pos")


def test_true_vs_one_typed_equality_blocks() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="claim-c-bool",
                    statement="Flag enabled bool",
                    source_span_ids=(),
                    key="feature_flag",
                    value=KnownIntakeValue(value=True),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="claim-c-int",
                    statement="Flag enabled int",
                    source_span_ids=(),
                    key="feature_flag",
                    value=KnownIntakeValue(value=1),
                    polarity="positive",
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 1
    assert len(questions) == 1
    q = questions[0]
    assert q.rule_class == "contradictory_constraints"
    assert q.deduplication_key == "constraint:feature_flag"
    assert q.priority == 0
    assert q.claim_ids == ("claim-c-bool", "claim-c-int")


def test_missing_oracle_blocks() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "acceptance_criteria": (
                IntakeAcceptanceCriterion(
                    id="claim-ac-none",
                    statement="Needs verification oracle",
                    source_span_ids=(),
                    observable_outcome="Passes check",
                    oracle=None,
                ),
                IntakeAcceptanceCriterion(
                    id="claim-ac-valid",
                    statement="Has automated test",
                    source_span_ids=(),
                    observable_outcome="Passes test",
                    oracle="automated_test",
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 1
    assert len(questions) == 1
    q = questions[0]
    assert q.rule_class == "missing_verification_oracle"
    assert q.deduplication_key == "oracle:claim-ac-none"
    assert q.statement == "missing_verification_oracle:oracle:claim-ac-none"
    assert q.priority == 1
    assert q.claim_ids == ("claim-ac-none",)


def test_unknown_vs_false_does_not_block() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="claim-c-unknown",
                    statement="Unknown redis setting",
                    source_span_ids=(),
                    key="redis_enabled",
                    value=UnknownIntakeValue(),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="claim-c-false",
                    statement="Redis disabled",
                    source_span_ids=(),
                    key="redis_enabled",
                    value=KnownIntakeValue(value=False),
                    polarity="negative",
                ),
            )
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert questions == ()
    assert issue_count == 0


def test_three_issues_ordered_and_capped_to_two() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            # Priority 0 issue
            "constraints": (
                IntakeConstraint(
                    id="c-1",
                    statement="Pos 5",
                    source_span_ids=(),
                    key="timeout_sec",
                    value=KnownIntakeValue(value=5),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-2",
                    statement="Pos 10",
                    source_span_ids=(),
                    key="timeout_sec",
                    value=KnownIntakeValue(value=10),
                    polarity="positive",
                ),
            ),
            # Priority 1 issue
            "acceptance_criteria": (
                IntakeAcceptanceCriterion(
                    id="crit-1",
                    statement="Outcome without oracle",
                    source_span_ids=(),
                    observable_outcome="Outcome",
                    oracle=None,
                ),
            ),
            # Priority 2 issue
            "unknowns": (
                IntakeUnknown(
                    id="unk-1",
                    statement="Material authority",
                    source_span_ids=(),
                    kind="authority_or_dependency",
                    material=True,
                ),
            ),
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 3
    assert len(questions) == 2
    # First: priority 0
    assert questions[0].priority == 0
    assert questions[0].rule_class == "contradictory_constraints"
    assert questions[0].deduplication_key == "constraint:timeout_sec"
    # Second: priority 1
    assert questions[1].priority == 1
    assert questions[1].rule_class == "missing_verification_oracle"
    assert questions[1].deduplication_key == "oracle:crit-1"


def test_questions_deduplication_and_same_priority_key_sorting() -> None:
    base = _make_ready_draft()
    draft = base.model_copy(
        update={
            "constraints": (
                IntakeConstraint(
                    id="c-z1",
                    statement="z positive 1",
                    source_span_ids=(),
                    key="z_param",
                    value=KnownIntakeValue(value=1),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-z2",
                    statement="z positive 2",
                    source_span_ids=(),
                    key="z_param",
                    value=KnownIntakeValue(value=2),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-a1",
                    statement="a positive 1",
                    source_span_ids=(),
                    key="a_param",
                    value=KnownIntakeValue(value=1),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-a2",
                    statement="a positive 2",
                    source_span_ids=(),
                    key="a_param",
                    value=KnownIntakeValue(value=2),
                    polarity="positive",
                ),
            ),
        }
    )
    questions, issue_count = evaluate_bounded_intake(draft)
    assert issue_count == 2
    assert len(questions) == 2
    # Both priority 0, sorted alphabetically by deduplication_key: "constraint:a_param" < "constraint:z_param"
    assert questions[0].deduplication_key == "constraint:a_param"
    assert questions[1].deduplication_key == "constraint:z_param"


def test_reordered_input_identical_hash_and_questions() -> None:
    text = "Alpha beta gamma delta epsilon zeta."
    s1_text = "Alpha"
    s2_text = "beta"
    s1 = IntakeSourceSpan(
        id="s1",
        start=text.index(s1_text),
        end=text.index(s1_text) + len(s1_text.encode("utf-8")),
        exact_text_sha256=text_sha256(s1_text),
    )
    s2 = IntakeSourceSpan(
        id="s2",
        start=text.index(s2_text),
        end=text.index(s2_text) + len(s2_text.encode("utf-8")),
        exact_text_sha256=text_sha256(s2_text),
    )

    c1 = IntakeConstraint(
        id="c1",
        statement="c1 stmt",
        source_span_ids=("s1", "s2"),
        key="alpha",
        value=KnownIntakeValue(value=10),
        polarity="positive",
    )
    c2 = IntakeConstraint(
        id="c2",
        statement="c2 stmt",
        source_span_ids=("s2", "s1"),
        key="alpha",
        value=KnownIntakeValue(value=20),
        polarity="positive",
    )

    u1 = IntakeUnknown(
        id="u1",
        statement="u1 stmt",
        source_span_ids=("s2", "s1"),
        kind="authority_or_dependency",
        material=True,
    )
    u2 = IntakeUnknown(
        id="u2",
        statement="u2 stmt",
        source_span_ids=("s1",),
        kind="routine_choice",
        material=False,
    )

    crit1 = IntakeAcceptanceCriterion(
        id="crit1",
        statement="crit1 stmt",
        source_span_ids=("s1", "s2"),
        observable_outcome="crit1 outcome",
        oracle=None,
    )
    crit2 = IntakeAcceptanceCriterion(
        id="crit2",
        statement="crit2 stmt",
        source_span_ids=(),
        observable_outcome="crit2 outcome",
        oracle="automated_test",
    )

    draft_order1 = BoundedIntakeDraft(
        archetype="small_code_change",
        source=_make_source(text, (s1, s2)),
        goal=IntakeGoal(id="g1", statement="goal stmt", source_span_ids=("s2", "s1")),
        constraints=(c1, c2),
        unknowns=(u1, u2),
        acceptance_criteria=(crit1, crit2),
    )

    draft_order2 = BoundedIntakeDraft(
        archetype="small_code_change",
        source=_make_source(text, (s2, s1)),
        goal=IntakeGoal(id="g1", statement="goal stmt", source_span_ids=("s1", "s2")),
        constraints=(c2, c1),
        unknowns=(u2, u1),
        acceptance_criteria=(crit2, crit1),
    )

    hash1 = bounded_intake_semantic_sha256(draft_order1)
    hash2 = bounded_intake_semantic_sha256(draft_order2)
    assert hash1 == hash2

    q1, count1 = evaluate_bounded_intake(draft_order1)
    q2, count2 = evaluate_bounded_intake(draft_order2)
    assert count1 == count2
    assert q1 == q2
