"""File-backed Enola-shaped traces for WorkService jobs."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time

from omp_work.enola_adapter import EnolaTraceResult, reason_trace


@dataclass
class EnolaStore:
    path: Path
    traces: dict[str, dict[str, Any]]

    @classmethod
    def open(cls, path: Path | str) -> "EnolaStore":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        traces: dict[str, dict[str, Any]] = {}
        if p.is_file():
            data = json.loads(p.read_text())
            traces = dict(data.get("traces") or {})
        store = cls(path=p, traces=traces)
        if not p.is_file():
            store.flush()
        return store

    def flush(self) -> None:
        payload = {"version": 1, "updated_at": time.time(), "traces": self.traces}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)

    def record_trace(
        self,
        *,
        job_id: str,
        goal: str,
        facts: list[str] | None = None,
    ) -> EnolaTraceResult:
        if not job_id.strip():
            raise ValueError("job_id required")
        result = reason_trace(goal=goal, job_id=job_id, facts=facts)
        key = f"{job_id}:{len(self.traces) + 1}"
        self.traces[key] = {
            "job_id": job_id,
            "goal": goal,
            "facts": list(facts or []),
            "finding": result.finding(),
            "steps": list(result.steps),
            "adapter": result.adapter,
            "at": time.time(),
        }
        self.flush()
        return result

    def for_job(self, job_id: str) -> list[dict[str, Any]]:
        return [v for v in self.traces.values() if v.get("job_id") == job_id]
