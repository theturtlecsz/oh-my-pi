"""Horizon B acceptance smoke."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.research_campaign import run_campaign


def test_b2_one_revision_stop():
    r = run_campaign(campaign_id="t", question="q", retrieved_facts=["a", "b"], parent_budget_tokens=4000)
    assert r.phases[-1] == "stop" and r.phases[-2] == "revision"
    assert len(r.candidates) <= 2
