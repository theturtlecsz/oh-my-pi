# OMP-595 transaction and worker handoff

Prepared 2026-10-11 in OMP-595-s01 against
`fe7dc94dc68b73dfde99bb651302f20d94c0fcfb`. This is a local interface plan,
not evidence that another lane has agreed to it. The task identifies active
`orch-worker-20261011-s03`, `-s06`, and `-s07`; their active file lists, tip
revisions, and acknowledgments are not present in this worktree. OMP-595-s02
must record the active handoff before overlapping edits. Installation hold
(OMP-424/519) and approved research remain unchanged.

## Local change and atomicity

`python/omp-work/src/omp_work/jobs/admission.py` adds keyword-only
`cur: psycopg.Cursor[dict[str, object]] | None` to `enqueue_job`. Validation,
parent/trial/budget gates, operation/job identity, insertion, and events use
one implementation. Supplied cursor must already carry workspace/actor RLS
identity. The caller owns commit/rollback; there is no nested transaction.
Without a cursor, existing transaction behavior remains, including committing
budget cancellation and refusal records before raising `budget_exceeded`.

A future request writer must insert the runnable job and full request on the
same cursor and commit once. Earlier idempotent preparation may remain
committed. A separate claimant cannot see the job until that commit; rollback
removes job, enqueue event, and operation. This lane supplies that primitive
and tests; it does not alter orchestrator service or sandbox execution.

## Active-lane interface map awaiting s02 confirmation

These are repository interfaces to reconcile with the three active lanes,
not asserted file ownership. Exact active files and revisions need the s02
handoff; no assignment or coordination has been inferred from lane numbers.

| Active lane | Files/interfaces to reconcile with this plan | Active revision / acknowledgment |
| --- | --- | --- |
| `orch-worker-20261011-s03` | `orchestrator/service.py` request preparation and stage admission; `jobs/admission.py::enqueue_job(cur=...)` atomic request/job boundary | Pending s02 evidence; no overlap authorized by this document |
| `orch-worker-20261011-s06` | `orchestrator/worker_sandbox.py::run_worker`, task/model token binding, execution lifetime; `jobs/worker.py::cancel_requested` | Pending s02 evidence; confirm actual file set |
| `orch-worker-20261011-s07` | `model_proxy_budget.py::ModelProxyBudget.admit`, project/item/task ceilings, live lease and stop revalidation; generated token revocation | Pending s02 evidence; confirm actual file set |

Local last-touch revisions (not active-lane tips): admission `8f51dc3309`,
service `f6d2fa0939`, sandbox `8d3679f7a0`, model budget `578a243f55`.
Record immutable active tips and changed-file lists here when s02 supplies them.

## Binding and lifecycle constraints

- Dynamic lease binding: request preparation cannot know the eventual claimant.
  Bind generated task tokens at claim to workspace/work/job, actual worker ID,
  and fence. Use current database lease expiry, not an enqueue-time snapshot.
  `service.py::lease_ok`/`lease_expires_at` and
  `model_proxy_budget.py::_revalidate` already check live worker/fence/status
  with `clock_timestamp()`. Reclaim must invalidate earlier bindings.
- Existing cancellation owner: `jobs/cancel.py` owns job/tree cancellation;
  `control_actions.py` owns control actions. `jobs/worker.py` signals
  `cancel_requested` when renewal fails. Preserve that ownership; request
  enlistment adds no cancellation controller or watchdog.
- Hard task deadline: record a fixed task deadline independently of renewable
  lease expiry. Bound task execution and model calls by both; renewal cannot
  extend the hard deadline. Exact deadline field, value, and enforcement
  interface remain pending active-lane handoff; this lane adds none.
- Model ceilings/revocation: preserve project/mission, root-item, and task
  admission ceilings through `ModelProxyBudget`; revalidate stop and lease
  after locks, before sending upstream. Generated task/model tokens must become
  unusable after cancellation, deadline, lease loss/reassignment, or revocation.
  Existing installed credentials and public contracts remain fixed. Exact
  revocation interface awaits s02 evidence; no new proxy/store is proposed.

## Master-report leads for planned changed files

No master-report files were available inside the authorized worktree. These
are lookup leads for s02/Chris, not claimed report findings or approvals.

| Changed file | Lead to verify in master-report |
| --- | --- |
| `python/omp-work/src/omp_work/jobs/admission.py` | OMP-595 atomic runnable-job/full-request boundary; native admission/idempotency and committed budget refusals |
| `python/omp-work/tests/test_orchestrator_enqueue_transaction.py` | OMP-595 independent claimant visibility, rollback residue, replay conflict, parent and budget gate acceptance evidence |
| `docs/OMP-595-handoff.md` | Active `orch-worker-20261011-s03/s06/s07` handoff, installation hold, approved research, lifecycle/deadline/token decisions |
| `docs/upstream-fork-inventory.tsv` | OMP-595 own behavior rows; serialize shared inventory integration and retain one row per path |

## Pending Chris acknowledgment

Status: **pending**. Chris acknowledgment text/date/reference: **not supplied**.
Active s03/s06/s07 immutable revisions and file ownership: **pending s02**.
Dynamic lease/token binding, hard deadline interface, ceilings/revocation,
and cancellation owner reconciliation: **pending acknowledgment**.
This document records no permission to install, change credentials/contracts,
resume held work, or alter the approved research scope.
