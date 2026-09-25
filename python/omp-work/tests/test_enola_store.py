"""EnolaStore persistence acceptance."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.enola_store import EnolaStore

def test_record_and_reload(tmp_path):
    path = tmp_path / "enola.json"
    s1 = EnolaStore.open(path)
    r = s1.record_trace(job_id="j1", goal="stay E1", facts=["Prefer Flash", "budget soft 70%"])
    assert r.finding()
    assert path.is_file()
    s2 = EnolaStore.open(path)
    rows = s2.for_job("j1")
    assert len(rows) == 1
    assert "E1" in rows[0]["goal"] or "Flash" in rows[0]["finding"]

def test_requires_job_id(tmp_path):
    s = EnolaStore.open(tmp_path / "e.json")
    try:
        s.record_trace(job_id=" ", goal="x")
        assert False
    except ValueError:
        pass
