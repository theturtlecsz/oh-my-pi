# Developing OMP without destabilizing active work

OMP's immediate reliability problem is that its development checkout can also
supply the running CLI, extensions, auditor transport, and editable Python
WorkService. Editing those files changes the system responsible for accepting
the edit. A fresh worker can load different code from its still-running owner;
the service then refuses writes or the execution grant detects judge drift.
These refusals protect real invariants, but they interrupt ordinary development.

This diagnosis follows the source and local reproductions. It does not identify
the cause of every failure in an installed session without that session's logs.

## Operating arrangement

Use a tested, pinned installation to supervise a separate development worktree.
Keep the CLI, session-system extensions, auditor definition/transport, native
addon, and WorkService on one qualified release. A stable CLI alone is
insufficient if its extension symlinks or service still point into the directory
being edited. `bash session-system/install.sh --print-manifest` inspects managed
live links without changing them; also inspect the CLI target and the
WorkService unit's executable and working directory.

| Environment | Purpose | Change rule |
| --- | --- | --- |
| Stable installation | Run real work and supervise development | Promote a qualified version between runs; retain the previous version for rollback. |
| Development worktree | Edit code and run focused tests | Changes may break this environment without replacing the live controller. |
| Qualification environment | Exercise the candidate CLI, extensions, and service together | Use a disposable PostgreSQL database, test capabilities, and local Git remotes. |

The current service-refresh exception is not a general deployment mechanism.
It restricts eligible changes and checks identities, but still changes service
code during an active grant. A full separation requires qualifying self-hosted
development against the stable service: the host currently recognizes Python
paths in the candidate worktree as a reason to refresh. Do not assume that moving
the CLI alone completes the isolation work.

Contract or migration changes need an explicit promotion procedure, compatibility
checks, and the existing owner approval where required. Follow
[Work Ledger operations](work-ledger-operations.md) for those operations. Restart
OMP after promoting extensions; changing a symlink does not reload modules in an
existing process. Never migrate a database backward merely by switching source
directories.

## Diagnose the stop before retrying

Capture `/execute status <key>`, the exact refusal, the grant/item phase, candidate
SHA, and installed version. Preserve the operation ID when reconciling a write
whose outcome is unknown. Remove credentials from any shared diagnostics.

| Observation | Interpretation | Recovery boundary |
| --- | --- | --- |
| `service_stale` | Running service and on-disk code or migrations differ. | Finish the appropriate service promotion/restart procedure, check service readiness, then reconcile the pending operation. Repeating the model turn cannot repair this. |
| `contract_mismatch` | Host and service speak different approved contracts. | Align qualified versions and restart the host; retain the contract approval requirement. |
| `judge TCB drift` | The grant's trusted runtime differs from the current runtime. | Restore the pinned runtime or end the old grant and start a qualified new run. Do not rewrite a digest to suppress the refusal. |
| Paused grant | Work may continue after its preflight succeeds. | `/execute resume <key>`; inspect its concrete refusal if the preflight fails. |
| Terminal grant | A cap, stop decision, or other terminal condition ended this authorization. | Follow the returned next command; terminal grants cannot resume. Planning omissions discovered before the first review can use the existing guarded replan path; they do not inherently require ending the grant. |
| `/summary` push unverified | Frozen candidate exists, but delivery is unproven. | Reconcile the remote, then re-enter `/summary`; do not recreate already recorded work. |
| Merge head changed | PR no longer identifies the audited candidate. | Review the new candidate; completion stays blocked. |

Stopping execution must remain possible when auditor discovery or transport is
broken. Stop, cancel, and owner-message pause now use the existing grant identity;
resume and review continue to validate the current trusted runtime. Service
readiness now reports stale code explicitly while preserving the prospective
fingerprint required by the existing refresh handshake.

## Qualification before production

Run the workflow suites as part of normal gates. They cover OMP-specific behavior
that the upstream package tests alone do not exercise:

```sh
bun test session-system/tests packages/work-client
bun test scripts/ci-test-ts.test.ts
uv run --project python/omp-work --extra dev pytest python/omp-work/tests
bun run test:py:work-ledger:integration
bun run test:session:smoke
OMP_WORK_POSTGRES_INTEGRATION=1 bun run session-system/tests/execute-cycle-smoke.ts
```

The integration commands require the supported PostgreSQL 18 environment and
native dependencies. Skipped integration cases do not qualify a release. The
execution smoke uses controlled fixtures; it does not establish live provider or
GitHub behavior. CI currently requires the PostgreSQL integration and candidate
smoke; the separate execution-cycle smoke above still needs qualification and
explicit CI inclusion. Run the CLI installation smoke and a bounded real canary
on the exact release before promotion.

Next engineering priorities, in order:

1. Qualify immutable controller/service installations and an explicit promotion
   boundary. Record which release owns each active grant; drain or deliberately
   terminate old grants before incompatible upgrades.
2. Exercise restart and failure recovery at every write/effect boundary using
   real PostgreSQL. Include a crash after a push/merge succeeds but before its
   receipt is stored, duplicate requests, and a stale worker after cancellation.
3. Make trusted test execution and delivery evidence mandatory at the service
   boundary. The current host merge check and model-supplied verification body
   do not, by themselves, prove every client's completion request is valid.
4. Track completion without human repair, false refusals, recovery time, and cost
   per accepted task. Preserve actual failures as regression cases. Expand Fleet
   only after the one-worker path satisfies the v9.1 qualification corpus.

The neuro-symbolic goal remains intact: models interpret intent and propose
changes; deterministic code binds authorization, state transitions, evidence,
and delivery to exact identities. Reliability work should make legitimate
progress and recovery dependable while preserving those boundaries. Another
agent framework would not remove these obligations.
