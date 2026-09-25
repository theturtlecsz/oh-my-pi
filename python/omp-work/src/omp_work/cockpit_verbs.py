"""Cockpit verbs: submit/status/cancel/resume/approve/evidence (W4).

Grok → Run Owner → WorkService-shaped job store. Not a separate OMP UI.
Durability is in-process for the day-30 demo harness; authority path is sealed packets.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import copy
import time
import uuid


TERMINAL = frozenset({"cancelled", "approved", "failed", "timed_out"})


@dataclass
class Job:
    job_id: str
    objective: str
    state: str = "submitted"
    checkpoint: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    callbacks_seen: set[str] = field(default_factory=set)
    effects: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    path: str = "grok->run_owner->workservice"


class Cockpit:
    """In-memory WorkService façade for verb + chaos proofs."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def submit(self, *, objective: str, job_id: str | None = None) -> dict[str, Any]:
        jid = job_id or f"job-{uuid.uuid4().hex[:10]}"
        if jid in self.jobs:
            # idempotent submit
            return {"ok": True, "job_id": jid, "state": self.jobs[jid].state, "deduped": True}
        job = Job(job_id=jid, objective=objective)
        job.checkpoint = {"phase": "submitted", "n": 0}
        job.evidence.append({"verb": "submit", "at": time.time()})
        self.jobs[jid] = job
        return {"ok": True, "job_id": jid, "state": job.state, "deduped": False}

    def status(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        job.evidence.append({"verb": "status", "at": time.time(), "state": job.state})
        return {
            "ok": True,
            "job_id": job_id,
            "state": job.state,
            "checkpoint": copy.deepcopy(job.checkpoint),
            "effects": job.effects,
            "path": job.path,
        }

    def cancel(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        if job.state in TERMINAL and job.state != "cancelled":
            return {"ok": False, "error": f"already terminal: {job.state}"}
        job.state = "cancelled"
        job.updated_at = time.time()
        job.evidence.append({"verb": "cancel", "at": time.time()})
        return {"ok": True, "job_id": job_id, "state": job.state}

    def resume(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        if job.state == "cancelled":
            return {"ok": False, "error": "cannot resume cancelled"}
        if job.state in ("approved", "failed", "timed_out"):
            return {"ok": False, "error": f"cannot resume terminal {job.state}"}
        # restore from checkpoint
        phase = job.checkpoint.get("phase", "submitted")
        job.state = "running" if phase != "submitted" else "running"
        job.updated_at = time.time()
        job.evidence.append({"verb": "resume", "at": time.time(), "from_checkpoint": copy.deepcopy(job.checkpoint)})
        return {"ok": True, "job_id": job_id, "state": job.state, "checkpoint": copy.deepcopy(job.checkpoint)}

    def approve(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        if job.state == "cancelled":
            return {"ok": False, "error": "cancelled"}
        job.state = "approved"
        job.updated_at = time.time()
        job.evidence.append({"verb": "approve", "at": time.time()})
        return {"ok": True, "job_id": job_id, "state": job.state}

    def evidence(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "state": job.state,
            "evidence": list(job.evidence),
            "checkpoint": copy.deepcopy(job.checkpoint),
            "effects": job.effects,
            "path": job.path,
        }

    def advance(self, *, job_id: str, phase: str) -> dict[str, Any]:
        """Internal step used by full-path / chaos (not a human cockpit verb)."""
        job = self._get(job_id)
        if job.state in TERMINAL:
            return {"ok": False, "error": f"terminal {job.state}"}
        job.state = "running"
        n = int(job.checkpoint.get("n", 0)) + 1
        job.checkpoint = {"phase": phase, "n": n}
        job.updated_at = time.time()
        return {"ok": True, "checkpoint": copy.deepcopy(job.checkpoint)}

    def apply_callback(self, *, job_id: str, callback_id: str) -> dict[str, Any]:
        """Idempotent effect: duplicate callback_id must not double-apply."""
        job = self._get(job_id)
        if callback_id in job.callbacks_seen:
            return {"ok": True, "applied": False, "deduped": True, "effects": job.effects}
        job.callbacks_seen.add(callback_id)
        job.effects += 1
        job.evidence.append({"verb": "callback", "callback_id": callback_id, "at": time.time()})
        return {"ok": True, "applied": True, "deduped": False, "effects": job.effects}

    def snapshot(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        return {
            "job_id": job.job_id,
            "objective": job.objective,
            "state": job.state,
            "checkpoint": copy.deepcopy(job.checkpoint),
            "effects": job.effects,
            "callbacks_seen": sorted(job.callbacks_seen),
            "evidence": list(job.evidence),
            "path": job.path,
        }

    def restore(self, *, snapshot: dict[str, Any]) -> dict[str, Any]:
        jid = snapshot["job_id"]
        job = Job(
            job_id=jid,
            objective=snapshot["objective"],
            state=snapshot.get("state", "running"),
            checkpoint=copy.deepcopy(snapshot.get("checkpoint") or {}),
            evidence=list(snapshot.get("evidence") or []),
            callbacks_seen=set(snapshot.get("callbacks_seen") or []),
            effects=int(snapshot.get("effects") or 0),
            path=snapshot.get("path", "grok->run_owner->workservice"),
        )
        self.jobs[jid] = job
        job.evidence.append({"verb": "restore", "at": time.time()})
        return {"ok": True, "job_id": jid, "state": job.state, "checkpoint": copy.deepcopy(job.checkpoint)}

    def mark_timeout(self, *, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        job.state = "timed_out"
        job.updated_at = time.time()
        job.evidence.append({"verb": "timeout", "at": time.time()})
        return {"ok": True, "job_id": job_id, "state": job.state}

    def _get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise KeyError(f"unknown job_id: {job_id}")
        return self.jobs[job_id]
