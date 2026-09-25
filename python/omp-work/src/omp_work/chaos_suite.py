"""W4 chaos proofs against Cockpit façade."""
from __future__ import annotations
from dataclasses import dataclass
from omp_work.cockpit_verbs import Cockpit


@dataclass
class ChaosResult:
    name: str
    passed: bool
    detail: str


def chaos_crash_reconnect(c: Cockpit) -> ChaosResult:
    sub = c.submit(objective="crash-reconnect demo")
    jid = sub["job_id"]
    c.advance(job_id=jid, phase="mid")
    snap = c.snapshot(job_id=jid)
    # simulate crash: drop in-memory job
    del c.jobs[jid]
    assert jid not in c.jobs
    c.restore(snapshot=snap)
    st = c.status(job_id=jid)
    ok = st["state"] == "running" and st["checkpoint"].get("phase") == "mid"
    return ChaosResult("crash_reconnect", ok, f"state={st['state']} checkpoint={st['checkpoint']}")


def chaos_timeout(c: Cockpit) -> ChaosResult:
    sub = c.submit(objective="timeout demo")
    jid = sub["job_id"]
    c.advance(job_id=jid, phase="working")
    c.mark_timeout(job_id=jid)
    st = c.status(job_id=jid)
    ok = st["state"] == "timed_out"
    # resume must fail on timed_out
    r = c.resume(job_id=jid)
    ok = ok and r.get("ok") is False
    return ChaosResult("timeout", ok, f"state={st['state']} resume_ok={r.get('ok')}")


def chaos_dup_callback(c: Cockpit) -> ChaosResult:
    sub = c.submit(objective="dup callback demo")
    jid = sub["job_id"]
    a1 = c.apply_callback(job_id=jid, callback_id="cb-1")
    a2 = c.apply_callback(job_id=jid, callback_id="cb-1")
    ok = a1["applied"] is True and a2["deduped"] is True and c.status(job_id=jid)["effects"] == 1
    return ChaosResult("dup_callback", ok, f"effects={c.status(job_id=jid)['effects']}")


def chaos_restore_checkpoint(c: Cockpit) -> ChaosResult:
    sub = c.submit(objective="restore demo")
    jid = sub["job_id"]
    c.advance(job_id=jid, phase="step-1")
    c.advance(job_id=jid, phase="step-2")
    snap = c.snapshot(job_id=jid)
    # mutate away
    c.advance(job_id=jid, phase="step-3-wrong")
    c.restore(snapshot=snap)
    st = c.status(job_id=jid)
    ok = st["checkpoint"].get("phase") == "step-2" and st["checkpoint"].get("n") == 2
    return ChaosResult("restore_checkpoint", ok, f"checkpoint={st['checkpoint']}")


def run_all_chaos(c: Cockpit | None = None) -> list[ChaosResult]:
    c = c or Cockpit()
    return [
        chaos_crash_reconnect(c),
        chaos_timeout(c),
        chaos_dup_callback(c),
        chaos_restore_checkpoint(c),
    ]
