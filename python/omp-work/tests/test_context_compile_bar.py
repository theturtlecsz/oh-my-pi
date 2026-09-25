
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.context_compile_bar import measure_compile

def test_measure():
    m = measure_compile(job_id="t", findings=["Prefer write-first"], objective="seal")
    assert m["tokens_est"] >= 1
    assert m["under_default_bar_1200"] is True
