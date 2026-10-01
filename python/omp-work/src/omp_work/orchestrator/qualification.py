"""Deterministic release qualification gate for one orchestrator step (OMP-417).

``qualified`` decides whether the checks a release must pass are recorded as
passing for that exact release, and ``max_workers`` reads the recorded rollout
result that governs concurrency. Both are pure: the release is passed in, the
record is passed in, and nothing is read from disk or the environment.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "ATTENDED_CHECKS",
    "ROLLOUT_CHECK",
    "UNATTENDED_CHECKS",
    "CheckRecord",
    "max_workers",
    "qualified",
]

# A check record maps a check id to its {"result": ..., "release": ...} entry;
# None means no entries were recorded.
CheckRecord = Mapping[str, Mapping[str, str]]

# The kill-restart proof runs whether or not an operator is watching. An
# unattended run additionally needs every lock proof.
ATTENDED_CHECKS: tuple[str, ...] = ("jobs-worker-kill-restart",)
UNATTENDED_CHECKS: tuple[str, ...] = ATTENDED_CHECKS + tuple(f"lock-{n}" for n in range(1, 13))
ROLLOUT_CHECK = "omp16-rollout"

_PASS = "pass"


def _entry_passed(entry: object, release: str | None) -> bool:
    """Whether an entry is a well-formed pass; ``release`` None skips the check."""
    if not isinstance(entry, Mapping) or entry.get("result") != _PASS:
        return False
    return release is None or entry.get("release") == release


def qualified(
    record: CheckRecord | None,
    *,
    unattended: bool,
    release: str,
) -> tuple[bool, tuple[str, ...]]:
    """Whether every required check passed for ``release``, and which did not.

    The required set is ``UNATTENDED_CHECKS`` for an unattended run and
    ``ATTENDED_CHECKS`` otherwise. Returns ``(True, ())`` when nothing is
    missing, else ``(False, missing)`` where ``missing`` lists the required
    check ids that are absent, failed, malformed, or recorded for another
    release, in required order.
    """
    required = UNATTENDED_CHECKS if unattended else ATTENDED_CHECKS
    entries = record if record is not None else {}
    missing = tuple(check for check in required if not _entry_passed(entries.get(check), release))
    return (not missing, missing)


def max_workers(configured: int, record: CheckRecord | None) -> int:
    """``configured`` workers when the recorded rollout passed, else one."""
    entry = None if record is None else record.get(ROLLOUT_CHECK)
    return configured if _entry_passed(entry, None) else 1
