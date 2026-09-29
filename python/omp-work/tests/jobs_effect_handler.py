"""Effect handler for native jobs crash recovery verification (OMP-400-s03).

Appends job_id to a file, sleeps; observe reads it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from omp_work.jobs.worker import Settlement

__all__ = ["JobsEffectHandler", "factory"]


class JobsEffectHandler:
    """Handler that records execution by appending job_id to a file."""

    def __init__(
        self,
        file_path: str | Path | None = None,
        sleep_seconds: float = 0.5,
    ) -> None:
        raw_path = file_path or os.environ.get(
            "JOBS_EFFECT_FILE", "/tmp/jobs_effect.txt"
        )
        self.file_path = Path(raw_path)
        self.sleep_seconds = float(sleep_seconds)

    def run(self, ctx: Any, job: dict[str, Any]) -> Settlement:
        job_id = str(job["job_id"])
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(f"{job_id}\n")
            f.flush()
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return Settlement(outcome="succeeded", receipts=[])

    def observe(self, ctx: Any, job: dict[str, Any]) -> Settlement | None:
        job_id = str(job["job_id"])
        if self.file_path.exists():
            lines = [
                line.strip()
                for line in self.file_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if job_id in lines:
                return Settlement(outcome="succeeded", receipts=[])
        return None


def factory(
    file_path: str | None = None,
    sleep_seconds: float = 0.5,
    **_kwargs: Any,
) -> JobsEffectHandler:
    return JobsEffectHandler(file_path=file_path, sleep_seconds=sleep_seconds)
