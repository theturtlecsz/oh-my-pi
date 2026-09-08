# S2.2 ordinary first-run completed-child reproduction

## Scope and identities

Continue OMP-246 revision `f5bb9640-3491-50b7-bdad-2a77f9c102e3` after PR #24
merge `0dab30c9046f591848e36cf7e29a33f3dfc95831`. Its tree
`806f6738756da038a342d4e163fde98e8d382d18` matches reviewed candidate
`af2aae1efa356ea6957a70fa9236fbb16c40d38e` and the qualified hosted tree.
Work only in isolated branch `codex/omp-first-run-task-recovery` at
`/tmp/omp-recovery-stabilization`.

Reproduce an ordinary synchronous task completing before its original parent
result becomes durable, with no initial crash or child recovery. Reuse immutable
local release
`/home/thetu/.local/state/omp-stabilization/releases/completed-af2aae1efa`,
manifest SHA256
`e6ced038502814e8320c6f636b03f84da26857a61deeee00285f0e0a24c99882`,
runtime source `af2aae1efa356ea6957a70fa9236fbb16c40d38e`, Bun 1.4.0.
Record the separate harness base, exact changed-file hashes and commands.

This is a reproduction-only plan: no production changes, journal reconstruction,
injected results, workflow phase edits or acceptance claims. PR #24 certifies
completed results only on the recovered synchronous path. Legacy uncertified
histories and started-but-unfinished processing remain deliberate refusals.

## Setup and external stop observer

Reuse disposable PostgreSQL/WorkService, local provider transport, local bare Git
remote and supported flat, non-isolated bundled synchronous task configuration.
Keep the actual installed CLI, task sessions, host, service and native finalizer.
The child performs a real `read` and successful `yield`. Hold its first provider
response only to record original parent/child/binding identities and arm the
observer; release continues this first execution without a prior crash.

Subscribe through supported `set_subagent_subscription: events` before release.
A single stdout reader recognizes the exact original child's completed lifecycle
frame and immediately SIGSTOPs the managed launcher/CLI process group. Do not
wait for main-thread polling, another RPC request or artifact copying first.

Use a nonblocking byte reader from the beginning of this case. It owns stdout
exclusively, preserves original bytes and complete frames, and drains available
buffered output after stop confirmation. Never combine a second reader with
existing buffered TextIO consumption. Existing cases retain their reader behavior.

Ordinary completion has no guaranteed post-finalization authority GET. RPC output
is queued asynchronously; observing completion can lag parent processing.
SIGSTOP selects a candidate cut, and frozen evidence determines whether it
qualifies. The previous output-triggered authority proxy alone cannot prove it.

## Validate the cut before killing

Confirm the actual CLI identity and stopped process/thread states through `/proc`.
Inspector, provider and service remain outside the stopped group. Send no RPC
command to the stopped host. Require all of these simultaneous witnesses:

- Original parent call, forward binding, reverse child initialization and
  preparation identities remain valid.
- Child's real read and successful yield results are complete in its durable
  active branch; the actual output artifact matches that yield.
- The original completed lifecycle frame was genuinely received.
- Original parent ToolResult is absent from the durable parent branch and
  drained RPC output. Ambiguous partial output is preserved as a missed cut.
- No parent provider continuation occurred.
- Grant, item/revision/version/reservation, workflow, workspace contents, HEAD
  and remote refs remain unchanged.
- No native-ready or processing-start record exists. This establishes
  uncertified first-run completion, not proof that parent processing never began.

Preserve stop timestamps, trigger frame, process topology, both journals,
artifact hashes, provider observations and direct-service/effect snapshots.
Account for outstanding filesystem writes by rechecking before kill and after
death. Do not infer a valid cut solely from a lifecycle event or signal time.

## Fault, restart and bounded attempts

For a qualifying frozen cut, SIGKILL the same group without SIGCONT. Verify
launcher exit, actual CLI death and RPC EOF. Recheck parent-result absence and
saved witnesses before restarting the identical parent session/runtime without
an owner prompt, retry command, new assignment or repair write.

Record actual behavior and repeat restart. Expected baseline behavior is explicit
refusal of uncertified completed-child history; a subsequent refusal may differ
after genuine shutdown diagnostics. Require no child reexecution, replacement
task, duplicate result, new reservation or authority/effect change.

Permit at most three fresh disposable attempts. Preserve every miss with its
actual reason. Never rewind a missed run or relabel a parent-result-durable cut.
If all attempts miss, report **checkpoint not reached** and refine the observer
before further runs. A public `tool_result` hook would already be inside ordinary
parent processing and cannot silently substitute for the requested boundary.

Keep the automatic recovery positive assertion visibly failing if unsupported.
Refusal safety is separate evidence and does not close S2.2. This is process-stage
loss, not S2.3 committed-command response loss. No production remedy is admitted
until an actual failing reproduction and bounded implementation plan exist.

## Files, verification and handoff

Allowed changes: this plan, the installed recovery test and shared support,
necessary static test fixtures, recovery coverage documentation and fork inventory.
Do not edit the immutable release, production modules or live checkout.

Review the observer before the actual run. Use the pinned installed qualification
command with a new durable basetemp and JUnit path, selecting only this new case
for the negative reproduction. Record exact command/cwd/exit, source and harness
identities, manifest, process IDs/signals, durable journals, real service and Git
effects, output hashes, misses and actual refusal/recovery behavior. Failed
reproduction remains a failure; it is not converted into a passing safety test.
Existing ten-case evidence remains valid for unchanged runtime source; expand
regression verification only if shared harness changes warrant it.

Write and read back the supported WorkService handoff, leaving OMP-246 and OMP-249
acceptance unchanged. Follow the standing automatic continuation/model policy:
Astra low for routine work, high for complex implementation, xhigh for hardest
design and independent review. No live activation, native audit or owner command
reference is manufactured.
