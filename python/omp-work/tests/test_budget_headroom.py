import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.budget_headroom import compute_headroom

def test_headroom_soft_warn(tmp_path):
    caps = {
        "soft_warn_fraction": 0.7,
        "hard_refuse_fraction": 1.0,
        "post_program_milestone_soft_tokens": 1000,
        "post_program_milestone_hard_tokens": 2000,
        "post_program_ledger": str(tmp_path / "led.json"),
    }
    caps_path = tmp_path / "caps.json"
    caps_path.write_text(json.dumps(caps))
    (tmp_path / "led.json").write_text(json.dumps({
        "events": [{"input_tokens": 400, "output_tokens": 350}]
    }))
    h = compute_headroom(caps_path=caps_path)
    assert h.spent_tokens == 750
    assert h.soft_warn is True
    assert h.hard_refuse is False
    assert h.soft_remaining == 250

def test_hard_refuse(tmp_path):
    caps = {
        "soft_warn_fraction": 0.7,
        "hard_refuse_fraction": 1.0,
        "post_program_milestone_soft_tokens": 100,
        "post_program_milestone_hard_tokens": 100,
    }
    caps_path = tmp_path / "c.json"
    caps_path.write_text(json.dumps(caps))
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"events": [{"tokens": 100}]}))
    h = compute_headroom(caps_path=caps_path, ledger_path=led)
    assert h.hard_refuse is True
