
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.cockpit_pipeline import submit_and_pipeline


def test_full_path_smoke(tmp_path):
    caps = tmp_path / "BUDGET-CAPS.json"
    caps.write_text(json.dumps({
        "soft_warn_fraction": 0.7,
        "hard_refuse_fraction": 1.0,
        "post_program_milestone_soft_tokens": 2000000,
        "post_program_milestone_hard_tokens": 5000000,
    }))
    meta, result = submit_and_pipeline(
        store_path=tmp_path / "cockpit.json",
        job_id="fullpath-smoke",
        objective="post-program continuous admit",
        query="write-first effort autonomy",
        caps_path=caps,
        enola_store_path=tmp_path / "enola.json",
        enola_enabled=True,
        ledger_attach=True,
        ledger_path=tmp_path / "led.json",
    )
    assert meta["submit"]["ok"] is True
    assert result.stopped if hasattr(result, "stopped") else result.campaign.stopped
    assert result.ledger_event is not None
    assert (tmp_path / "cockpit.json").is_file()
    assert (tmp_path / "led.json").is_file()
