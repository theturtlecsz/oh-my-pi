"""Product acceptance gates mission completion independently of technical delivery."""

from dataclasses import replace
from uuid import UUID

import pytest
from omp_work.orchestrator.stages import (
    PRODUCT_ACCEPTANCE_RULE,
    Bounds,
    Facts,
    ProductAcceptance,
    decide,
)

BOUNDS = Bounds(max_retries=2, repair_rounds=3)
CLOSE = Facts(
    mission_id="m1",
    project_id=UUID("11111111-1111-1111-1111-111111111111"),
    step_index=11,
    stage="close",
    mission_status="active",
    outcome="succeeded",
    qualified=True,
)


def test_close_without_required_product_acceptance_completes() -> None:
    step = decide(CLOSE, BOUNDS)
    assert (step.kind, step.rule_id, step.next_stage, step.decision) == (
        "end", "mission-complete", None, None
    )


@pytest.mark.parametrize("acceptance", ["none", "pending", "fail"])
@pytest.mark.parametrize("candidate", [None, "ab" * 20])
def test_unaccepted_delivery_pauses_with_owner_choices_and_evidence(
    acceptance: ProductAcceptance, candidate: str | None
) -> None:
    facts = replace(
        CLOSE,
        requires_product_acceptance=True,
        product_acceptance=acceptance,
        candidate_commit=candidate,
    )
    step = decide(facts, BOUNDS)
    assert (step.kind, step.rule_id, step.stage, step.next_stage) == (
        "pause", PRODUCT_ACCEPTANCE_RULE, "close", "close"
    )
    decision = step.decision
    assert decision is not None
    assert decision.options == ("accept", "reject")
    assert decision.default_if_any is None
    assert decision.action_class is None
    assert decision.target_sha256 is None
    assert set(decision.risk_of_each_choice) == {"accept", "reject"}
    assert all(decision.risk_of_each_choice.values())
    expected_refs = ("orch_step:m1:11",)
    if candidate is not None:
        expected_refs += (f"delivery:{candidate}",)
    assert decision.evidence_refs == expected_refs
    assert decision.resume_state == "close"


def test_accepted_product_allows_mission_completion() -> None:
    step = decide(
        replace(CLOSE, requires_product_acceptance=True, product_acceptance="pass"), BOUNDS
    )
    assert (step.kind, step.rule_id, step.next_stage, step.decision) == (
        "end", "mission-complete", None, None
    )


def test_product_acceptance_does_not_block_earlier_stage_success() -> None:
    step = decide(replace(CLOSE, stage="merge", requires_product_acceptance=True), BOUNDS)
    assert (step.kind, step.rule_id, step.next_stage, step.decision) == (
        "advance", "stage-order", "close", None
    )


def test_invalid_product_acceptance_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown product_acceptance"):
        replace(CLOSE, product_acceptance="approved")  # type: ignore[arg-type]


def test_equal_facts_produce_equal_acceptance_steps() -> None:
    facts = replace(CLOSE, requires_product_acceptance=True, candidate_commit="cd" * 20)
    equal_facts = replace(facts)
    assert facts == equal_facts
    assert decide(facts, BOUNDS) == decide(equal_facts, BOUNDS)
