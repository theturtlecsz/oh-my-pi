import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.research_ledger import run_campaign_with_ledger, append_ledger_event

def test_ledged_campaign(tmp_path):
    led = run_campaign_with_ledger(
        campaign_id="camp-1",
        question="What is E1?",
        retrieved_facts=["Default Flash", "effort_reason for escalate"],
    )
    assert led.campaign.stopped is True
    assert led.ledger_event["conversation_id"] == "camp-1"
    assert led.ledger_event["input_tokens"] + led.ledger_event["output_tokens"] == led.campaign.spent_tokens
    path = tmp_path / "ledger.json"
    append_ledger_event(path, led.ledger_event)
    data = json.loads(path.read_text())
    assert data["events"][0]["kind"] == "research_campaign"

def test_to_dict():
    led = run_campaign_with_ledger(campaign_id="c2", question="q", retrieved_facts=["a"])
    d = led.to_dict()
    assert d["stopped"] is True
    assert "ledger_event" in d
