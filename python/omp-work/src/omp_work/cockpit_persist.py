"""File-backed persistence for the in-process Cockpit (WorkService-shaped)."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json
import time

from omp_work.cockpit_verbs import Cockpit, Job

_MUTATORS = frozenset({
    "submit", "cancel", "approve", "resume", "restore", "mark_timeout",
    "advance", "checkpoint", "record_evidence", "fail",
})


class PersistentCockpit(Cockpit):
    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self._load()

    def _job_to_dict(self, job: Job) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "objective": job.objective,
            "state": job.state,
            "checkpoint": job.checkpoint,
            "evidence": list(job.evidence),
            "callbacks_seen": sorted(job.callbacks_seen),
            "effects": job.effects,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "path": job.path,
        }

    def _dict_to_job(self, data: dict[str, Any]) -> Job:
        return Job(
            job_id=data["job_id"],
            objective=data["objective"],
            state=data.get("state", "submitted"),
            checkpoint=dict(data.get("checkpoint") or {}),
            evidence=list(data.get("evidence") or []),
            callbacks_seen=set(data.get("callbacks_seen") or []),
            effects=int(data.get("effects") or 0),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            path=data.get("path", "grok->run_owner->workservice"),
        )

    def _flush(self) -> None:
        payload = {
            "version": 1,
            "jobs": {jid: self._job_to_dict(j) for jid, j in self.jobs.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)

    def _load(self) -> None:
        data = json.loads(self.path.read_text())
        for jid, jd in (data.get("jobs") or {}).items():
            self.jobs[jid] = self._dict_to_job(jd)

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name in _MUTATORS and callable(attr):
            def wrapped(*args, **kwargs):
                out = attr(*args, **kwargs)
                object.__getattribute__(self, "_flush")()
                return out
            return wrapped
        return attr
