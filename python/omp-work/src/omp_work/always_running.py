"""Detect stalled post-program queue from heartbeat + UNIT-QUEUE mtimes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import os
import time

ACTIVE = Path(
    os.environ.get("OMP_ECONOMY_ACTIVE_DIR") or (Path.home() / ".codex/workflows/economy/ACTIVE")
)


@dataclass(frozen=True)
class StallCheck:
    stalled: bool
    reason: str
    age_minutes: float
    next_job_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stalled": self.stalled,
            "reason": self.reason,
            "age_minutes": self.age_minutes,
            "next_job_id": self.next_job_id,
        }


def check_stall(
    *,
    max_idle_minutes: float = 20.0,
    now: float | None = None,
    active_dir: Path | str | None = None,
) -> StallCheck:
    now = time.time() if now is None else now
    active = (
        Path(active_dir)
        if active_dir is not None
        else Path(os.environ.get("OMP_ECONOMY_ACTIVE_DIR") or ACTIVE)
    )
    qpath = active / "UNIT-QUEUE.json"
    if not qpath.is_file():
        return StallCheck(True, "missing UNIT-QUEUE", 0.0, None)
    q = json.loads(qpath.read_text())
    if q.get("halt") is True:
        return StallCheck(False, "halted", 0.0, q.get("next_job_id"))
    age_min = (now - qpath.stat().st_mtime) / 60.0
    nxt = q.get("next_job_id") or q.get("admitted_next")
    if age_min >= max_idle_minutes:
        return StallCheck(True, "queue_file_idle", age_min, nxt)
    return StallCheck(False, "fresh", age_min, nxt)
