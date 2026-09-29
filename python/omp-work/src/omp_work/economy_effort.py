"""Effort gate helpers for policy-routed stage jobs (W1 kill-gate PROOF-05b).

Port of economy/effort-validate.ts rules. The gate is enforced at admission of
policy-routed stage jobs by ``omp_work.jobs.stage_admission.admit_stage_job``.
"""
from __future__ import annotations
import re
from typing import Any

_EFFORT_RE = re.compile(r"(?im)^\s*(?:\*\*)?effort(?:\*\*)?\s*[:=]\s*(?:\*\*)?\s*(E[0-4])\b")
_REASON_RE = re.compile(r"(?im)^\s*(?:\*\*)?effort_reason(?:\*\*)?\s*[:=]\s*[\"\']?(.+?)[\"\']?\s*$")
_BOILERPLATE_RE = re.compile(r"^(because|needed|required|n/?a|tbd|todo)\.?$", re.I)
_EFFORTS = ("E0", "E1", "E2", "E3", "E4")


def validate_effort(effort: object, reason: object) -> dict[str, Any]:
    """Validate a raw effort/reason pair without touching a packet string.

    ``effort`` must be exactly one of ``E0``..``E4`` (case-sensitive str); any
    other value, including ``None``, a non-str, or ``"e1"``, is invalid. A
    non-E1 effort needs a non-empty reason, and E4 needs a reason of at least
    20 characters that is not boilerplate.
    """
    if not isinstance(effort, str) or effort not in _EFFORTS:
        return {"ok": False, "message": "missing effort on job packet (require E0-E4; default E1)"}
    if effort != "E1" and not reason:
        return {"ok": False, "message": f"effort {effort} requires effort_reason", "effort": effort}
    if effort == "E4" and (
        not isinstance(reason, str) or len(reason) < 20 or _BOILERPLATE_RE.match(reason)
    ):
        return {"ok": False, "message": "E4 requires non-boilerplate effort_reason (length >= 20)", "effort": effort}
    return {"ok": True, "effort": effort, "reason": reason}


def validate_effort_fields(packet_text: str) -> dict[str, Any]:
    text = packet_text or ""
    m = _EFFORT_RE.search(text)
    effort = m.group(1).upper() if m else None
    rm = _REASON_RE.search(text)
    reason = (rm.group(1).strip() if rm else "")
    return validate_effort(effort, reason)


def require_effort_on_admit(packet_text: str) -> None:
    r = validate_effort_fields(packet_text)
    if not r.get("ok"):
        raise ValueError(r.get("message") or "effort validation failed")
