# OMP-595 transaction and worker handoff

Refreshed 2026-10-15 in OMP-595-s08 (ledger OMP-595 r2) against
`60fd67bfde1272938018b6c2a78501c4c1dadebc`. This is a local interface and
verification record. Every claim below is source verification on this revision;
none is installed proof. Installed acceptance stays with OMP-424/OMP-519 and the
installation hold is unchanged. External coordination with
`orch-worker-20261011` cannot be sent from this isolated, credential-free lane,
so it is recorded here rather than claimed as an acknowledgment.

## Tested revision and exact commands

Tested full git SHA: `60fd67bfde1272938018b6c2a78501c4c1dadebc` (recorded per
test through `record_property("tested_git_revision", ...)`).

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_orchestrator_durable_lease_e2e.py
```

Result on the tested revision: **7 passed, 0 failed, 0 skipped, 1 warning in
48.66s** (`fastapi/testclient.py` Starlette deprecation warning only). Every case
executes; the Linux namespace probe passes, so no case is skipped as hidden
proof. Measured physical termination for the three killing cases, against the
bound `TERMINATION_BOUND_S = 5.0` declared in the module before the run:

| Case | Measured termination (s) | Bound (s) |
| --- | --- | --- |
| lost lease `cancel` | 0.047 | 5.0 |
| lost lease `reassign` | 0.037 | 5.0 |
| frozen stop | 0.043 | 5.0 |

Termination is measured by scanning `/proc/*/cmdline` for the launch marker and
`/proc/<pid>/stat` for a non-zombie state, so both the child and its descendant
must be gone.

## What the integrated test proves

`python/omp-work/tests/test_orchestrator_durable_lease_e2e.py` composes the
disposable database, world, worker, sandbox, and model-proxy utilities with real
`JobWorker`, `OrchestratorHandler`, `run_implement`, `worker_sandbox`, and
`model_proxy` plus a fake provider. It adds cross-boundary cases the prior new
tests do not cover:

- **Lost response replays one identity.** A child submitter commits the work
  batch, is killed mid-submission (`work_commit`), or commits the job and request
  but dies before returning (`publication_commit`). A fresh submitter replays the
  identical request and gets the same work and job identity, one published job,
  and one `submitted` step; a runnable job is never visible before the complete
  durable request commits.
- **The replayed job runs past its initial lease.** The implement job's
  `lease_seconds` is dropped to 1, and the run continues under renewal beyond
  that initial interval while the hard task authority still bounds it.
- **Lost lease stops the run within the bound and refuses stale completion.**
  Owner cancel and fence reassignment both reach the process through the job
  context, kill the child and descendant, refuse late writes (`ctx.command`,
  `ctx.act`, `ctx.intents.record_intent` raise `LeaseLost`/`Frozen`), record no
  outcome, advance no stage, enqueue no job, and leave no settlement.
- **Frozen stop holds ownership and resumes once.** An engaged stop kills the run
  but keeps the lease admitted with the same worker, fence, and unreleased
  reservation; reconcile writes nothing, a competing claim gets no job, and one
  release resumes the saved work exactly once (`lease_resumed`/`resume_claimed`,
  no `lease_recovered`, one recovery step, fence unchanged).
- **Renewed model authority is bounded by the hard deadline.** A real compiled
  `orchestrator-worker` calls a fake provider whose first response is held until
  a renewal past the initial lease commits; the run then completes, the manifest
  `task_deadline` outlives the claimed initial expiry, and the task capability
  file carries the same `expires_at`, `worker_id`, and `fence`.

## Local change and atomicity

`python/omp-work/src/omp_work/jobs/admission.py` adds keyword-only
`cur: psycopg.Cursor[dict[str, object]] | None` to `enqueue_job`. Validation,
parent/trial/budget gates, operation/job identity, insertion, and events use
one implementation. Supplied cursor must already carry workspace/actor RLS
identity. The caller owns commit/rollback; there is no nested transaction.
Without a cursor, existing transaction behavior remains, including committing
budget cancellation and refusal records before raising `budget_exceeded`.

A request writer inserts the runnable job and full request on the same cursor
and commits once; earlier idempotent preparation may remain committed. A
separate claimant cannot see the job until that commit; rollback removes job,
enqueue event, and operation. `service.py::submit` takes
`pg_advisory_xact_lock(hashtextextended('orch:' || mission_id, 0))`, enqueues
job 0 through that cursor, and appends the `submitted` step in the same
transaction, so a replayed `submitted` request returns `enqueued` with the same
identity and a changed request raises `idempotency_conflict`.

## Merged revisions

OMP-595 revisions merged into this revision (newest first):

| Revision | Change |
| --- | --- |
| `60fd67bfde` | OMP-595-s07-s07: test model token revocation across worker exits |
| `e9d7fc36c2` | OMP-595-s07-s06: cover renewed model refusal and stale acceptance |
| `e589ae51ea` | OMP-595-s07-s05-s04: prove renewed-call billing and final revocation across ledgers |
| `2ad1cd8f30` | OMP-595-s07-s05-s03: prove identical renewed worker model authority, caps and prepared bounds |
| `76bce8d0bc` | OMP-595-s07-s05-s02: prove live renewal across original model worker lease expiry |
| `94b5d4d76d` | OMP-595-s07-s05-s01: bound fixture DB waits and capture proxy HTTP evidence |
| `0cd4282af8` | OMP-595-s07-s04-s02-s03: cover post-grant manifest and sandbox cleanup |
| `b1571c018b` | OMP-595-s07-s04-s02-s02: cover budget exhaustion and live-renewal worker failure |
| `4d175da311` | OMP-595-s07-s04-s02-s01: prove controls-only renewal stays under the fixed task deadline |
| `625f7e422c` | OMP-595-s07-s04-s01-s02: bind task capability expiry to captured task deadline |
| `80b85ac6d2` | OMP-595-s07-s04-s01-s01-s01-s01: bind worker grants and sandbox to task deadline |
| `cd69dfc8b3` | OMP-595-s07-s01: shared exact task_deadline from job-tree creation and explicit limits |
| `7d5feba140` | OMP-595-s06: carry JobWorker lease into stage contexts, fence stage appends, cancel sandbox on loss |
| `9a34a1b942` | OMP-595-s05: publish initial job and submitted request atomically |
| `22d3a63b73` | OMP-595-s04: reject mixed lease binding and non-string scopes in task capability |
| `1501dd5b07` | OMP-595-s03: distinguish lost authority from frozen stop on renewal |
| `65e88061c3` | OMP-595-s03: enforce execution authority and renewal lifecycle in JobWorker.tick |
| `9b4acaabca` | OMP-595-s01: enlist job enqueue in caller-owned transactions |

Local last-touch revisions (not active-lane tips): admission `8f51dc3309`,
service `f6d2fa0939`, sandbox `8d3679f7a0`, model budget `578a243f55`.

## Active-lane interface map

These are repository interfaces to reconcile with the active lanes, not asserted
file ownership. Exact active files and revisions need the reciprocal handoff.

| Active lane | Files/interfaces to reconcile with this plan | Active revision / acknowledgment |
| --- | --- | --- |
| `orch-worker-20261011-s03` | `orchestrator/service.py` request preparation and stage admission; `jobs/admission.py::enqueue_job(cur=...)` atomic request/job boundary | No reciprocal acknowledgment recorded in this worktree; OMP-595-s08 added tests only |
| `orch-worker-20261011-s06` | `orchestrator/worker_sandbox.py::run_worker`, task/model token binding, execution lifetime; `jobs/worker.py::cancel_requested` | No reciprocal acknowledgment recorded; test-only additions |
| `orch-worker-20261011-s07` | `model_proxy_budget.py::ModelProxyBudget.admit`, project/item/task ceilings, live lease and stop revalidation; generated token revocation | No reciprocal acknowledgment recorded; test-only additions |

OMP-595-s08 changes only
`python/omp-work/tests/test_orchestrator_durable_lease_e2e.py` and this
documentation; production sources, existing tests and helpers, contract
schemas, and qualification approvals are unchanged. The lane's own handoff note
is the `OMP-595-s08 durable lease handoff` entry in `NOTES-EXTERNAL.md`.

## Binding and lifecycle constraints

- Dynamic lease binding: request preparation cannot know the eventual claimant.
  Bind generated task tokens at claim to workspace/work/job, actual worker ID,
  and fence, using the current database lease expiry, not an enqueue-time
  snapshot. `service.py::lease_ok`/`lease_expires_at` and
  `model_proxy_budget.py::_revalidate` check live worker/fence/status with
  `clock_timestamp()`. Reclaim invalidates earlier bindings. See
  [orchestrator-control-plane.md](orchestrator-control-plane.md#dynamic-fenced-authority-versus-hard-task-deadline)
  for the authority model.
- Cancellation owner: `jobs/cancel.py` owns job/tree cancellation and
  `control_actions.py` owns control actions; `jobs/worker.py` signals
  `cancel_requested` when renewal fails. The integrated test confirmed the lost
  lease reaches the process through the job context with no new controller or
  watchdog.
- Hard task deadline: the fixed `_task_deadline` (oldest job-tree `created_at`
  plus the smaller of published and controls wall clocks) is recorded
  independently of renewable lease expiry and cannot be moved by renewal; the
  manifest and the task capability carry the same value.
- Model ceilings/revocation: project/mission, root-item, and task admission
  ceilings stay in `ModelProxyBudget`, which revalidates stop and lease after
  locks and before sending upstream. Generated task/model tokens become unusable
  after cancellation, deadline, lease loss/reassignment, or revocation. Existing
  installed credentials and public contracts remain fixed; OMP-595-s08 adds no
  proxy/store.

## Chris acknowledgment

No OMP-595-specific owner acknowledgment text is recorded in this worktree.
Dated owner decisions by Chris present in the repository, which bound this
programme's authority but are not an acknowledgment of OMP-595:

- `docs/model-review.md:30` — "Ledger OMP-615 r3, Chris, 2026-10-13", owner model
  review schedule.
- `docs/programme/ACCEPTED-DECISIONS.md` — "Owner decision by Chris on
  2026-10-13 (programme decision 4)", ECC database advisor retirement.
- `docs/webui-oversight.md:8` — `s05` (Chris) applies the oversight control to
  `omp-webui`.

Status: OMP-595-specific acknowledgment **not supplied**; this document does not
claim one. Installed qualification, live cutover, and any change to the
installation hold remain outside this lane and are owned by OMP-424/OMP-519.

## Master-report leads for changed files

Lookup leads, not claimed report findings.

| Changed file | Lead to verify |
| --- | --- |
| `python/omp-work/tests/test_orchestrator_durable_lease_e2e.py` | OMP-595 r2 integrated lost-response replay, past-initial-lease renewal, bounded kill with stale-refusal, frozen hold/release, and hard-deadline model authority evidence |
| `docs/OMP-595-handoff.md` | Tested revision, merged OMP-595 revisions, exact commands, pass/failure/skip counts, measured termination, owner-acknowledgment status |
| `docs/orchestrator-control-plane.md` | Atomic submission, dynamic fenced authority versus hard task deadline, lost lease versus frozen stop/release |
| `docs/upstream-fork-inventory.tsv` | OMP-595 own behavior rows; one row per path |

This document records no permission to install, change credentials or contracts,
resume held work, or alter the approved research scope.
