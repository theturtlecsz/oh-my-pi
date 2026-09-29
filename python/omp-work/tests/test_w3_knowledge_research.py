"""W3 knowledge + research campaign acceptance."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.enola_adapter import reason_trace
from omp_work.research_campaign import run_campaign


def test_adapters_require_job_id():
    try:
        reason_trace(goal="x", job_id="")
        assert False
    except ValueError:
        pass


def test_campaign_phases_and_bon2():
    r = run_campaign(
        campaign_id="camp-w3-1",
        question="How should empty soft be treated?",
        retrieved_facts=["Prefer write-first", "Escalate with effort_reason"],
        parent_budget_tokens=8000,
    )
    assert r.phases == ["baseline", "candidate", "eval", "revision", "stop"]
    assert len(r.candidates) == 2
    assert r.stopped and r.adapted
    assert r.spent_tokens <= r.parent_budget_tokens
    assert r.finding


def test_finding_into_eng_packet_shape():
    r = run_campaign(
        campaign_id="camp-w3-1",
        question="q",
        retrieved_facts=["finding-alpha"],
        parent_budget_tokens=4000,
    )
    eng = "# ENG packet" + chr(10) + "## Retrieved research finding" + chr(10) + r.finding + chr(10)
    assert "finding-alpha" in eng or "Revised" in eng
