"""Horizon B1 — Cognee/Enola depth with finding reuse across eng packets."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from omp_work.cognee_adapter import query_graph
from omp_work.enola_adapter import reason_trace


@dataclass
class FindingReuse:
    finding_id: str
    finding_text: str
    source_job_id: str
    eng_packets: list[str] = field(default_factory=list)
    eval_notes: list[str] = field(default_factory=list)

    def reuse_in(self, packet_id: str, eval_note: str) -> None:
        self.eng_packets.append(packet_id)
        self.eval_notes.append(eval_note)


def produce_finding(*, job_id: str) -> FindingReuse:
    cog = query_graph(query="write-first empty soft policy", job_id=job_id)
    eno = reason_trace(goal="stable knowledge for eng reuse", job_id=job_id, facts=[cog.finding()])
    text = eno.finding() or cog.finding()
    return FindingReuse(
        finding_id=f"find-{job_id}",
        finding_text=text,
        source_job_id=job_id,
    )
