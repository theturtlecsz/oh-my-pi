"""Probe job handler for health checks and substrate verification (OMP-400-s03).

Provides ProbeHandler and factory for omp.probe jobs. Jobs have no payload
column, so a per-job probe sleep is encoded in the job id (OMP-475-s01).
"""

from __future__ import annotations

import math
import re
import time
from typing import Any
from uuid import uuid4

from omp_work.jobs.worker import Settlement

__all__ = ["ProbeHandler", "factory", "probe_job_id", "probe_sleep_seconds"]

# probe-sleep<ms>-<uuid4>, ms a canonical decimal (no sign, no leading zeros).
_ENCODED_SLEEP = re.compile(
    r"^probe-sleep(0|[1-9][0-9]*)-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def probe_job_id(sleep_seconds: float = 0.0) -> str:
    """Return ``probe-<uuid4>``, or ``probe-sleep<ms>-<uuid4>`` when sleep is non-zero.

    ``ms`` is ``round(sleep_seconds * 1000)``. Negative, NaN, and infinite
    values raise ValueError.
    """
    try:
        seconds = float(sleep_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("sleep_seconds must be a finite number >= 0") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("sleep_seconds must be a finite number >= 0")
    token = uuid4()
    if seconds == 0.0:
        return f"probe-{token}"
    ms = round(seconds * 1000)
    return f"probe-sleep{ms}-{token}"


def probe_sleep_seconds(job_id: str) -> float | None:
    """Return the sleep encoded in ``job_id`` (seconds), or None if absent or malformed."""
    if not isinstance(job_id, str):
        return None
    match = _ENCODED_SLEEP.fullmatch(job_id)
    if match is None:
        return None
    return int(match.group(1)) / 1000


class ProbeHandler:
    """Handler for omp.probe jobs."""

    def __init__(self, sleep_seconds: float = 0.0) -> None:
        self.sleep_seconds = float(sleep_seconds)

    def _delay_seconds(self, job: dict[str, Any]) -> float:
        encoded = probe_sleep_seconds(job["job_id"])
        if encoded is not None:
            return encoded
        return self.sleep_seconds

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        delay = self._delay_seconds(job)
        if delay > 0:
            time.sleep(delay)
        return Settlement(outcome="succeeded", receipts=[])

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        delay = self._delay_seconds(job)
        if delay > 0:
            time.sleep(delay)
        return Settlement(outcome="succeeded", receipts=[])


def factory(sleep_seconds: float = 0.0, **_kwargs: Any) -> ProbeHandler:
    return ProbeHandler(sleep_seconds=sleep_seconds)
