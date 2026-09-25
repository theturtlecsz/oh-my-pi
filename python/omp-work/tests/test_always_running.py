
import sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.always_running import check_stall

def test_halt_not_stalled(monkeypatch, tmp_path):
    # smoke import path only — live ACTIVE exists on arch-dev
    r = check_stall(max_idle_minutes=99999)
    assert r.stalled is False or r.reason in ("fresh", "queue_file_idle", "halted")
