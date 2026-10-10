"""Lock-map file for the qualification checks (OMP-417-s07-s08).

The parser rejects duplicate JSON keys. The recorded keys are
``UNATTENDED_CHECKS`` plus ``ROLLOUT_CHECK``, each once, and each mapped
test file exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omp_work.orchestrator.qualification import ROLLOUT_CHECK, UNATTENDED_CHECKS

_PACKAGE = Path(__file__).resolve().parents[1]
_MAP = _PACKAGE / "src" / "omp_work" / "orchestrator" / "lock-tests.json"

# ADR 0005 lock rows, plus the two qualification checks that are not locks.
_EXPECTED: dict[str, tuple[str, str]] = {
    "jobs-worker-kill-restart": ("OMP-417", "tests/test_jobs_check_midkill.py"),
    "lock-1": ("OMP-417", "tests/test_orchestrator_candidate_git.py"),
    "lock-2": ("OMP-417", "tests/test_orchestrator_worker_sandbox.py"),
    "lock-3": ("OMP-417", "tests/test_orchestrator_worker_sandbox.py"),
    "lock-4": ("OMP-421, OMP-417", "tests/test_task_capability.py"),
    "lock-5": ("OMP-417, OMP-400", "tests/test_orchestrator_kill_restart.py"),
    "lock-6": ("OMP-417, OMP-420", "tests/test_orchestrator_mission_e2e.py"),
    "lock-7": ("OMP-418, OMP-413, OMP-430", "tests/test_item_budget_stop.py"),
    "lock-8": ("OMP-431", "tests/test_egress_policy.py"),
    "lock-9": ("OMP-403, OMP-418", "tests/test_tier3_authorization_store.py"),
    "lock-10": (
        "OMP-405, OMP-430, OMP-425, OMP-417",
        "tests/test_jobs_stop_frozen_lease.py",
    ),
    "lock-11": ("OMP-417, OMP-415", "tests/test_orchestrator_audit_chain.py"),
    "lock-12": ("OMP-421, OMP-403", "tests/test_control_plane_fail_closed.py"),
    "omp16-rollout": ("OMP-417", "tests/test_orchestrator_rollout.py"),
}


def parse_lock_map(text: str) -> dict:
    """Parse a lock map and reject any object that repeats a key."""

    def pairs(seq: list[tuple[str, object]]) -> dict:
        keys = [key for key, _value in seq]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(seq)

    document = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(document, dict):
        raise ValueError("lock map must be an object")
    return document


def test_lock_map_keys_match_qualification_checks_once() -> None:
    """Keys equal UNATTENDED_CHECKS plus ROLLOUT_CHECK, each once, in that order."""
    document = parse_lock_map(_MAP.read_text(encoding="utf-8"))
    locks = document["locks"]
    assert tuple(locks) == UNATTENDED_CHECKS + (ROLLOUT_CHECK,)
    assert len(locks) == len(set(locks))
    for key, (item, test) in _EXPECTED.items():
        assert locks[key]["item"] == item
        assert locks[key]["test"] == test
        assert (_PACKAGE / test).is_file(), test


def test_lock_map_parser_rejects_duplicate_keys() -> None:
    """A repeated key inside locks is refused."""
    text = json.dumps(
        {
            "locks": {
                "lock-1": {"item": "OMP-417", "test": "tests/test_orchestrator_candidate_git.py"}
            }
        }
    )
    # json.dumps cannot emit a duplicate; splice a second lock-1 into the object.
    duplicate = text.replace(
        '"lock-1":',
        '"lock-1": {"item": "other", "test": "tests/missing.py"}, "lock-1":',
        1,
    )
    with pytest.raises(ValueError, match="duplicate key"):
        parse_lock_map(duplicate)
