
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.cockpit_pipeline import submit_and_pipeline

CAPS = Path("/home/thetu/.codex/workflows/economy/ACTIVE/BUDGET-CAPS.json")

def test_full_path_smoke(tmp_path):
    meta, result = submit_and_pipeline(
        store_path=tmp_path / "cockpit.json",
        job_id="fullpath-smoke",
        objective="post-program continuous admit",
        query="write-first effort autonomy",
        caps_path=CAPS,
        enola_enabled=True,
        ledger_attach=True,
        ledger_path=tmp_path / "led.json",
    )
    assert meta["submit"]["ok"] is True
    assert result.stopped if hasattr(result, "stopped") else result.campaign.stopped
    assert result.ledger_event is not None
    assert (tmp_path / "cockpit.json").is_file()
    assert (tmp_path / "led.json").is_file()
