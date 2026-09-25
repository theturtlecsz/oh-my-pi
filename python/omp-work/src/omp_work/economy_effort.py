"""Soft effort gate helpers for WorkService admit (W1 kill-gate PROOF-05b).

Port of economy/effort-validate.ts rules — soft until wired into admit path.
"""
from __future__ import annotations
import re
from typing import Any

_EFFORT_RE = re.compile(r"(?im)^\s*(?:\*\*)?effort(?:\*\*)?\s*[:=]\s*(?:\*\*)?\s*(E[0-4])\b")
_REASON_RE = re.compile(r"(?im)^\s*(?:\*\*)?effort_reason(?:\*\*)?\s*[:=]\s*[\"\']?(.+?)[\"\']?\s*$")


def validate_effort_fields(packet_text: str) -> dict[str, Any]:
    text = packet_text or ""
    m = _EFFORT_RE.search(text)
    effort = m.group(1).upper() if m else None
    rm = _REASON_RE.search(text)
    reason = (rm.group(1).strip() if rm else "")
    if not effort:
        return {"ok": False, "message": "missing effort on job packet (require E0-E4; default E1)"}
    if effort != "E1" and not reason:
        return {"ok": False, "message": f"effort {effort} requires effort_reason", "effort": effort}
    if effort == "E4":
        if len(reason) < 20 or re.match(r"^(because|needed|required|n/?a|tbd|todo)\.?$", reason, re.I):
            return {"ok": False, "message": "E4 requires non-boilerplate effort_reason (length >= 20)", "effort": effort}
    return {"ok": True, "effort": effort, "reason": reason}


def require_effort_on_admit(packet_text: str) -> None:
    r = validate_effort_fields(packet_text)
    if not r.get("ok"):
        raise ValueError(r.get("message") or "effort validation failed")
