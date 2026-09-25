import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.ledger_milestone import sync_milestone_total, sum_event_tokens

def test_sync(tmp_path):
    p = tmp_path / "led.json"
    p.write_text(json.dumps({"events": [
        {"input_tokens": 10, "output_tokens": 5},
        {"tokens": 7},
    ]}))
    out = sync_milestone_total(p)
    assert out["milestone_total_tokens"] == 22
    data = json.loads(p.read_text())
    assert data["milestone_total_tokens"] == 22

def test_sum():
    assert sum_event_tokens([{"input_tokens": 1, "output_tokens": 2}]) == 3
