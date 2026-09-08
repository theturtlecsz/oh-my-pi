# S2.2: real execution process recovery

This is the bounded implementation plan for the next failure reproductions in
OMP-246. The [coverage map](omp-recovery-coverage.md) identifies existing evidence;
WorkService receipts own progress and completion. Start from the qualified
installation of merge `579b374cd3d2f71e59498b3ed866dc45aea743cd` and disposable
state. No live configuration, grant, database, or installation may be changed.

## First reproduction: lost controller continuation

1. Factor existing installed-test helpers into shared test support; retain the
   two existing isolation cases without changing their observable contracts.
2. Use real disposable PostgreSQL and installed WorkService. Set up fixture work
   and a single-item execution grant through supported service operations, with
   explicit test provenance. Compute actual installed TCB identities and create
   the execution worktree through existing helpers. Do not substitute arbitrary
   hashes or manually invent a workspace/journal layout.
3. Start the admitted RPC CLI with a persisted session. Actual startup recovery
   must select the grant and relocate the real SessionManager. This fixture
   qualifies recovery, not GitHub admission; the admitted launcher does not
   inherit a test PATH or injected extension.
4. A loopback provider server supplies deterministic protocol responses for
   criteria sealing, plan stamping, one approved text edit and execution review.
   Keep real AgentSession, SessionManager, extension dispatch, scheduler, journal
   and effect implementations. Never script an auditor PASS or service success.
   Static fixture plan text lives in a Markdown file.
5. Reach the production pending-checkpoint path in `begin_execution_review`,
   which calls the actual continuation delivery function. Hold the subsequent
   provider response while the current turn is still active. Before killing,
   prove checkpoint delivery attestations finished, the real outbox records
   `delivered`, and the corresponding hidden continuation is not yet in the
   persisted session. If this window cannot be observed, refine the reproduction
   rather than labeling a different failure as this regression.
6. SIGKILL the controller and restart the same session file/runtime state, without
   a repair prompt. Require exactly one recovered continuation bound to the same
   authorized grant/item/revision; no duplicate reservation or local Git effect.
   Restart again to check replay does not duplicate progress.

## Remediation and further stage coverage

Raw RPC attribution must distinguish recovery refusals from dropped continuation.
The initial review-path probe exposed an earlier failure: freezing advanced HEAD
while the grant item remained `executing`, so startup rejected its own finalized
candidate before reaching outbox replay. Preserve that as a separate regression;
any recovery allowance must bind the finalized candidate to the current revision
and actual execution grant/attempt, while continuing to reject foreign HEADs.

To isolate the outbox window without that prerequisite, hold the initial real
provider request, pause through the disposable service API, then invoke the actual
RPC `/execute resume` command during streaming. This uses the same production
delivery path with unchanged HEAD. Capture raw RPC and require no preflight refusal
before attributing the restarted silence to outbox suppression. Do not edit grant
phase, candidate identity or journal contents to force either reproduction.

Preserve the failing baseline evidence before changing production code. Reuse
receipt-backed extension delivery and checkpoint-delivery primitives; never await
an injection receipt inside the handler whose completion permits injection.
Distinguish queued intent, actual injection and consumption. Injection alone is
not proof of an exactly-once external effect. Cancelled, expired, superseded or
terminal work cannot be revived to make a recovery test pass.

After the first controller regression is fixed, identify and execute the other
real stage boundaries, including process death while a real task session/tool
is active. `runSubprocess` currently runs task agents in-process: kill the shared
controller/task process, not a nonexistent independent worker PID. Keep coverage
separate; a test that never starts a task cannot satisfy task-recovery coverage. S2.3 owns the committed-response-loss case and S2.5 owns cancellation
after queueing; reuse the fixture without claiming those scenarios passed early.

## Verification and evidence

- Reproduce against the exact baseline artifact, recording the real barrier,
  process IDs, signal/exit status, service state and session/journal contents.
- Stage a fresh candidate artifact after any runtime fix and rerun the regression
  against its recorded manifest; do not mutate the admitted baseline installation.
- Run existing installed-isolation cases after helper extraction, focused session
  and outbox regression tests after production changes, and supported type checks.
- Add implemented installed recovery cases to required CI and retain JUnit,
  manifest and useful fault/effect output. Missing inputs or skipped cases do not
  qualify a release.
- Keep installation-guide commands and evidence descriptions aligned with the
  implemented CI cases; retain explicit limits for the remaining stage coverage.
- Independent review must inspect the real failing path, assertions, remediation
  and declared limits before code delivery. Record each result through WorkService
  and read back the receipt; leave S2.2 open until its stated stage coverage passes.

Scope excludes paid/provider-readiness trials, live GitHub effect trials, native
acceptance fabrication, contract/schema changes, and live promotion. Any actual
interface blocker gets a concrete recorded remedy, not a synthetic success.
