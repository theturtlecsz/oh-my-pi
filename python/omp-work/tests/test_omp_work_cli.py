
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.__main__ import main

def test_pipeline_cli(capsys):
    rc = main(["pipeline", "--job-id", "cli1", "--query", "write-first", "--objective", "demo", "--no-enola", "--no-ledger"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["job_id"] == "cli1"

def test_headroom_cli(capsys):
    assert main(["headroom"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "spent_tokens" in data

def test_stall_cli(capsys):
    assert main(["stall-check", "--max-idle-minutes", "99999"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "stalled" in data
