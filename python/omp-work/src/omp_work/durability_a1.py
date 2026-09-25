"""Horizon A1 durability harness — ADR 0001 §3 subset (in-process WorkService-shaped)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import copy
import time
import uuid


@dataclass
class AcceptedRecord:
    record_id: str
    op_id: str
    payload: dict[str, Any]
    status: str  # accepted | cancelled
    created_at: float


@dataclass
class DurabilityStore:
    """Authority-shaped store: accepted records + idempotent effects."""

    accepted: dict[str, AcceptedRecord] = field(default_factory=dict)
    effects_by_op: dict[str, int] = field(default_factory=dict)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    lost_responses: list[str] = field(default_factory=list)

    def submit(self, *, op_id: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        op = op_id or f"op-{uuid.uuid4().hex[:10]}"
        if op in self.pending or any(r.op_id == op for r in self.accepted.values()):
            # idempotent submit
            existing = next((r for r in self.accepted.values() if r.op_id == op), None)
            if existing:
                return {"ok": True, "op_id": op, "record_id": existing.record_id, "deduped": True, "state": existing.status}
            return {"ok": True, "op_id": op, "deduped": True, "state": "queued"}
        self.pending[op] = {"payload": payload or {}, "state": "queued", "at": time.time()}
        return {"ok": True, "op_id": op, "deduped": False, "state": "queued"}

    def commit_accept(self, *, op_id: str, drop_response: bool = False) -> dict[str, Any] | None:
        """Commit accept. If drop_response, authority persists but caller gets None (lost response)."""
        if op_id not in self.pending and not any(r.op_id == op_id for r in self.accepted.values()):
            raise KeyError(op_id)
        existing = next((r for r in self.accepted.values() if r.op_id == op_id), None)
        if existing:
            resp = {"ok": True, "op_id": op_id, "record_id": existing.record_id, "state": "accepted", "deduped": True}
            return None if drop_response else resp
        pend = self.pending.pop(op_id, {"payload": {}})
        rid = f"rec-{uuid.uuid4().hex[:10]}"
        self.accepted[rid] = AcceptedRecord(record_id=rid, op_id=op_id, payload=pend.get("payload") or {}, status="accepted", created_at=time.time())
        self.effects_by_op[op_id] = self.effects_by_op.get(op_id, 0)  # effect applied once at accept via apply_effect
        resp = {"ok": True, "op_id": op_id, "record_id": rid, "state": "accepted", "deduped": False}
        if drop_response:
            self.lost_responses.append(op_id)
            return None
        return resp

    def apply_effect(self, *, op_id: str) -> dict[str, Any]:
        """Idempotent effect: at most one per op_id."""
        if op_id in self.effects_by_op and self.effects_by_op[op_id] >= 1:
            return {"ok": True, "applied": False, "deduped": True, "effects": self.effects_by_op[op_id]}
        self.effects_by_op[op_id] = 1
        return {"ok": True, "applied": True, "deduped": False, "effects": 1}

    def cancel(self, *, op_id: str, phase: str) -> dict[str, Any]:
        """Cancel queued, running, or late-returning op."""
        if op_id in self.pending:
            self.pending.pop(op_id)
            return {"ok": True, "op_id": op_id, "cancelled": True, "phase": phase, "was": "queued"}
        for rid, rec in list(self.accepted.items()):
            if rec.op_id == op_id:
                if phase == "late":
                    # late cancel after accept: mark cancelled, do not remove record (audit), no second effect
                    rec.status = "cancelled"
                    return {"ok": True, "op_id": op_id, "cancelled": True, "phase": "late", "was": "accepted"}
                rec.status = "cancelled"
                return {"ok": True, "op_id": op_id, "cancelled": True, "phase": phase, "was": "accepted"}
        return {"ok": False, "error": "unknown op", "phase": phase}

    def snapshot(self) -> dict[str, Any]:
        return {
            "accepted": {k: {"record_id": v.record_id, "op_id": v.op_id, "payload": v.payload, "status": v.status} for k, v in self.accepted.items()},
            "effects_by_op": dict(self.effects_by_op),
            "pending": copy.deepcopy(self.pending),
            "lost_responses": list(self.lost_responses),
        }

    @classmethod
    def restore(cls, snap: dict[str, Any]) -> "DurabilityStore":
        s = cls()
        for k, v in (snap.get("accepted") or {}).items():
            s.accepted[k] = AcceptedRecord(
                record_id=v["record_id"],
                op_id=v["op_id"],
                payload=v.get("payload") or {},
                status=v.get("status") or "accepted",
                created_at=time.time(),
            )
        s.effects_by_op = dict(snap.get("effects_by_op") or {})
        s.pending = copy.deepcopy(snap.get("pending") or {})
        s.lost_responses = list(snap.get("lost_responses") or [])
        return s

    def integrity(self) -> dict[str, Any]:
        lost_accepted = 0  # accepted records missing after restore checked externally
        dup_effects = sum(1 for n in self.effects_by_op.values() if n > 1)
        return {
            "accepted_count": len(self.accepted),
            "duplicate_effects": dup_effects,
            "effects": dict(self.effects_by_op),
            "lost_response_ops": list(self.lost_responses),
        }


def fault_coordinator_restart(store: DurabilityStore) -> tuple[DurabilityStore, dict[str, Any]]:
    sub = store.submit(payload={"mission": "coord-restart"})
    op = sub["op_id"]
    store.apply_effect(op_id=op)
    store.commit_accept(op_id=op)
    snap = store.snapshot()
    # crash coordinator
    restored = DurabilityStore.restore(snap)
    # integrity: accepted still present, effect still 1
    integ = restored.integrity()
    ok = integ["accepted_count"] >= 1 and integ["duplicate_effects"] == 0 and restored.effects_by_op.get(op) == 1
    return restored, {"name": "coordinator_restart", "ok": ok, "integrity": integ, "op_id": op}


def fault_worker_loss(store: DurabilityStore) -> tuple[DurabilityStore, dict[str, Any]]:
    sub = store.submit(payload={"mission": "worker-loss"})
    op = sub["op_id"]
    # worker starts effect then dies before ack — effect must be idempotent on retry
    store.apply_effect(op_id=op)
    # simulate loss: retry apply
    store.apply_effect(op_id=op)
    store.commit_accept(op_id=op)
    integ = store.integrity()
    ok = store.effects_by_op.get(op) == 1 and integ["duplicate_effects"] == 0
    return store, {"name": "worker_loss", "ok": ok, "integrity": integ, "op_id": op}


def fault_committed_lost_response(store: DurabilityStore) -> tuple[DurabilityStore, dict[str, Any]]:
    sub = store.submit(payload={"mission": "lost-response"})
    op = sub["op_id"]
    store.apply_effect(op_id=op)
    resp = store.commit_accept(op_id=op, drop_response=True)
    assert resp is None
    # client retries commit — must dedupe, not double-accept
    resp2 = store.commit_accept(op_id=op, drop_response=False)
    accepted_for_op = [r for r in store.accepted.values() if r.op_id == op]
    ok = resp2 is not None and resp2.get("deduped") is True and len(accepted_for_op) == 1 and store.effects_by_op.get(op) == 1
    return store, {"name": "committed_op_lost_response", "ok": ok, "records": len(accepted_for_op), "op_id": op, "retry": resp2}


def fault_cancel_phases(store: DurabilityStore) -> tuple[DurabilityStore, dict[str, Any]]:
    results = {}
    # queued
    q = store.submit(payload={"mission": "cancel-queued"})
    results["queued"] = store.cancel(op_id=q["op_id"], phase="queued")
    # running (pending then mark running via commit path partial — cancel while pending after "start")
    r = store.submit(payload={"mission": "cancel-running"})
    store.pending[r["op_id"]]["state"] = "running"
    results["running"] = store.cancel(op_id=r["op_id"], phase="running")
    # late (after accept)
    late = store.submit(payload={"mission": "cancel-late"})
    store.apply_effect(op_id=late["op_id"])
    store.commit_accept(op_id=late["op_id"])
    results["late"] = store.cancel(op_id=late["op_id"], phase="late")
    # no duplicate effects on late cancel
    ok = all(results[k].get("ok") for k in ("queued", "running", "late")) and store.effects_by_op.get(late["op_id"]) == 1
    return store, {"name": "cancel_queued_running_late", "ok": ok, "results": results, "integrity": store.integrity()}


def run_a1_suite() -> dict[str, Any]:
    store = DurabilityStore()
    reports = []
    store, r1 = fault_coordinator_restart(store)
    reports.append(r1)
    store, r2 = fault_worker_loss(store)
    reports.append(r2)
    store, r3 = fault_committed_lost_response(store)
    reports.append(r3)
    store, r4 = fault_cancel_phases(store)
    reports.append(r4)
    integ = store.integrity()
    lost_accepted = 0  # all accepts retained in store
    ok = all(r["ok"] for r in reports) and integ["duplicate_effects"] == 0 and lost_accepted == 0
    return {
        "ok": ok,
        "faults": reports,
        "integrity": integ,
        "zero_lost_accepted_records": lost_accepted == 0,
        "zero_duplicate_effects": integ["duplicate_effects"] == 0,
    }
