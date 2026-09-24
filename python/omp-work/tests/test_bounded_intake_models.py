from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import (
    Approval,
    BoundedIntakeDraft,
    EvidenceKind,
    EvidenceReceipt,
    FableAdvicePayload,
    IntakeAcceptanceCriterion,
    IntakeConstraint,
    IntakeGoal,
    IntakeSource,
    IntakeSourceSpan,
    IntakeUnknown,
    KnownIntakeValue,
    UnknownIntakeValue,
)
from pydantic import ValidationError


def _make_source(text: str, spans: tuple[IntakeSourceSpan, ...] = ()) -> IntakeSource:
    return IntakeSource(
        text=text,
        sha256=text_sha256(text),
        spans=spans,
    )


def test_valid_bounded_intake_draft_passes() -> None:
    text = "Optimize cache eviction policy to avoid unbounded memory growth in worker pool."
    raw_bytes = text.encode("utf-8")

    # Span 1: "Optimize cache eviction policy" (0:30)
    s1_text = "Optimize cache eviction policy"
    s1_bytes = s1_text.encode("utf-8")
    s1_start = text.index(s1_text)
    s1_end = s1_start + len(s1_bytes)
    assert raw_bytes[s1_start:s1_end].decode("utf-8") == s1_text

    # Span 2: "unbounded memory growth"
    s2_text = "unbounded memory growth"
    s2_bytes = s2_text.encode("utf-8")
    s2_start = text.index(s2_text)
    s2_end = s2_start + len(s2_bytes)
    assert raw_bytes[s2_start:s2_end].decode("utf-8") == s2_text

    span1 = IntakeSourceSpan(
        id="span-1",
        start=s1_start,
        end=s1_end,
        exact_text_sha256=text_sha256(s1_text),
    )
    span2 = IntakeSourceSpan(
        id="span-2",
        start=s2_start,
        end=s2_end,
        exact_text_sha256=text_sha256(s2_text),
    )

    source = _make_source(text, (span1, span2))

    draft = BoundedIntakeDraft(
        archetype="small_code_change",
        source=source,
        goal=IntakeGoal(
            id="claim-goal-1",
            statement="Bound memory growth in cache",
            source_span_ids=("span-1",),
        ),
        constraints=(
            IntakeConstraint(
                id="claim-constraint-1",
                statement="Must not regress hit latency",
                source_span_ids=("span-1",),
                key="max_latency_ms",
                value=KnownIntakeValue(kind="known", value=5),
                polarity="positive",
            ),
            IntakeConstraint(
                id="claim-constraint-2",
                statement="Do not disable background sweeping",
                source_span_ids=(),
                key="sweep_disabled",
                value=KnownIntakeValue(value=False),
                polarity="negative",
            ),
            IntakeConstraint(
                id="claim-constraint-3",
                statement="Unknown external Redis requirement",
                source_span_ids=("span-2",),
                key="redis_version",
                value=UnknownIntakeValue(),
                polarity="positive",
            ),
        ),
        unknowns=(
            IntakeUnknown(
                id="claim-unknown-1",
                statement="Need infra team sign-off on memory cap",
                source_span_ids=("span-2",),
                kind="authority_or_dependency",
                material=True,
            ),
            IntakeUnknown(
                id="claim-unknown-2",
                statement="Choice between LRU and 2Q eviction",
                source_span_ids=(),
                kind="routine_choice",
                material=False,
            ),
        ),
        acceptance_criteria=(
            IntakeAcceptanceCriterion(
                id="claim-ac-1",
                statement="Heap usage remains under 256MB under synthetic load",
                source_span_ids=("span-2",),
                observable_outcome="RSS <= 256MB after 100k insertions",
                oracle="automated_test",
            ),
            IntakeAcceptanceCriterion(
                id="claim-ac-2",
                statement="Eviction counters increment on overflow",
                source_span_ids=(),
                observable_outcome="prometheus metric evicted_total > 0",
                oracle="static_check",
            ),
            IntakeAcceptanceCriterion(
                id="claim-ac-3",
                statement="Manual code review of lock contention",
                source_span_ids=(),
                observable_outcome="No lock held during hash table resize",
                oracle="manual_inspection",
            ),
            IntakeAcceptanceCriterion(
                id="claim-ac-4",
                statement="External receipt from perf lab benchmark",
                source_span_ids=(),
                observable_outcome="Pass receipt issued by benchmark suite",
                oracle="external_receipt",
            ),
            IntakeAcceptanceCriterion(
                id="claim-ac-5",
                statement="Subjective documentation clarity",
                source_span_ids=(),
                observable_outcome="Documentation reviewed",
                oracle=None,
            ),
        ),
    )

    assert draft.archetype == "small_code_change"
    assert draft.source.text == text
    assert len(draft.constraints) == 3
    assert len(draft.unknowns) == 2
    assert len(draft.acceptance_criteria) == 5


def test_duplicate_claim_id_rejected() -> None:
    source = _make_source("A simple task description")
    goal = IntakeGoal(id="claim-shared-1", statement="Goal statement")

    # Conflict between goal and constraint
    with pytest.raises(ValidationError, match="duplicate claim id: claim-shared-1"):
        BoundedIntakeDraft(
            source=source,
            goal=goal,
            constraints=(
                IntakeConstraint(
                    id="claim-shared-1",
                    statement="Constraint sharing goal id",
                    key="k",
                    value=KnownIntakeValue(value=True),
                    polarity="positive",
                ),
            ),
        )

    # Conflict between two constraints
    with pytest.raises(ValidationError, match="duplicate claim id: c-dup"):
        BoundedIntakeDraft(
            source=source,
            goal=IntakeGoal(id="g1", statement="Goal"),
            constraints=(
                IntakeConstraint(
                    id="c-dup",
                    statement="C1",
                    key="k1",
                    value=KnownIntakeValue(value=1),
                    polarity="positive",
                ),
                IntakeConstraint(
                    id="c-dup",
                    statement="C2",
                    key="k2",
                    value=KnownIntakeValue(value=2),
                    polarity="positive",
                ),
            ),
        )

    # Conflict between unknown and acceptance criterion
    with pytest.raises(ValidationError, match="duplicate claim id: conflict-id"):
        BoundedIntakeDraft(
            source=source,
            goal=IntakeGoal(id="g1", statement="Goal"),
            unknowns=(
                IntakeUnknown(
                    id="conflict-id",
                    statement="Unknown item",
                    kind="routine_choice",
                    material=False,
                ),
            ),
            acceptance_criteria=(
                IntakeAcceptanceCriterion(
                    id="conflict-id",
                    statement="AC item",
                    observable_outcome="outcome",
                    oracle="automated_test",
                ),
            ),
        )


def test_unknown_span_ref_rejected() -> None:
    text = "hello world"
    span1 = IntakeSourceSpan(
        id="span-exists",
        start=0,
        end=5,
        exact_text_sha256=text_sha256("hello"),
    )
    source = _make_source(text, (span1,))

    # Goal references unknown span
    with pytest.raises(ValidationError, match="unknown span ref: span-nonexistent"):
        BoundedIntakeDraft(
            source=source,
            goal=IntakeGoal(
                id="g1",
                statement="Goal",
                source_span_ids=("span-nonexistent",),
            ),
        )

    # Constraint references unknown span
    with pytest.raises(ValidationError, match="unknown span ref: span-ghost"):
        BoundedIntakeDraft(
            source=source,
            goal=IntakeGoal(
                id="g1", statement="Goal", source_span_ids=("span-exists",)
            ),
            constraints=(
                IntakeConstraint(
                    id="c1",
                    statement="Constraint",
                    source_span_ids=("span-ghost",),
                    key="k",
                    value=KnownIntakeValue(value="v"),
                    polarity="positive",
                ),
            ),
        )

    # Acceptance criterion references unknown span
    with pytest.raises(ValidationError, match="unknown span ref: span-missing"):
        BoundedIntakeDraft(
            source=source,
            goal=IntakeGoal(id="g1", statement="Goal", source_span_ids=()),
            acceptance_criteria=(
                IntakeAcceptanceCriterion(
                    id="ac1",
                    statement="AC",
                    source_span_ids=("span-missing",),
                    observable_outcome="pass",
                    oracle=None,
                ),
            ),
        )


def test_tampered_text_rejected() -> None:
    text = "legitimate task text"
    good_hash = text_sha256(text)
    # text does not match sha256
    with pytest.raises(ValidationError, match="text sha256 mismatch"):
        IntakeSource(
            text="tampered text",
            sha256=good_hash,
            spans=(),
        )


def test_tampered_span_hash_rejected() -> None:
    text = "some text to be indexed by spans"
    span = IntakeSourceSpan(
        id="s1",
        start=5,
        end=9,
        exact_text_sha256="0" * 64,  # tampered hash
    )
    with pytest.raises(ValidationError, match="exact_text_sha256 mismatch"):
        IntakeSource(
            text=text,
            sha256=text_sha256(text),
            spans=(span,),
        )


def test_mid_codepoint_span_rejected_and_aligned_accepted() -> None:
    # "Emoji: 🚀, Japanese: こんにちは"
    # '🚀' is 4 bytes: 0xf0 0x9f 0x9a 0x80
    # 'こ' is 3 bytes: 0xe3 0x81 0x93
    text = "Emoji: 🚀 end"
    raw_bytes = text.encode("utf-8")
    emoji_start = text.index(
        "🚀"
    )  # character index 7, byte index 7 (since 'Emoji: ' is 7 ASCII bytes)
    assert emoji_start == 7
    assert raw_bytes[7:11] == "🚀".encode()
    assert len("🚀".encode()) == 4

    # 1. Aligned span covering exactly 🚀 (byte offset 7 to 11) -> accepted!
    aligned_span = IntakeSourceSpan(
        id="span-aligned",
        start=7,
        end=11,
        exact_text_sha256=text_sha256("🚀"),
    )
    src_aligned = IntakeSource(
        text=text,
        sha256=text_sha256(text),
        spans=(aligned_span,),
    )
    assert len(src_aligned.spans) == 1

    # 2. Mid-codepoint span slicing into the first 2 bytes of 🚀 (7 to 9) -> rejected!
    mid_span_start = IntakeSourceSpan(
        id="span-mid-1",
        start=7,
        end=9,
        exact_text_sha256=text_sha256("dummy"),
    )
    with pytest.raises(ValidationError, match="does not decode as valid UTF-8"):
        IntakeSource(
            text=text,
            sha256=text_sha256(text),
            spans=(mid_span_start,),
        )

    # 3. Mid-codepoint span slicing the tail of 🚀 (8 to 11) -> rejected!
    mid_span_end = IntakeSourceSpan(
        id="span-mid-2",
        start=8,
        end=11,
        exact_text_sha256=text_sha256("dummy"),
    )
    with pytest.raises(ValidationError, match="does not decode as valid UTF-8"):
        IntakeSource(
            text=text,
            sha256=text_sha256(text),
            spans=(mid_span_end,),
        )


def test_span_index_constraints() -> None:
    # end <= start rejected
    with pytest.raises(ValidationError, match="end must be greater than start"):
        IntakeSourceSpan(id="s1", start=5, end=5, exact_text_sha256="a" * 64)

    with pytest.raises(ValidationError, match="end must be greater than start"):
        IntakeSourceSpan(id="s1", start=5, end=4, exact_text_sha256="a" * 64)

    # start < 0 rejected
    with pytest.raises(ValidationError):
        IntakeSourceSpan(id="s1", start=-1, end=5, exact_text_sha256="a" * 64)

    # span out of text bounds rejected
    text = "short"
    span_oob = IntakeSourceSpan(
        id="s1", start=0, end=100, exact_text_sha256=text_sha256("x")
    )
    with pytest.raises(ValidationError, match="out of bounds"):
        IntakeSource(
            text=text,
            sha256=text_sha256(text),
            spans=(span_oob,),
        )


def test_duplicate_span_id_rejected() -> None:
    text = "duplicate span ids in source"
    span1 = IntakeSourceSpan(
        id="s-dup", start=0, end=4, exact_text_sha256=text_sha256("dupl")
    )
    span2 = IntakeSourceSpan(
        id="s-dup", start=5, end=9, exact_text_sha256=text_sha256("cate")
    )
    with pytest.raises(ValidationError, match="duplicate span id: s-dup"):
        IntakeSource(
            text=text,
            sha256=text_sha256(text),
            spans=(span1, span2),
        )


def test_unknown_not_equal_known_false() -> None:
    unk = UnknownIntakeValue()
    known_false = KnownIntakeValue(value=False)
    known_true = KnownIntakeValue(value=True)
    known_zero = KnownIntakeValue(value=0)
    known_empty_str = KnownIntakeValue(value="")

    # Equality checks
    assert unk != known_false
    assert known_false != unk
    assert unk != known_zero
    assert unk != known_empty_str
    assert unk != known_true

    # Distinct kinds
    assert unk.kind == "unknown"
    assert known_false.kind == "known"

    # Distinct values and types preserved
    assert known_false.value is False
    assert known_true.value is True
    assert known_zero.value == 0
    assert known_zero.value is not False

    # Within constraint
    c_unk = IntakeConstraint(
        id="c1",
        statement="Unknown constraint",
        key="flag",
        value=UnknownIntakeValue(),
        polarity="positive",
    )
    c_false = IntakeConstraint(
        id="c2",
        statement="Known false constraint",
        key="flag",
        value=KnownIntakeValue(value=False),
        polarity="positive",
    )
    assert c_unk.value != c_false.value


def test_fable_advice_payload() -> None:
    payload = FableAdvicePayload(
        advisor_model_family="fable",
        advice_sha256="1" * 64,
        disposition="considered",
        intake_semantic_sha256="2" * 64,
        rule_bundle_sha256="3" * 64,
    )
    assert payload.advisor_model_family == "fable"
    assert payload.disposition == "considered"
    assert payload.advice_sha256 == "1" * 64
    assert payload.intake_semantic_sha256 == "2" * 64
    assert payload.rule_bundle_sha256 == "3" * 64

    # Default fields
    default_payload = FableAdvicePayload(
        advice_sha256="a" * 64,
        intake_semantic_sha256="b" * 64,
        rule_bundle_sha256="c" * 64,
    )
    assert default_payload.advisor_model_family == "fable"
    assert default_payload.disposition == "considered"

    # Non-hex64 sha rejected
    with pytest.raises(ValidationError):
        FableAdvicePayload(
            advice_sha256="short",
            intake_semantic_sha256="b" * 64,
            rule_bundle_sha256="c" * 64,
        )

    # Wrong advisor_model_family rejected
    with pytest.raises(ValidationError):
        FableAdvicePayload(  # type: ignore[call-arg]
            advisor_model_family="other",
            advice_sha256="a" * 64,
            intake_semantic_sha256="b" * 64,
            rule_bundle_sha256="c" * 64,
        )


def test_evidence_kind_and_approval_omp_266() -> None:
    assert EvidenceKind.INTAKE_PUBLICATION == "intake_publication"
    assert EvidenceKind.INTAKE_ADMISSION == "intake_admission"

    receipt_pub = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.INTAKE_PUBLICATION,
        payload={"draft_id": "test"},
        payload_sha256="0" * 64,
        issuer="owner",
        issued_at=datetime.now(UTC),
    )
    assert receipt_pub.kind is EvidenceKind.INTAKE_PUBLICATION

    receipt_adm = EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=EvidenceKind.INTAKE_ADMISSION,
        payload={"admission": True},
        payload_sha256="0" * 64,
        issuer="owner",
        issued_at=datetime.now(UTC),
    )
    assert receipt_adm.kind is EvidenceKind.INTAKE_ADMISSION

    approval = Approval(
        contract_version="work.omp.dev/v1",
        contract_sha256="f" * 64,
        approved_by="owner",
        approved_at=datetime.now(UTC),
        issue="OMP-266",
    )
    assert approval.issue == "OMP-266"
