"""CogneeStore persistence acceptance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.cognee_store import CogneeStore, run_job_query

def test_seed_and_query(tmp_path):
    path = tmp_path / "cognee.json"
    s = CogneeStore.open(path)
    assert path.is_file()
    assert "write-first" in s.nodes
    r = s.query(query="write-first", job_id="job-1")
    assert r.nodes
    assert "write-first" in r.finding().lower() or "Prefer write-first" in r.finding()

def test_upsert_survives_reload(tmp_path):
    path = tmp_path / "cognee.json"
    s1 = CogneeStore.open(path, seed_if_empty=False)
    s1.upsert("budget-caps", claim="Soft warn 70%; hard refuse 100%", source="BUDGET-CAPS")
    s2 = CogneeStore.open(path)
    assert "budget-caps" in s2.nodes
    r = run_job_query(store_path=path, job_id="job-2", query="budget")
    assert r.invoked_via.startswith("workservice:")
    assert "70%" in r.finding() or "budget" in r.finding().lower() or r.nodes

def test_requires_job_id(tmp_path):
    path = tmp_path / "c.json"
    CogneeStore.open(path)
    try:
        run_job_query(store_path=path, job_id=" ", query="x")
        assert False
    except ValueError:
        pass
