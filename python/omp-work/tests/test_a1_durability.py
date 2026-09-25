"""Horizon A1 durability acceptance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.durability_a1 import run_a1_suite, DurabilityStore, fault_coordinator_restart

def test_a1_suite_pass():
    r = run_a1_suite()
    assert r["ok"], r
    assert r["zero_lost_accepted_records"]
    assert r["zero_duplicate_effects"]
    names = {f["name"] for f in r["faults"]}
    assert names == {"coordinator_restart", "worker_loss", "committed_op_lost_response", "cancel_queued_running_late"}

def test_coordinator_restart_retains_accept():
    s = DurabilityStore()
    s2, rep = fault_coordinator_restart(s)
    assert rep["ok"]
    assert s2.integrity()["accepted_count"] >= 1
