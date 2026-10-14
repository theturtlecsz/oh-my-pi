import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.research_campaign import run_campaign
from omp_work.research_ledger import append_ledger_event, run_campaign_with_ledger
from omp_work.routing.policy import load_policy


def test_research_ledger_model_is_null_and_no_tier_model(tmp_path: Path) -> None:
    campaign_id = "camp-attr-1"
    question = "What is the policy?"
    retrieved_facts = ["Fact 1", "Fact 2"]

    ledged = run_campaign_with_ledger(
        campaign_id=campaign_id,
        question=question,
        retrieved_facts=retrieved_facts,
    )

    ledger_path = tmp_path / "ledger.json"
    append_ledger_event(ledger_path, ledged.ledger_event)

    persisted = json.loads(ledger_path.read_text())
    persisted_event = persisted["events"][0]

    assert "model" in persisted_event
    assert persisted_event["model"] is None

    # No event string contains "gemini" or a load_policy() tier model
    policy = load_policy()
    tier_models = [tier.model for tier in policy.tiers.values()]
    event_str = json.dumps(persisted_event)

    assert "gemini" not in event_str.lower()
    for tier_model in tier_models:
        assert tier_model not in event_str

    for key, value in persisted_event.items():
        if isinstance(value, str):
            assert "gemini" not in value.lower()
            for tier_model in tier_models:
                assert tier_model not in value


def test_research_ledger_tokens_and_campaign_parity(tmp_path: Path) -> None:
    campaign_id = "camp-attr-2"
    question = "Compare direct vs ledged"
    retrieved_facts = ["Fact Alpha", "Fact Beta"]
    parent_budget_tokens = 6000

    ledged = run_campaign_with_ledger(
        campaign_id=campaign_id,
        question=question,
        retrieved_facts=retrieved_facts,
        parent_budget_tokens=parent_budget_tokens,
    )

    ledger_path = tmp_path / "ledger.json"
    append_ledger_event(ledger_path, ledged.ledger_event)

    persisted = json.loads(ledger_path.read_text())
    persisted_event = persisted["events"][0]

    # Persisted tokens sum to campaign.spent_tokens
    assert persisted_event["input_tokens"] + persisted_event["output_tokens"] == ledged.campaign.spent_tokens

    # campaign.finding/stopped/eval_winner_n equal a direct research_campaign.run_campaign with the same inputs
    direct = run_campaign(
        campaign_id=campaign_id,
        question=question,
        retrieved_facts=retrieved_facts,
        parent_budget_tokens=parent_budget_tokens,
    )
    assert ledged.campaign.finding == direct.finding
    assert ledged.campaign.stopped == direct.stopped
    assert ledged.campaign.eval_winner_n == direct.eval_winner_n
    assert ledged.campaign.spent_tokens == direct.spent_tokens


def test_passing_model_raises_type_error() -> None:
    with pytest.raises(TypeError):
        run_campaign_with_ledger(
            campaign_id="camp-attr-3",
            question="Should fail on model arg",
            retrieved_facts=["Fact 1"],
            model="gemini-3.8-flash-high",  # type: ignore[call-arg]
        )
