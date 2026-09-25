"""PersistentCockpit survives process restart."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.cockpit_persist import PersistentCockpit
from omp_work.cockpit_verbs import Cockpit

def test_persist_roundtrip(tmp_path):
    path = tmp_path / "cockpit.json"
    c1 = PersistentCockpit(path)
    out = c1.submit(objective="persist-me", job_id="job-persist-1")
    assert out["ok"] is True
    assert path.is_file()
    # new process façade
    c2 = PersistentCockpit(path)
    st = c2.status(job_id="job-persist-1")
    assert st["job_id"] == "job-persist-1"
    assert "persist-me" in str(st) or st.get("state")

def test_dedupe_after_reload(tmp_path):
    path = tmp_path / "cockpit.json"
    c1 = PersistentCockpit(path)
    c1.submit(objective="x", job_id="dup-1")
    c2 = PersistentCockpit(path)
    again = c2.submit(objective="x", job_id="dup-1")
    assert again.get("deduped") is True

def test_inherits_cockpit():
    assert issubclass(PersistentCockpit, Cockpit)
