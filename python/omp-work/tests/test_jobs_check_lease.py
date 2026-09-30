"""Probe job lease derivation for jobs check (OMP-475-s02). No postgres.

Defends:
- A default lease is a quarter of the timeout, clamped to 1..30.
- An explicit lease passes through when it is an int in 1..3600 and below the timeout.
- A lease that is not a valid int below the timeout (equal to the timeout, zero,
  above 3600, or a bool) raises ValueError instead of being clamped.
"""

from __future__ import annotations

import pytest

from omp_work.jobs.process import probe_lease_seconds


@pytest.mark.parametrize(
    ("timeout", "lease", "expected"),
    [
        (300, None, 30),
        (40, None, 10),
        (2, None, 1),
        (300, 5, 5),
    ],
)
def test_probe_lease_seconds(
    timeout: float, lease: int | None, expected: int
) -> None:
    assert probe_lease_seconds(timeout, lease) == expected


@pytest.mark.parametrize(
    ("timeout", "lease"),
    [
        (300, 300),
        (300, 0),
        (5000, 4000),
        (300, True),
    ],
)
def test_probe_lease_seconds_rejects_invalid(
    timeout: float, lease: int | None
) -> None:
    with pytest.raises(ValueError):
        probe_lease_seconds(timeout, lease)
