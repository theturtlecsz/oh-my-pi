"""Thin Enola adapter for WorkService jobs (W3). Not Cursor agent memory."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnolaTraceResult:
    adapter: str
    goal: str
    steps: list[dict[str, Any]]
    invoked_via: str

    def finding(self) -> str:
        for s in self.steps:
            if s.get("kind") == "conclusion":
                return str(s.get("text", ""))
        return ""


def reason_trace(*, goal: str, job_id: str, facts: list[str] | None = None) -> EnolaTraceResult:
    if not job_id.strip():
        raise ValueError("job_id required — adapters run via WorkService, not chat")
    facts = facts or []
    steps = [{"kind": "observe", "text": f} for f in facts[:5]]
    conclusion = (
        f"Under goal '{goal}', prioritize: "
        + (facts[0] if facts else "gather evidence then one revision")
    )
    steps.append({"kind": "conclusion", "text": conclusion})
    return EnolaTraceResult(
        adapter="enola-omp-v1",
        goal=goal,
        steps=steps,
        invoked_via=f"workservice:{job_id}",
    )
