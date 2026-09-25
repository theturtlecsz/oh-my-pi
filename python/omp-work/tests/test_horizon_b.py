"""Horizon B acceptance smoke."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.knowledge_b1 import produce_finding
from omp_work.research_campaign import run_campaign
from omp_work.context_compile import compile_context

def test_b1_reuse():
    f = produce_finding(job_id="t-b1")
    f.reuse_in("p1", "note1")
    f.reuse_in("p2", "note2")
    assert len(f.eng_packets) == 2 and f.finding_text

def test_b2_one_revision_stop():
    r = run_campaign(campaign_id="t", question="q", retrieved_facts=["a", "b"], parent_budget_tokens=4000)
    assert r.phases[-1] == "stop" and r.phases[-2] == "revision"
    assert len(r.candidates) <= 2

def test_b3_compile():
    c = compile_context(job_id="t", findings=["x"], objective="o")
    assert "Compiled context" in c.as_packet_section()
