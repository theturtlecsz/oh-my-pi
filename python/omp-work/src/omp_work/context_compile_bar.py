"""Helpers for intake-bar measurements on compiled context."""
from __future__ import annotations
from omp_work.context_compile import compile_context
from omp_work.engine_pipeline import estimate_tokens


def measure_compile(*, job_id: str, findings: list[str], objective: str) -> dict:
    compiled = compile_context(job_id=job_id, findings=findings, objective=objective)
    section = compiled.as_packet_section()
    est = estimate_tokens(section)
    return {
        "job_id": job_id,
        "finding_count": len(findings),
        "tokens_est": est,
        "chars": len(section),
        "under_default_bar_1200": est <= 1200,
    }
