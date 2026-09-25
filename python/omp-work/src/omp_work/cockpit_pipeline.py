"""Run engine_pipeline under a PersistentCockpit job id."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from omp_work.cockpit_persist import PersistentCockpit
from omp_work.engine_pipeline import run_retrieve_compile_campaign, PipelineResult


def submit_and_pipeline(
    *,
    store_path: Path | str,
    job_id: str,
    objective: str,
    query: str,
    **pipeline_kwargs: Any,
) -> tuple[dict[str, Any], PipelineResult]:
    cockpit = PersistentCockpit(store_path)
    submit = cockpit.submit(objective=objective, job_id=job_id)
    result = run_retrieve_compile_campaign(
        job_id=job_id,
        query=query,
        objective=objective,
        **pipeline_kwargs,
    )
    # record evidence-ish checkpoint on job if status exists
    try:
        st = cockpit.status(job_id=job_id)
    except Exception:
        st = submit
    return {"submit": submit, "status": st}, result
