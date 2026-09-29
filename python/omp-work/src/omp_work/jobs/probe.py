"""Probe job handler for health checks and substrate verification (OMP-400-s03).

Provides ProbeHandler and factory for omp.probe jobs.
"""

from __future__ import annotations

import time
from typing import Any

from omp_work.jobs.worker import Settlement

__all__ = ["ProbeHandler", "factory"]


class ProbeHandler:
    """Handler for omp.probe jobs."""

    def __init__(self, sleep_seconds: float = 0.0) -> None:
        self.sleep_seconds = float(sleep_seconds)

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return Settlement(outcome="succeeded", receipts=[])

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return Settlement(outcome="succeeded", receipts=[])


def factory(sleep_seconds: float = 0.0, **_kwargs: Any) -> ProbeHandler:
    return ProbeHandler(sleep_seconds=sleep_seconds)
