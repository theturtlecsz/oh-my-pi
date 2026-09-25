import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.economy_effort import validate_effort_fields, require_effort_on_admit

def test_missing_effort():
    assert validate_effort_fields("do stuff")["ok"] is False

def test_e1_ok():
    assert validate_effort_fields("effort: E1\n")["ok"] is True

def test_require_raises():
    try:
        require_effort_on_admit("nope")
        assert False
    except ValueError:
        pass
