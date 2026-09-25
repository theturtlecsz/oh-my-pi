import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.cockpit_pipeline import submit_and_pipeline

CAPS = Path("/home/thetu/.codex/workflows/economy/ACTIVE/BUDGET-CAPS.json")

def test_submit_and_pipeline(tmp_path):
    cockpit = tmp_path / "c.json"
    meta, result = submit_and_pipeline(
        store_path=cockpit,
        job_id="cp-1",
        objective="seal under E1",
        query="write-first",
        store={"write-first": {"claim": "Prefer write-first", "source": "t"}},
        caps_path=CAPS,
        enola_enabled=False,
        ledger_attach=False,
    )
    assert meta["submit"]["ok"] is True
    assert result.job_id == "cp-1"
    assert result.campaign.stopped is True
    assert cockpit.is_file()
