"""Tests for the pure release qualification gate (OMP-417-s01-s05)."""

from __future__ import annotations

from omp_work.orchestrator.qualification import (
    ATTENDED_CHECKS,
    ROLLOUT_CHECK,
    UNATTENDED_CHECKS,
    max_workers,
    qualified,
)

RELEASE = "omp-2026.10.1"
OTHER_RELEASE = "omp-2026.10.2"


def passing(check_ids: tuple[str, ...], *, release: str = RELEASE) -> dict[str, dict[str, str]]:
    return {check: {"result": "pass", "release": release} for check in check_ids}


# ==============================================================================
# The required set depends on whether the run is attended.
# ==============================================================================


def test_attended_run_needs_only_the_kill_restart_check() -> None:
    record = passing(ATTENDED_CHECKS)
    assert qualified(record, unattended=False, release=RELEASE) == (True, ())


def test_unattended_run_requires_every_lock_check_in_order() -> None:
    record = passing(ATTENDED_CHECKS)
    ok, missing = qualified(record, unattended=True, release=RELEASE)
    assert ok is False
    assert missing == tuple(f"lock-{n}" for n in range(1, 13))


def test_unattended_run_passes_with_all_thirteen_checks() -> None:
    assert len(UNATTENDED_CHECKS) == 13
    record = passing(UNATTENDED_CHECKS)
    assert qualified(record, unattended=True, release=RELEASE) == (True, ())


# ==============================================================================
# Each way a required check can fail to qualify is reported as missing.
# ==============================================================================


def test_failed_check_is_missing() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ATTENDED_CHECKS[0]] = {"result": "fail", "release": RELEASE}
    assert qualified(record, unattended=False, release=RELEASE) == (False, ATTENDED_CHECKS)


def test_missing_check_is_missing() -> None:
    assert qualified({}, unattended=False, release=RELEASE) == (False, ATTENDED_CHECKS)


def test_check_for_another_release_is_missing() -> None:
    record = passing(ATTENDED_CHECKS, release=OTHER_RELEASE)
    assert qualified(record, unattended=False, release=RELEASE) == (False, ATTENDED_CHECKS)


def test_no_record_is_missing_every_required_check() -> None:
    assert qualified(None, unattended=False, release=RELEASE) == (False, ATTENDED_CHECKS)


def test_unrelated_entries_do_not_qualify_a_missing_check() -> None:
    record = passing(ATTENDED_CHECKS)
    record["some-other-check"] = {"result": "pass", "release": RELEASE}
    record[ROLLOUT_CHECK] = {"result": "pass", "release": RELEASE}
    assert qualified(record, unattended=False, release=RELEASE) == (True, ())


# ==============================================================================
# max_workers reads only the rollout result; a missing or failed one means one.
# ==============================================================================


def test_max_workers_is_one_without_a_rollout_entry() -> None:
    assert max_workers(4, passing(ATTENDED_CHECKS)) == 1
    assert max_workers(4, None) == 1


def test_max_workers_is_one_when_rollout_failed() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "fail", "release": RELEASE}
    assert max_workers(4, record) == 1


def test_max_workers_uses_configured_when_rollout_passed() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "pass", "release": OTHER_RELEASE}
    assert max_workers(4, record) == 4


# ==============================================================================
# An unattended run with no release identity refuses a complete proof outright.
# ==============================================================================


def test_unattended_empty_release_refuses_labelled_complete_proof() -> None:
    record = passing(UNATTENDED_CHECKS)
    assert qualified(record, unattended=True, release="") == (False, UNATTENDED_CHECKS)


def test_unattended_empty_release_refuses_all_empty_complete_proof() -> None:
    record = passing(UNATTENDED_CHECKS, release="")
    assert qualified(record, unattended=True, release="") == (False, UNATTENDED_CHECKS)


def test_attended_empty_release_does_not_use_the_unattended_refusal() -> None:
    record = passing(ATTENDED_CHECKS, release="")
    assert qualified(record, unattended=False, release="") == (True, ())


# ==============================================================================
# max_workers release filtering: None keeps any rollout, strings match exactly.
# ==============================================================================


def test_max_workers_release_selects_the_matching_rollout() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "pass", "release": "r0"}
    assert max_workers(4, record, "r0") == 4
    assert max_workers(4, record, "r1") == 1


def test_max_workers_matching_release_uses_configured() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "pass", "release": "r1"}
    assert max_workers(4, record, "r1") == 4


def test_max_workers_explicit_empty_release_requires_an_empty_label() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "pass", "release": ""}
    assert max_workers(4, record, "") == 4
    record[ROLLOUT_CHECK] = {"result": "pass", "release": "r1"}
    assert max_workers(4, record, "") == 1
    record[ROLLOUT_CHECK] = {"result": "pass"}
    assert max_workers(4, record, "") == 1


def test_max_workers_none_release_ignores_the_rollout_label() -> None:
    record = passing(ATTENDED_CHECKS)
    record[ROLLOUT_CHECK] = {"result": "pass", "release": ""}
    assert max_workers(4, record, None) == 4
    record[ROLLOUT_CHECK] = {"result": "pass", "release": "r1"}
    assert max_workers(4, record) == 4
