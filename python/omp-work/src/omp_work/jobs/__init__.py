"""Native research jobs API (R03, OMP-324) on the shared omp_jobs substrate."""

from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.store import (
    JobError,
    NativeJobStore,
    drain_worker,
    native_trial_bind_diagnostic,
    project_trial,
    record_usage,
    register_worker,
)

__all__ = [
    "JobError",
    "NativeJobStore",
    "claim_job",
    "drain_worker",
    "enqueue_job",
    "native_trial_bind_diagnostic",
    "project_trial",
    "record_usage",
    "register_worker",
]
