"""Tests for ADR 0004 control plane checks: high_risk_review and contract_freeze."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from omp_work.control_plane.adr0004 import (
    BLAST_RADIUS_PATTERNS,
    FROZEN_CONTRACT_PATHS,
    check_contract_freeze,
    check_high_risk_review,
    contract_freeze,
    high_risk_review,
)
from omp_work.control_plane.gate import (
    Check,
    CheckContext,
    ControlPlaneFacts,
    DecisionPayload,
    Proposal,
    Refusal,
    Verdict,
    decision_payload,
    evaluate,
)


def _make_context(
    approved_hashes: frozenset[str] = frozenset(),
    revision: int = 1,
) -> CheckContext:
    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
    facts = ControlPlaneFacts(
        now=now,
        current_revision=revision,
        approved_contract_sha256=approved_hashes,
    )
    return CheckContext(facts=facts)


def test_check_objects_and_attributes() -> None:
    assert isinstance(high_risk_review, Check)
    assert high_risk_review.name == "high_risk_review"
    assert callable(high_risk_review)
    assert high_risk_review.fn is check_high_risk_review

    assert isinstance(contract_freeze, Check)
    assert contract_freeze.name == "contract_freeze"
    assert callable(contract_freeze)
    assert contract_freeze.fn is check_contract_freeze


def test_blast_radius_path_unreviewed_refused() -> None:
    ctx = _make_context()
    proposal = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=False,
        reviewer_override_reason=None,
    )

    verdict = evaluate(proposal, ctx, [high_risk_review])
    assert verdict.allowed is False
    assert len(verdict.refusals) == 1
    refusal = verdict.refusals[0]
    assert refusal.check == "high_risk_review"
    assert refusal.code == "review_required"
    assert refusal.raise_decision is False
    assert refusal.action_class is None
    assert verdict.raises_decision is False

    # Also check when reviewer_required is None and override is None
    p_none = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=None,
        reviewer_override_reason=None,
    )
    direct_refusal = high_risk_review(p_none, ctx)
    assert direct_refusal == refusal


def test_blast_radius_reviewed_or_20_char_override_passes() -> None:
    ctx = _make_context()

    # Case A: reviewer_required is True
    p_reviewed = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=True,
        reviewer_override_reason=None,
    )
    v_reviewed = evaluate(p_reviewed, ctx, [high_risk_review])
    assert v_reviewed.allowed is True
    assert len(v_reviewed.refusals) == 0
    assert high_risk_review(p_reviewed, ctx) is None

    # Case B: reviewer_override_reason with exactly 20 non-blank characters
    override_20 = "12345678901234567890"
    p_override_20 = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=False,
        reviewer_override_reason=override_20,
    )
    v_override_20 = evaluate(p_override_20, ctx, [high_risk_review])
    assert v_override_20.allowed is True
    assert len(v_override_20.refusals) == 0

    # Case C: reviewer_override_reason with >= 20 non-blank chars and spaces
    override_spaces = "   Emergency hotfix for OMP-421 production bug   "
    p_spaces = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=None,
        reviewer_override_reason=override_spaces,
    )
    v_spaces = evaluate(p_spaces, ctx, [high_risk_review])
    assert v_spaces.allowed is True
    assert len(v_spaces.refusals) == 0


def test_blast_radius_19_char_override_refused() -> None:
    ctx = _make_context()

    # 19 characters without spaces
    override_19 = "1234567890123456789"
    proposal = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=False,
        reviewer_override_reason=override_19,
    )
    verdict = evaluate(proposal, ctx, [high_risk_review])
    assert verdict.allowed is False
    assert len(verdict.refusals) == 1
    assert verdict.refusals[0].code == "review_required"
    assert verdict.refusals[0].raise_decision is False

    # 19 non-blank characters with surrounding and embedded spaces (total length > 20)
    override_19_padded = "   1234567890 123456789   "
    assert sum(1 for c in override_19_padded if not c.isspace()) == 19
    assert len(override_19_padded) > 20
    p_padded = Proposal(
        proposal_id=uuid4(),
        touched_paths=("python/omp-work/src/omp_work/v1/service.py",),
        reviewer_required=None,
        reviewer_override_reason=override_19_padded,
    )
    verdict_padded = evaluate(p_padded, ctx, [high_risk_review])
    assert verdict_padded.allowed is False
    assert len(verdict_padded.refusals) == 1
    assert verdict_padded.refusals[0].code == "review_required"


def test_unrelated_path_passes_without_review() -> None:
    ctx = _make_context()

    proposal = Proposal(
        proposal_id=uuid4(),
        touched_paths=("docs/README.md", "session-system/tests/example.test.ts"),
        reviewer_required=False,
        reviewer_override_reason=None,
    )
    verdict = evaluate(proposal, ctx, [high_risk_review])
    assert verdict.allowed is True
    assert len(verdict.refusals) == 0
    assert high_risk_review(proposal, ctx) is None

    # Empty touched paths
    p_empty = Proposal(proposal_id=uuid4(), touched_paths=())
    assert high_risk_review(p_empty, ctx) is None


def test_contract_freeze_unapproved_hash_refused() -> None:
    approved = frozenset({"feedface" * 8})
    ctx = _make_context(approved_hashes=approved)

    proposal = Proposal(
        proposal_id=uuid4(),
        contract_sha256="deadbeef" * 8,
        touched_paths=(),
    )
    verdict = evaluate(proposal, ctx, [contract_freeze])
    assert verdict.allowed is False
    assert len(verdict.refusals) == 1
    refusal = verdict.refusals[0]
    assert refusal.check == "contract_freeze"
    assert refusal.code == "contract_not_approved"
    assert refusal.raise_decision is True
    assert refusal.action_class == "contract_hash"
    assert verdict.raises_decision is True


def test_contract_freeze_prefix_of_approved_hash_refused() -> None:
    full_hash = "abcdef0123456789" * 4
    approved = frozenset({full_hash})
    ctx = _make_context(approved_hashes=approved)

    prefix_hash = full_hash[:32]
    proposal = Proposal(
        proposal_id=uuid4(),
        contract_sha256=prefix_hash,
        touched_paths=(),
    )
    verdict = evaluate(proposal, ctx, [contract_freeze])
    assert verdict.allowed is False
    assert len(verdict.refusals) == 1
    refusal = verdict.refusals[0]
    assert refusal.check == "contract_freeze"
    assert refusal.code == "contract_not_approved"
    assert refusal.raise_decision is True
    assert refusal.action_class == "contract_hash"


def test_contract_freeze_approved_hash_passes() -> None:
    full_hash = "abcdef0123456789" * 4
    approved = frozenset({full_hash})
    ctx = _make_context(approved_hashes=approved)

    proposal = Proposal(
        proposal_id=uuid4(),
        contract_sha256=full_hash,
        touched_paths=(
            "python/omp-work/src/omp_work/contracts/v1/schema.json",
            "packages/work-client/src/contract.ts",
        ),
    )
    verdict = evaluate(proposal, ctx, [contract_freeze])
    assert verdict.allowed is True
    assert len(verdict.refusals) == 0
    assert contract_freeze(proposal, ctx) is None


def test_contract_freeze_frozen_path_without_hash_refused() -> None:
    ctx = _make_context()

    # Case A: contracts/v1/*.json
    p_json = Proposal(
        proposal_id=uuid4(),
        contract_sha256=None,
        touched_paths=("python/omp-work/src/omp_work/contracts/v1/api-schema.json",),
    )
    v_json = evaluate(p_json, ctx, [contract_freeze])
    assert v_json.allowed is False
    assert len(v_json.refusals) == 1
    refusal_json = v_json.refusals[0]
    assert refusal_json.check == "contract_freeze"
    assert refusal_json.code == "contract_frozen"
    assert refusal_json.raise_decision is True
    assert refusal_json.action_class == "contract_hash"

    # Case B: packages/work-client/src/contract.ts
    p_ts = Proposal(
        proposal_id=uuid4(),
        contract_sha256=None,
        touched_paths=("packages/work-client/src/contract.ts",),
    )
    v_ts = evaluate(p_ts, ctx, [contract_freeze])
    assert v_ts.allowed is False
    assert len(v_ts.refusals) == 1
    refusal_ts = v_ts.refusals[0]
    assert refusal_ts.check == "contract_freeze"
    assert refusal_ts.code == "contract_frozen"
    assert refusal_ts.raise_decision is True
    assert refusal_ts.action_class == "contract_hash"


def test_contract_freeze_unrelated_path_without_hash_passes() -> None:
    ctx = _make_context()

    proposal = Proposal(
        proposal_id=uuid4(),
        contract_sha256=None,
        touched_paths=("python/omp-work/src/omp_work/utils.py", "docs/index.md"),
    )
    verdict = evaluate(proposal, ctx, [contract_freeze])
    assert verdict.allowed is True
    assert len(verdict.refusals) == 0
    assert contract_freeze(proposal, ctx) is None


def test_decision_payload_over_freeze_refusal_carries_contract_hash() -> None:
    pid = UUID("00000000-0000-0000-0000-000000000100")
    prid = UUID("00000000-0000-0000-0000-000000000200")
    ctx = _make_context()

    proposal = Proposal(
        proposal_id=pid,
        project_id=prid,
        contract_sha256=None,
        touched_paths=("python/omp-work/src/omp_work/contracts/v1/schema.json",),
    )
    verdict = evaluate(proposal, ctx, [contract_freeze])
    assert verdict.allowed is False
    assert verdict.raises_decision is True

    payload = decision_payload(proposal, verdict)
    assert isinstance(payload, DecisionPayload)
    assert payload.action_class == "contract_hash"
    assert payload["action_class"] == "contract_hash"
    assert payload.project_id == prid
    assert payload.question == f"Authorize control plane proposal {pid}?"
    assert payload.options == ("authorize", "reject")

    # Also verify direct refusal object input
    refusal = verdict.refusals[0]
    payload_direct = decision_payload(proposal, refusal)
    assert payload_direct.action_class == "contract_hash"

    # Also test unapproved contract_sha256 refusal
    p_unapproved = Proposal(
        proposal_id=pid,
        project_id=prid,
        contract_sha256="badhash",
    )
    v_unapproved = evaluate(p_unapproved, ctx, [contract_freeze])
    p_unapproved_payload = decision_payload(p_unapproved, v_unapproved)
    assert p_unapproved_payload.action_class == "contract_hash"


def test_blast_radius_patterns_all_categories_covered() -> None:
    ctx = _make_context()

    test_paths = [
        # policy
        "docs/ACTIVE-POLICY.md",
        "some/path/ACTIVE-POLICY-v1",
        # ledger
        "python/omp-work/src/omp_work/v1/models.py",
        "python/omp-work/src/omp_work/operations/migrations/001_initial.sql",
        "python/omp-work/src/omp_work/operations/jobs_migrations/002_jobs.sql",
        "python/omp-work/src/omp_work/operations/sql/query.sql",
        # auth
        "python/omp-work/src/omp_work/operations/capabilities.py",
        "python/omp-work/src/omp_work/v1/owner_signature.py",
        "python/omp-work/src/omp_work/control_plane/gate.py",
        # economy
        "python/omp-work/src/omp_work/jobs/budget.py",
        "python/omp-work/src/omp_work/spend_budget.py",
        "python/omp-work/src/omp_work/mission_budget.py",
        "python/omp-work/src/omp_work/economy_effort.py",
        # deploy/release
        ".github/workflows/ci.yml",
        "scripts/release.ts",
        "infra/terraform/main.tf",
    ]

    for p in test_paths:
        proposal = Proposal(
            proposal_id=uuid4(),
            touched_paths=(p,),
            reviewer_required=False,
            reviewer_override_reason=None,
        )
        res = high_risk_review(proposal, ctx)
        assert res is not None, f"Expected {p} to match BLAST_RADIUS_PATTERNS"
        assert res.code == "review_required"
        assert res.raise_decision is False


def test_combined_evaluation_precedence_and_reporting() -> None:
    ctx = _make_context()
    # A proposal touching both a blast-radius path without review
    # and a frozen contract path without hash.
    proposal = Proposal(
        proposal_id=uuid4(),
        touched_paths=(
            "python/omp-work/src/omp_work/v1/service.py",
            "python/omp-work/src/omp_work/contracts/v1/schema.json",
        ),
        reviewer_required=False,
        reviewer_override_reason=None,
        contract_sha256=None,
    )

    verdict = evaluate(proposal, ctx, [high_risk_review, contract_freeze])
    assert verdict.allowed is False
    assert len(verdict.refusals) == 2
    codes = {r.code for r in verdict.refusals}
    assert codes == {"review_required", "contract_frozen"}

    # high_risk_review does NOT raise decision, but contract_frozen DOES raise decision
    assert verdict.raises_decision is True
    assert verdict.deciding_refusal is not None
    assert verdict.deciding_refusal.code == "contract_frozen"

    payload = decision_payload(proposal, verdict)
    assert payload.action_class == "contract_hash"
