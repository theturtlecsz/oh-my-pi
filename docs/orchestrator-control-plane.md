# Orchestrator control plane

One jobs worker whose capabilities include `omp.orchestrator` is the control plane for a workspace. It holds `pg_try_advisory_lock(hashtextextended('omp_jobs.controller:' || workspace_id, 0))` for its lifetime. A second such process exits 3. While that config file `orchestrator.json` is present under `OperationsConfig.defaults().config_dir`, `parallel_streams` `tick`, `enqueue`, and `claim_one` raise `RuntimeError("control_plane_owns_admission")`.

## Authorities

Each authority is decided only by the modules named in its row.

| Authority | Modules |
| --- | --- |
| fleet admission | `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/jobs/stage_admission.py`, `python/omp-work/src/omp_work/control_plane/work_gates.py` |
| scheduling | `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/jobs/worker.py`, `python/omp-work/src/omp_work/orchestrator/stages.py` |
| leases | `python/omp-work/src/omp_work/jobs/lease.py`, `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/control_plane/mutation.py` |
| routing | `python/omp-work/src/omp_work/routing/policy.py`, `python/omp-work/src/omp_work/jobs/admission.py` |
| retry and recovery | `python/omp-work/src/omp_work/jobs/lease.py`, `python/omp-work/src/omp_work/jobs/worker.py`, `python/omp-work/src/omp_work/orchestrator/stages.py` |
| cancellation | `python/omp-work/src/omp_work/jobs/cancel.py`, `python/omp-work/src/omp_work/control_actions.py` |
| budgets | `python/omp-work/src/omp_work/jobs/budget.py`, `python/omp-work/src/omp_work/project_store.py` |
| progress events | `python/omp-work/src/omp_work/orchestrator/step_log.py`, `python/omp-work/src/omp_work/jobs/store.py` |

## Worker launch contract

Production orchestrator workers run headless in an isolated sandbox jail via exact argv `["/opt/omp/bin/omp", "orchestrator-worker"]`. See [docs/orchestrator-worker.md](orchestrator-worker.md) for the complete launch contract, token/manifest injection, scoped tools, evaluate stage, and loopback model proxy configuration.

## Verifier launch contract

Production audit launches the verifier through `run_worker` with exact argv `["/usr/bin/python3","-P","-m","omp_work.orchestrator.verify_main"]`, not the test launcher. Its cwd is `/work`; the candidate checkout is read-only, while `/work/.omp` is the writable audit directory. Linked git metadata and trusted `PYTHONPATH` runtime roots are mounted read-only, with `PYTHONSAFEPATH=1` so the candidate cannot shadow the verifier runtime.

The verifier reads `.omp/verify-task.json`, publishes `.omp/audit-report.txt`, then writes `.omp/verdict.json`. The outer verifier receives a read-only signing-key bind; inner `run_tests` runs against a frozen, read-only snapshot with no key bind. The launch timeout is the remaining stage lease, which also bounds the task's test timeout. A signed PASS settles only for the frozen candidate digest and attempt.

## Parallel streams features

Where the file-backed loop's feature lives in the control plane. `none` means no control-plane component decides it yet.

| Feature | Component |
| --- | --- |
| in_flight_max lock | none |
| heartbeat | none |
| claim | `python/omp-work/src/omp_work/jobs/admission.py` |
| path leases | none |
| budget reserve | `python/omp-work/src/omp_work/jobs/budget.py` |
| provider partitions | `python/omp-work/src/omp_work/routing/policy.py` |

## Parallel streams consumers

Callers of `parallel_streams` that still have to move. `migrated` is yes only when that caller no longer admits through the file-backed loop.

| Consumer | Path | Migrated |
| --- | --- | --- |
| parallel-admit CLI | `python/omp-work/src/omp_work/__main__.py` | no |
| parallel-admit tests | `python/omp-work/tests/test_parallel_streams.py` | no |
| parallel-admit routing tests | `python/omp-work/tests/test_parallel_streams_routing.py` | no |
| parallel streams baseline note | `docs/programme/R00-BASELINE-2026-09-25.md` | no |

## Atomic submission

`orchestrator/service.py::submit` prepares and publishes a mission's first runnable
job in one transaction. It takes
`pg_advisory_xact_lock(hashtextextended('orch:' || mission_id, 0))`, creates the work
batch and qualification, resolves the budget, then calls
`enqueue_job(..., cur=...)` for job `orch:{mission}:0` and appends the `submitted`
step on that same caller-owned cursor before it commits once. `enqueue_job` opens,
commits, and rolls back nothing when a cursor is supplied (see
[Authorities](#authorities)); the caller owns the transaction.

Consequences a claimant can rely on:

- No claimable job exists before the complete durable request commits. Job,
  enqueue event, operation row, work item, and `submitted` step become visible
  together, so a claimant never sees a runnable job without its request.
- A failure between preparation and publication leaves either the complete
  recoverable pair or no runnable job. Rollback removes the job, its enqueue
  event, and the operation; an earlier idempotent preparation may stay committed
  and is completed by a replay.
- Replay is identity-stable. A replayed `submitted` request under the same
  mission returns `{"status": "enqueued", "work_id": ..., "job_id": ...}` for the
  same work and job; a changed request raises `OrchestratorError("idempotency_conflict")`.
  Ids are `uuid5(NAMESPACE_URL, f"omp-417:{mission}:{name}")`, so concurrent
  identical submits converge and a concurrent claimant at the publication
  boundary observes either the pre-commit absence or the committed pair, never a
  partial job.

## Dynamic fenced authority versus hard task deadline

Two independent clocks bound a running job, and neither can extend the other.

- **Dynamic fenced authority.** The lease is renewed by `JobWorker` in a
  background thread at interval `lease_seconds / 3` while the handler runs.
  Renewal proves current ownership: the job row is `admitted`, its `worker_id`
  and `fence` match the claim, and `lease_expires_at > clock_timestamp()`. A
  healthy holder therefore continues indefinitely past the lease value that was
  current when it claimed, and the scoped task capability and process lifetime
  follow the renewals, not the obsolete initial deadline.
- **Hard task deadline.** `jobs/budget.py::task_deadline` is fixed at launch:
  the oldest job-tree `created_at` plus the smaller of the published item
  `wall_clock_seconds` and a positive-integer `controls.wall_clock_seconds`
  (or `DEFAULT_TASK_SECONDS` from now when neither limit exists). It is written
  to the model-worker manifest and to the task capability's `expires_at`, and it
  returns `None` once elapsed. Renewal never moves it; the sandbox timeout and
  the model-call ceiling are both clamped to it. See
  [orchestrator-worker.md](orchestrator-worker.md) for the manifest fields.

So a replayed or long-running job is bounded by whichever comes first: a lost
lease under the dynamic authority, or the fixed hard deadline. A future lease
does not outlive a past hard deadline.

## Lost lease versus frozen stop and release

Renewal failure is classified through the shared `jobs/lease.py::current_authority`
read, which distinguishes two outcomes with opposite effects.

- **Lost lease.** The row is missing, non-native, not `admitted`, held by another
  worker or a stale fence, or its `lease_expires_at` has passed. `JobWorker.tick`
  sets `cancel_requested` with a `cancel_reason`, stops renewal, and returns the
  job id **without settling**. The cancellation reaches the process through the
  stage context (`StageContext.cancel` via `StopOrCancel`), the sandbox is killed,
  and late writes are refused: `check_lease` raises `LeaseLost`, `settle_job`
  requires a live fence, and `require_live_holder` raises `job_fence_stale`. A
  killed child's result is stale and is never accepted.
- **Frozen stop.** An engaged agent stop on `aggregate_id=workspace_id` whose
  `engage_at` precedes the lease expiry makes `current_authority` report
  **frozen**: ownership is retained, not lost. `renew_lease` refuses with
  `agent_stop_engaged`, `tick` sets `frozen_requested`/`frozen_stopped` (not
  `cancel_requested`), and the job stays `admitted` with the same worker, fence,
  and unreleased reservation even as `lease_expires_at` passes. `reconcile_jobs`
  writes nothing while stopped. One `release_stop` resumes the saved work once
  for an active holder — `lease_resumed`/`resume_claimed` with the fence
  unchanged and no `lease_recovered` — and a holder that is missing or inactive
  goes back to backlog as `lease_recovered`.

`renew_lease`, `require_live_holder`, and `settle_job` all fence stage writes on
the current authority, so the database fence is what authorizes a result, not
the process state. The integrated cross-boundary evidence for both outcomes,
including measured physical termination within a predeclared bound, is
`python/omp-work/tests/test_orchestrator_durable_lease_e2e.py`; see
[OMP-595-handoff.md](OMP-595-handoff.md).
