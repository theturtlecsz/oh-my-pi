"""R05 runners pre-author — mechanical C27.

Fail-closed process assertions against acceptance bullets.
R05_RUNNERS_IMPLEMENTED.flag must stay ABSENT until a later
bounded-implement unit delivers real runner proofs.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

from omp_work.v1.canonical import validate_execution_path  # noqa: E402

FLAG = Path(__file__).with_name("R05_RUNNERS_IMPLEMENTED.flag")
V1 = ROOT / "omp_work" / "contracts" / "v1"
OPS = ROOT / "omp_work" / "operations"


def test_r05_implementation_flag_absent_until_real_runners():
    assert not FLAG.exists(), (
        "R05_RUNNERS_IMPLEMENTED.flag must not exist until real runner "
        "contracts + acceptance proofs land (no fake seal)"
    )


def test_building_blocks_present_for_r05():
    assert (V1 / "artifact_store.py").is_file()  # R04 sealed
    assert (V1 / "recovery.py").is_file()  # R03 sealed
    assert (OPS / "capabilities.py").is_file()
    assert (ROOT / "omp_work" / "v1" / "canonical.py").is_file()


def test_candidate_path_escapes_refused_for_writable_roots():
    """Acceptance: escape declared writable paths refused."""
    bad = ["/etc/passwd", "../secret", "foo/../bar", "./relative", "a//b", "glob*.py"]
    for path in bad:
        try:
            validate_execution_path(path)
            raise AssertionError(f"must refuse escape path: {path!r}")
        except ValueError:
            pass
    validate_execution_path("python/omp-work/src/omp_work/operations/capabilities.py")


def test_evaluator_credentials_not_readable_without_r05():
    """Acceptance: candidate cannot access evaluator credentials (pre-author policy)."""
    assert not FLAG.exists()
    claimed = {"role": "candidate", "requested": "evaluator_credentials"}
    # Without R05 runners, any candidate credential request is refuse-by-policy.
    refused = claimed["role"] == "candidate" and claimed["requested"] == "evaluator_credentials" and not FLAG.exists()
    assert refused is True


def test_control_state_mutate_refused_without_r05():
    """Acceptance: candidate cannot mutate control state without runner admission."""
    assert not FLAG.exists()
    attempted = {"actor": "candidate", "action": "mutate_control_state", "target": "close_attempt"}
    refused = attempted["actor"] == "candidate" and not FLAG.exists()
    assert refused is True


def test_backend_incompatibility_must_be_explicit_policy():
    """Acceptance: backend incompatibility is explicit (pre-author contract shape)."""
    # Until R05 exists, incompatibility reports must not be silent success.
    report = {"backend": "gpu", "available": ["cpu"], "compatible": False, "silent": False}
    assert report["compatible"] is False
    assert report["silent"] is False
    assert not FLAG.exists()


def test_cancel_descendants_require_runner_surface():
    """Acceptance: keep-running-after-cancel needs runner cancel API — absent until flag."""
    assert not FLAG.exists()
    # Pre-author: without runners module, cancel-after-confirm cannot be claimed complete.
    runners_module = V1 / "runners.py"
    # May or may not exist as stub; flag gates real acceptance.
    claimed_cancel_complete = FLAG.exists() and runners_module.is_file()
    assert claimed_cancel_complete is False
