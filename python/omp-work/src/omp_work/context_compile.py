"""Compile retrieval into worker-facing context for a sealed packet (W3)."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class CompiledContext:
    job_id: str
    findings: list[str]
    worker_context: str

    def as_packet_section(self) -> str:
        lines = ["## Compiled context (retrieval)", f"job_id: {self.job_id}", ""]
        for i, f in enumerate(self.findings, 1):
            lines.append(f"{i}. {f}")
        lines.append("")
        lines.append(self.worker_context)
        return chr(10).join(lines)


def compile_context(*, job_id: str, findings: list[str], objective: str) -> CompiledContext:
    if not findings:
        raise ValueError("findings required for context compile")
    body = (
        f"Worker-usable context for objective: {objective}"
        + chr(10)
        + "Use finding #1 as acceptance constraint. Do not invent sources."
        + chr(10)
        + f"Primary finding: {findings[0]}"
    )
    return CompiledContext(job_id=job_id, findings=list(findings), worker_context=body)
