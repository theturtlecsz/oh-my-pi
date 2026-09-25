"""Full cockpit path runner — twice including interrupt (W4 D29–30)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from omp_work.cockpit_verbs import Cockpit


VERBS = ("submit", "status", "cancel", "resume", "approve", "evidence")


@dataclass
class FullPathResult:
    run_id: str
    interrupted: bool
    verbs_exercised: list[str]
    final_state: str
    evidence_count: int
    ok: bool
    detail: str = ""
    trail: list[dict[str, Any]] = field(default_factory=list)


def exercise_all_verbs(c: Cockpit) -> FullPathResult:
    """Dedicated verb surface proof (uses cancel on a throwaway job so approve path stays clean)."""
    trail: list[dict[str, Any]] = []
    verbs: list[str] = []

    # cancel path
    s = c.submit(objective="verb-cancel-path")
    j_cancel = s["job_id"]
    trail.append({"submit_cancel_path": s})
    verbs.append("submit")
    trail.append({"status": c.status(job_id=j_cancel)})
    verbs.append("status")
    trail.append({"cancel": c.cancel(job_id=j_cancel)})
    verbs.append("cancel")

    # happy path with resume + approve + evidence
    s2 = c.submit(objective="verb-happy-path")
    jid = s2["job_id"]
    c.advance(job_id=jid, phase="implement")
    snap = c.snapshot(job_id=jid)
    del c.jobs[jid]
    trail.append({"resume": c.resume(job_id=jid)} if False else {"restore_then_resume": None})
    # after crash, restore then resume
    c.restore(snapshot=snap)
    r = c.resume(job_id=jid)
    trail.append({"resume": r})
    verbs.append("resume")
    a = c.approve(job_id=jid)
    trail.append({"approve": a})
    verbs.append("approve")
    e = c.evidence(job_id=jid)
    trail.append({"evidence": {"count": len(e["evidence"]), "state": e["state"]}})
    verbs.append("evidence")

    # ensure submit counted once already; all six present
    missing = [v for v in VERBS if v not in verbs]
    ok = not missing and a.get("ok") and e.get("ok")
    return FullPathResult(
        run_id="verbs",
        interrupted=False,
        verbs_exercised=verbs,
        final_state=e.get("state", "?"),
        evidence_count=len(e.get("evidence") or []),
        ok=ok,
        detail=f"missing={missing}",
        trail=trail,
    )


def run_full_path(c: Cockpit, *, run_id: str, interrupt: bool) -> FullPathResult:
    trail: list[dict[str, Any]] = []
    verbs: list[str] = []
    s = c.submit(objective=f"full-path {run_id}")
    jid = s["job_id"]
    trail.append({"submit": s})
    verbs.append("submit")
    trail.append({"status": c.status(job_id=jid)})
    verbs.append("status")
    c.advance(job_id=jid, phase="intake")
    c.advance(job_id=jid, phase="implement")

    interrupted = False
    if interrupt:
        interrupted = True
        snap = c.snapshot(job_id=jid)
        del c.jobs[jid]  # crash mid-flight
        c.restore(snapshot=snap)
        r = c.resume(job_id=jid)
        trail.append({"interrupt_restore_resume": r})
        verbs.append("resume")
    else:
        # still exercise resume from checkpoint without crash
        r = c.resume(job_id=jid)
        trail.append({"resume": r})
        verbs.append("resume")

    c.advance(job_id=jid, phase="review")
    # apply one callback (effect)
    c.apply_callback(job_id=jid, callback_id=f"done-{run_id}")
    a = c.approve(job_id=jid)
    trail.append({"approve": a})
    verbs.append("approve")
    e = c.evidence(job_id=jid)
    trail.append({"evidence": {"count": len(e["evidence"]), "state": e["state"]}})
    verbs.append("evidence")

    # cancel exercised on a sibling job so this path can approve
    sc = c.submit(objective=f"cancel-sidecar {run_id}")
    c.cancel(job_id=sc["job_id"])
    verbs.append("cancel")

    ok = (
        a.get("ok")
        and e.get("state") == "approved"
        and all(v in verbs for v in ("submit", "status", "cancel", "resume", "approve", "evidence"))
        and (interrupted if interrupt else True)
    )
    return FullPathResult(
        run_id=run_id,
        interrupted=interrupted,
        verbs_exercised=sorted(set(verbs)),
        final_state=e.get("state", "?"),
        evidence_count=len(e.get("evidence") or []),
        ok=ok,
        detail=f"interrupt={interrupted}",
        trail=trail,
    )
