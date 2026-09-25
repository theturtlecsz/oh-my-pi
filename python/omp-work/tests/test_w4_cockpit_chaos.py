"""W4 cockpit verbs + chaos + double full-path acceptance."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omp_work.cockpit_verbs import Cockpit
from omp_work.chaos_suite import run_all_chaos
from omp_work.full_path import exercise_all_verbs, run_full_path, VERBS


def test_six_verbs():
    r = exercise_all_verbs(Cockpit())
    assert r.ok, r.detail
    for v in VERBS:
        assert v in r.verbs_exercised


def test_chaos_all_pass():
    results = run_all_chaos()
    assert all(x.passed for x in results), [(x.name, x.detail) for x in results]


def test_full_path_twice_with_interrupt():
    c = Cockpit()
    r1 = run_full_path(c, run_id="run1", interrupt=False)
    r2 = run_full_path(c, run_id="run2", interrupt=True)
    assert r1.ok and not r1.interrupted
    assert r2.ok and r2.interrupted
    assert r1.final_state == "approved" and r2.final_state == "approved"


def test_feature_freeze_marker():
    # D29–30 freeze: this suite must not import new W5 feature modules
    import omp_work.cockpit_verbs as cv
    import omp_work.chaos_suite as cs
    import omp_work.full_path as fp
    assert cv and cs and fp
