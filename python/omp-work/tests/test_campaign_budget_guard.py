
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.campaign_budget_guard import run_campaign_guarded

def test_too_small_parent():
    try:
        run_campaign_guarded(campaign_id="c", question="q", retrieved_facts=["a"], parent_budget_tokens=2)
        assert False
    except ValueError as e:
        assert "too small" in str(e) or "exceeded" in str(e) or "budget" in str(e).lower()

def test_ok():
    r = run_campaign_guarded(campaign_id="c2", question="q", retrieved_facts=["a", "b"], parent_budget_tokens=8000)
    assert r.stopped is True
