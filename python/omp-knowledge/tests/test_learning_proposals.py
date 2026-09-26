from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import EvidenceKind, EvidenceReceipt

from omp_knowledge.learning.models import (
    Attribution,
    Claim,
    Lesson,
    Precondition,
    SourceIdentity,
)
from omp_knowledge.learning.policy import (
    REASON_INVALID_CITATION,
    REASON_NARRATION_ONLY,
    REASON_NO_CITATION,
    REASON_TOOL_SUCCESS_ONLY,
    ClaimEvaluation,
    NativeReceipts,
    PolicyDecision,
    evaluate,
)
from omp_knowledge.learning.proposals import (
    ProposalRecord,
    compute_lesson_fingerprint,
    create_proposal,
)
from omp_knowledge.learning.store import LearningStore


class DictReceiptReader:
    """Dict-backed fake reader conforming to NativeReceipts protocol."""

    def __init__(self, receipts: dict[str | UUID, EvidenceReceipt] | None = None) -> None:
        self._receipts: dict[str, EvidenceReceipt] = {}
        if receipts:
            for k, v in receipts.items():
                self._receipts[str(k)] = v

    def add(self, receipt: EvidenceReceipt) -> None:
        self._receipts[str(receipt.receipt_id)] = receipt

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        key = str(receipt_id)
        if key not in self._receipts:
            raise KeyError(f"Receipt {receipt_id} not found in reader")
        return self._receipts[key]


def make_receipt(
    *,
    receipt_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
    payload: dict[str, Any] | None = None,
) -> EvidenceReceipt:
    rid = receipt_id or uuid4()
    p = payload if payload is not None else {"exit_code": 0}
    return EvidenceReceipt(
        receipt_id=rid,
        work_id=uuid4(),
        revision_id=uuid4(),
        candidate_id=uuid4(),
        kind=kind,
        payload=p,
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        issuer="test-runner",
        issued_at=datetime.now(timezone.utc),
        verdict=verdict,
        independent=True,
    )


def make_attribution(
    model: str = "qwen-2.5",
    profile: str = "local-qwen",
) -> Attribution:
    return Attribution(
        model=model,
        profile=profile,
        source=SourceIdentity(
            workspace_id=str(uuid4()),
            event_id=str(uuid4()),
            event_sequence=1,
            aggregate_id=str(uuid4()),
        ),
    )


def test_native_receipts_protocol_conformance() -> None:
    reader = DictReceiptReader()
    assert isinstance(reader, NativeReceipts)

    from omp_work.v1.client import WorkClient
    assert issubclass(WorkClient, NativeReceipts)


def test_valid_pass_plus_unknown_id_rejected_invalid_citation_no_procedure() -> None:
    store = LearningStore(":memory:")
    valid_receipt = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({valid_receipt.receipt_id: valid_receipt})

    unknown_id = str(uuid4())
    lesson = Lesson(
        title="Valid PASS plus Unknown ID",
        steps=("step 1", "step 2"),
        claims=(
            Claim(
                text="Claim with valid PASS and missing receipt",
                receipt_ids=(valid_receipt.receipt_id, unknown_id),
            ),
        ),
    )
    attr = make_attribution()

    record = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attr,
        reader=reader,
    )

    assert record.status == "rejected"
    assert record.reason == REASON_INVALID_CITATION
    assert record.procedure_id is None

    # Proposals table row must store status and reason
    row = store.execute("SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,)).fetchone()
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reason"] == REASON_INVALID_CITATION
    assert row["procedure_id"] is None

    # No procedure or support rows must be created
    proc_count = store.execute("SELECT COUNT(*) as c FROM procedures").fetchone()["c"]
    assert proc_count == 0
    ver_count = store.execute("SELECT COUNT(*) as c FROM procedure_versions").fetchone()["c"]
    assert ver_count == 0
    supp_count = store.execute("SELECT COUNT(*) as c FROM procedure_support").fetchone()["c"]
    assert supp_count == 0


def test_claim_a_pass_claim_b_handoff_only_rejected_narration_only() -> None:
    store = LearningStore(":memory:")
    r_pass = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    r_handoff = make_receipt(kind=EvidenceKind.HANDOFF, verdict=None)
    reader = DictReceiptReader({
        r_pass.receipt_id: r_pass,
        r_handoff.receipt_id: r_handoff,
    })

    lesson = Lesson(
        title="Claim A PASS Claim B Handoff Only",
        steps=("step 1", "step 2"),
        claims=(
            Claim(text="Claim A", receipt_ids=(r_pass.receipt_id,)),
            Claim(text="Claim B", receipt_ids=(r_handoff.receipt_id,),
            ),
        ),
    )
    attr = make_attribution()

    record = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attr,
        reader=reader,
    )

    assert record.status == "rejected"
    assert record.reason == REASON_NARRATION_ONLY
    assert record.procedure_id is None

    row = store.execute("SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,)).fetchone()
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reason"] == REASON_NARRATION_ONLY

    proc_count = store.execute("SELECT COUNT(*) as c FROM procedures").fetchone()["c"]
    assert proc_count == 0


def test_verdict_none_verification_only_rejected_tool_success_only() -> None:
    store = LearningStore(":memory:")
    r_tool = make_receipt(kind=EvidenceKind.VERIFICATION, verdict=None, payload={"exit_code": 0})
    reader = DictReceiptReader({r_tool.receipt_id: r_tool})

    lesson = Lesson(
        title="Tool Success Only",
        steps=("run verification",),
        claims=(
            Claim(text="Verification ran with exit code 0", receipt_ids=(r_tool.receipt_id,)),
        ),
    )
    attr = make_attribution()

    record = create_proposal(
        store,
        unit_id=uuid4(),
        lesson=lesson,
        attribution=attr,
        reader=reader,
    )

    assert record.status == "rejected"
    assert record.reason == REASON_TOOL_SUCCESS_ONLY
    assert record.procedure_id is None

    row = store.execute("SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,)).fetchone()
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reason"] == REASON_TOOL_SUCCESS_ONLY

    proc_count = store.execute("SELECT COUNT(*) as c FROM procedures").fetchone()["c"]
    assert proc_count == 0


def test_accepted_creates_procedure_v1_with_attribution() -> None:
    store = LearningStore(":memory:")
    r_pass = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({r_pass.receipt_id: r_pass})

    precond = Precondition(key="project_id", op="eq", value="proj-alpha")
    lesson = Lesson(
        title="Accepted Procedure Lesson",
        steps=("step one", "step two"),
        preconditions=(precond,),
        claims=(
            Claim(text="Proved claim", receipt_ids=(r_pass.receipt_id,)),
        ),
    )
    attr = make_attribution(model="claude-3-opus", profile="anthropic-prod")
    unit_id = uuid4()

    record = create_proposal(
        store,
        unit_id=unit_id,
        lesson=lesson,
        attribution=attr,
        reader=reader,
    )

    assert record.status == "accepted"
    assert record.reason is None
    assert record.procedure_id is not None
    proc_id = record.procedure_id

    # Check proposals row
    prop_row = store.execute("SELECT * FROM proposals WHERE proposal_id = ?", (record.proposal_id,)).fetchone()
    assert prop_row is not None
    assert prop_row["unit_id"] == str(unit_id)
    assert prop_row["status"] == "accepted"
    assert prop_row["reason"] is None
    assert prop_row["procedure_id"] == proc_id
    assert prop_row["model"] == "claude-3-opus"
    assert prop_row["profile"] == "anthropic-prod"

    # Check procedures row: active, version 1, title
    proc_row = store.execute("SELECT * FROM procedures WHERE procedure_id = ?", (proc_id,)).fetchone()
    assert proc_row is not None
    assert proc_row["status"] == "active"
    assert proc_row["current_version"] == 1
    assert proc_row["title"] == "Accepted Procedure Lesson"
    assert proc_row["fingerprint"] == compute_lesson_fingerprint(lesson)

    # Check procedure_versions row: version 1, attribution copied
    ver_row = store.execute(
        "SELECT * FROM procedure_versions WHERE procedure_id = ? AND version = 1",
        (proc_id,),
    ).fetchone()
    assert ver_row is not None
    assert ver_row["version"] == 1
    assert ver_row["title"] == "Accepted Procedure Lesson"
    assert ver_row["model"] == "claude-3-opus"
    assert ver_row["profile"] == "anthropic-prod"
    assert ver_row["source_json"] == attr.source.model_dump_json()

    # Check procedure_support row: initial receipt recorded
    supp_rows = store.execute(
        "SELECT * FROM procedure_support WHERE procedure_id = ?",
        (proc_id,),
    ).fetchall()
    assert len(supp_rows) == 1
    assert supp_rows[0]["receipt_id"] == str(r_pass.receipt_id)
    assert supp_rows[0]["proposal_id"] == record.proposal_id


def test_same_lesson_same_receipt_support_count_unchanged_plus_new_receipt_plus_one() -> None:
    store = LearningStore(":memory:")
    r1 = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    r2 = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="PASS")
    reader = DictReceiptReader({
        r1.receipt_id: r1,
        r2.receipt_id: r2,
    })

    precond = Precondition(key="repository", op="eq", value="repo-test")
    lesson1 = Lesson(
        title="Reusable Procedure",
        steps=("step alpha", "step beta"),
        preconditions=(precond,),
        claims=(
            Claim(text="Proved with r1", receipt_ids=(r1.receipt_id,)),
        ),
    )
    attr = make_attribution()

    # Proposal 1: initial acceptance
    p1 = create_proposal(store, unit_id=uuid4(), lesson=lesson1, attribution=attr, reader=reader)
    assert p1.status == "accepted"
    proc_id = p1.procedure_id
    assert proc_id is not None

    supp1 = store.execute("SELECT COUNT(*) as c FROM procedure_support WHERE procedure_id = ?", (proc_id,)).fetchone()["c"]
    assert supp1 == 1

    # Proposal 2: same lesson + same receipt
    lesson_same = Lesson(
        title="Reusable Procedure - Run 2",
        steps=("step alpha", "step beta"),
        preconditions=(precond,),
        claims=(
            Claim(text="Proved with same r1", receipt_ids=(r1.receipt_id,)),
        ),
    )
    p2 = create_proposal(store, unit_id=uuid4(), lesson=lesson_same, attribution=attr, reader=reader)
    assert p2.status == "accepted"
    assert p2.procedure_id == proc_id

    # Known fingerprint adds no version
    ver_count = store.execute("SELECT COUNT(*) as c FROM procedure_versions WHERE procedure_id = ?", (proc_id,)).fetchone()["c"]
    assert ver_count == 1

    # Support count unchanged
    supp2 = store.execute("SELECT COUNT(*) as c FROM procedure_support WHERE procedure_id = ?", (proc_id,)).fetchone()["c"]
    assert supp2 == 1

    # Proposal 3: same lesson + new receipt r2
    lesson_new_receipt = Lesson(
        title="Reusable Procedure - Run 3",
        steps=("step alpha", "step beta"),
        preconditions=(precond,),
        claims=(
            Claim(text="Proved with new r2", receipt_ids=(r2.receipt_id,)),
        ),
    )
    p3 = create_proposal(store, unit_id=uuid4(), lesson=lesson_new_receipt, attribution=attr, reader=reader)
    assert p3.status == "accepted"
    assert p3.procedure_id == proc_id

    # Still no new version added
    ver_count3 = store.execute("SELECT COUNT(*) as c FROM procedure_versions WHERE procedure_id = ?", (proc_id,)).fetchone()["c"]
    assert ver_count3 == 1

    # Support count incremented by 1 (now 2)
    supp3 = store.execute("SELECT COUNT(*) as c FROM procedure_support WHERE procedure_id = ?", (proc_id,)).fetchone()["c"]
    assert supp3 == 2

    support_receipt_ids = {
        row["receipt_id"]
        for row in store.execute(
            "SELECT receipt_id FROM procedure_support WHERE procedure_id = ?",
            (proc_id,),
        ).fetchall()
    }
    assert support_receipt_ids == {str(r1.receipt_id), str(r2.receipt_id)}


def test_each_rejection_row_stores_its_reason() -> None:
    store = LearningStore(":memory:")
    attr = make_attribution()

    # 1. no claims -> no_citation
    lesson_no_claims = Lesson(title="No Claims", steps=("step 1",), claims=())
    p_no_claims = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_no_claims, attribution=attr, reader=DictReceiptReader()
    )
    assert p_no_claims.status == "rejected"
    assert p_no_claims.reason == REASON_NO_CITATION
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_no_claims.proposal_id,)).fetchone()
    assert row["reason"] == REASON_NO_CITATION

    # 2. claim citing nothing -> no_citation
    lesson_empty_citation = Lesson(
        title="Empty Citation",
        steps=("step 1",),
        claims=(Claim(text="Uncited claim", receipt_ids=()),),
    )
    p_empty_citation = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_empty_citation, attribution=attr, reader=DictReceiptReader()
    )
    assert p_empty_citation.status == "rejected"
    assert p_empty_citation.reason == REASON_NO_CITATION
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_empty_citation.proposal_id,)).fetchone()
    assert row["reason"] == REASON_NO_CITATION

    # 3. unresolvable receipt -> invalid_citation
    lesson_unresolvable = Lesson(
        title="Unresolvable",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(uuid4(),)),),
    )
    p_unresolvable = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_unresolvable, attribution=attr, reader=DictReceiptReader()
    )
    assert p_unresolvable.status == "rejected"
    assert p_unresolvable.reason == REASON_INVALID_CITATION
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_unresolvable.proposal_id,)).fetchone()
    assert row["reason"] == REASON_INVALID_CITATION

    # 4. narration only: plan
    r_plan = make_receipt(kind=EvidenceKind.PLAN, verdict=None)
    reader_plan = DictReceiptReader({r_plan.receipt_id: r_plan})
    lesson_plan = Lesson(
        title="Plan Only",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(r_plan.receipt_id,)),),
    )
    p_plan = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_plan, attribution=attr, reader=reader_plan
    )
    assert p_plan.status == "rejected"
    assert p_plan.reason == REASON_NARRATION_ONLY
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_plan.proposal_id,)).fetchone()
    assert row["reason"] == REASON_NARRATION_ONLY

    # 5. narration only: closeout
    r_closeout = make_receipt(kind=EvidenceKind.CLOSEOUT, verdict=None)
    reader_closeout = DictReceiptReader({r_closeout.receipt_id: r_closeout})
    lesson_closeout = Lesson(
        title="Closeout Only",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(r_closeout.receipt_id,)),),
    )
    p_closeout = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_closeout, attribution=attr, reader=reader_closeout
    )
    assert p_closeout.status == "rejected"
    assert p_closeout.reason == REASON_NARRATION_ONLY
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_closeout.proposal_id,)).fetchone()
    assert row["reason"] == REASON_NARRATION_ONLY

    # 6. narration only: intake_publication
    r_intake = make_receipt(kind=EvidenceKind.INTAKE_PUBLICATION, verdict=None)
    reader_intake = DictReceiptReader({r_intake.receipt_id: r_intake})
    lesson_intake = Lesson(
        title="Intake Only",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(r_intake.receipt_id,)),),
    )
    p_intake = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_intake, attribution=attr, reader=reader_intake
    )
    assert p_intake.status == "rejected"
    assert p_intake.reason == REASON_NARRATION_ONLY
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_intake.proposal_id,)).fetchone()
    assert row["reason"] == REASON_NARRATION_ONLY

    # 7. tool success only: verification with verdict NEEDS_FIX
    r_fix = make_receipt(kind=EvidenceKind.VERIFICATION, verdict="NEEDS_FIX")
    reader_fix = DictReceiptReader({r_fix.receipt_id: r_fix})
    lesson_fix = Lesson(
        title="Needs Fix",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(r_fix.receipt_id,)),),
    )
    p_fix = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_fix, attribution=attr, reader=reader_fix
    )
    assert p_fix.status == "rejected"
    assert p_fix.reason == REASON_TOOL_SUCCESS_ONLY
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_fix.proposal_id,)).fetchone()
    assert row["reason"] == REASON_TOOL_SUCCESS_ONLY

    # 8. tool success only: verification with verdict None
    r_none = make_receipt(kind=EvidenceKind.VERIFICATION, verdict=None)
    reader_none = DictReceiptReader({r_none.receipt_id: r_none})
    lesson_none = Lesson(
        title="Verdict None",
        steps=("step 1",),
        claims=(Claim(text="Claim", receipt_ids=(r_none.receipt_id,)),),
    )
    p_none = create_proposal(
        store, unit_id=uuid4(), lesson=lesson_none, attribution=attr, reader=reader_none
    )
    assert p_none.status == "rejected"
    assert p_none.reason == REASON_TOOL_SUCCESS_ONLY
    row = store.execute("SELECT reason FROM proposals WHERE proposal_id = ?", (p_none.proposal_id,)).fetchone()
    assert row["reason"] == REASON_TOOL_SUCCESS_ONLY


def test_audit_receipt_with_pass_verdict_supports_claim() -> None:
    store = LearningStore(":memory:")
    r_audit = make_receipt(kind=EvidenceKind.AUDIT, verdict="PASS")
    reader = DictReceiptReader({r_audit.receipt_id: r_audit})

    lesson = Lesson(
        title="Audit Supported Lesson",
        steps=("audit step",),
        claims=(Claim(text="Audit passed", receipt_ids=(r_audit.receipt_id,)),),
    )
    attr = make_attribution()

    decision = evaluate(lesson, reader)
    assert decision.accepted is True
    assert decision.reason is None

    record = create_proposal(store, unit_id=uuid4(), lesson=lesson, attribution=attr, reader=reader)
    assert record.status == "accepted"
    assert record.procedure_id is not None


def test_fingerprint_normalization_invariants() -> None:
    p1 = Precondition(key="project_id", op="eq", value="p-1")
    p2 = Precondition(key="repository", op="ne", value="r-2")

    # Order of preconditions does not change fingerprint
    l1 = Lesson(title="T1", steps=("step A", "step B"), preconditions=(p1, p2))
    l2 = Lesson(title="T2", steps=("step A", "step B"), preconditions=(p2, p1))
    assert compute_lesson_fingerprint(l1) == compute_lesson_fingerprint(l2)

    # Whitespace padding on steps does not change fingerprint
    l3 = Lesson(title="T3", steps=("  step A  ", "step B\n"), preconditions=(p1, p2))
    assert compute_lesson_fingerprint(l1) == compute_lesson_fingerprint(l3)

    # Different steps produce different fingerprint
    l_diff_steps = Lesson(title="T4", steps=("step A", "step C"), preconditions=(p1, p2))
    assert compute_lesson_fingerprint(l1) != compute_lesson_fingerprint(l_diff_steps)

    # Different preconditions produce different fingerprint
    p3 = Precondition(key="project_id", op="eq", value="p-other")
    l_diff_pre = Lesson(title="T5", steps=("step A", "step B"), preconditions=(p3,))
    assert compute_lesson_fingerprint(l1) != compute_lesson_fingerprint(l_diff_pre)
