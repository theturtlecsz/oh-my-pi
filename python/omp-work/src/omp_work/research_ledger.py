"""Attach research_campaign spends to the post-program usage ledger shape."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time

from omp_work.research_campaign import run_campaign, CampaignResult


@dataclass(frozen=True)
class LedgedCampaign:
    campaign: CampaignResult
    ledger_event: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        c = self.campaign
        return {
            "campaign_id": c.campaign_id,
            "finding": c.finding,
            "spent_tokens": c.spent_tokens,
            "stopped": c.stopped,
            "ledger_event": self.ledger_event,
        }


def run_campaign_with_ledger(
    *,
    campaign_id: str,
    question: str,
    retrieved_facts: list[str],
    parent_budget_tokens: int = 8000,
    model: str = "gemini-3.8-flash-high",
    role: str = "researcher",
) -> LedgedCampaign:
    camp = run_campaign(
        campaign_id=campaign_id,
        question=question,
        retrieved_facts=retrieved_facts,
        parent_budget_tokens=parent_budget_tokens,
    )
    # Split spend roughly half in / half out for ledger fidelity
    half = max(1, camp.spent_tokens // 2)
    event = {
        "conversation_id": campaign_id,
        "step_index": 1,
        "role": role,
        "model": model,
        "input_tokens": half,
        "output_tokens": camp.spent_tokens - half,
        "at": time.time(),
        "kind": "research_campaign",
        "stopped": camp.stopped,
    }
    return LedgedCampaign(campaign=camp, ledger_event=event)


def append_ledger_event(ledger_path: Path | str, event: dict[str, Any]) -> None:
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        data = json.loads(path.read_text())
    else:
        data = {"events": []}
    if isinstance(data, list):
        data.append(event)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return
    events = data.setdefault("events", [])
    events.append(event)
    path.write_text(json.dumps(data, indent=2) + "\n")
