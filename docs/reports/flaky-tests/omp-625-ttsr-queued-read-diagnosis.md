# OMP-625 queued read authority timeout diagnosis

Scope: only `TTSR queued read authority records the exact controlled target before completing its continuation` in `packages/coding-agent/test/agent-session-task-recovery.test.ts`. Bun 1.4.2, Linux x64. No product, fixture, stimulus, assertions, or other cases changed. Final case timeout remains 15000 ms.

## Finding

**The reproduced timeout is the second wait, `f.run`, not `held.promise`.** The run still makes progress and eventually completes; no 60-second hang occurred. The product did not miss the authority hold, controlled target, or continuation event in this timeout reproduction.

The fixture holds the first HTTP response body after a child read. Authority validation can emit the controlled TTSR events before that HTTP request actually starts. Yielding before native `fetch` makes TTSR abort that request first. The continuation's request then consumes the fixture's first-body hold. Its socket remains parked until Bun's server idle timeout closes it; transport retries and completes normally. The second test-local 10-second deadline expires before this recovery finishes. The unchanged assertions pass when awaiting the run itself.

This identifies an actual phase-specific failure mechanism, not uniform machine slowness. The supplied historical gate stacks omit the test line, so they cannot independently prove which wait failed in those particular runs. The controlled reproduction below directly identifies `f.run`.

## Temporary instrumentation and reproduction

Instrumentation was confined to this case and removed before commit. A monotonic timestamp trace recorded every parent extension `tool_call` (tool name and origin presence), every authority validation (binding, candidate, READY presence), hold/release, `continue`, run settlement, and request model sequence. Diagnostic event waits raced a throwing `Bun.sleep(60000)` watchdog, with a temporary 120000 ms case timeout. Additional fetch tracing recorded invocation, abort, response, and rejection.

Baseline command, from `packages/coding-agent`:

```sh
OMP_TEST_CONCURRENCY=7 bun test test/agent-session-task-recovery.test.ts \
  -t "TTSR queued read authority" --rerun-each 200
```

Instrumented baseline: **200 pass, 0 fail, 3400 assertions, 40.02 seconds**. Hold wait range 41–217 ms; run wait range 84–121 ms.

Fetch spy used `await Bun.sleep(delay); return originalFetch(...args)` on `globalThis.fetch`, with per-test restoration through the existing `afterEach`. Even delay 0 yields to the event loop. Selected trace from delay 0 (times relative to case entry):

```text
222ms tool_call task origin=true
239ms authority binding=false candidate=false READY=false
347ms tool_call read origin=false
380ms authority binding=true candidate=true READY=false
381ms authority binding=true candidate=true READY=true
381ms held
383ms release; wait f.run
384ms fetch enter child aborted=false
386ms fetch abort child
398ms fetch invoke child aborted=true
398ms fetch reject child TTSR matched rule: queued-no-unwrap
438ms continue
464ms fetch invoke child aborted=false
12223ms fetch reject child TypeError: The socket connection was closed unexpectedly.
12723ms fetch invoke child aborted=false
12724ms fetch response child status=200
12746ms tool_call yield origin=false
12787ms run resolved models=parent,parent,child,child,child,parent
```

Bun also printed `Bun.serve() timed out a request after 10 seconds`. The rejected already-aborted request never reaches the fixture; the continuation request inherits the hold. The next request after socket failure succeeds. This explains the long gap after `continue`, with authority hold already satisfied.

A separate run reinstated the original 10-second waits under the same fetch delay 0:

```text
327ms held
328ms wait ended held.promise; release; wait f.run
341ms fetch invoke child aborted=true
384ms continue
415ms fetch invoke child aborted=false
10329ms wait ended f.run
AbortError at untilAborted/onAbort
0 pass, 1 fail, 9 assertions; case 10545.62ms
```

The original wait fails after the pre-release assertions, before post-run assertions. The diagnostic event wait instead reaches settlement and all 17 assertions.

## Delay sweep

One diagnostic run per cell, one seam at a time. Append spy delayed synchronous `SessionManager.appendCustomEntry` with `Bun.sleepSync`; flush spy awaited `Bun.sleep` before `SessionManager.flush`; fetch and authority spies awaited `Bun.sleep` before the original operation. Values below are **hold wait / run wait in ms**, measured from entering each wait. These runs used the diagnostic 120-second case timeout, not the committed timeout.

| Seam | 0 ms | 50 ms | 500 ms |
| --- | --- | --- | --- |
| appendCustomEntry | 187 / 153, pass | 576 / 361, pass | 3266 / 2164, pass |
| flush | 185 / 119, pass | 446 / 420, pass | 2682 / 3149, pass |
| fetch | 181 / 12420, pass | 312 / 12468, pass | 1251 / 13038, pass |
| authority callback | 211 / 123, pass | 789 / 441, assertion failure | 3306 / 12644, pass |

No cell had 60 seconds without progress. Fetch cells and authority 500 ms were slow, exceeding the old run-wait deadline. Authority 500 ms also exceeded the final case's entire 15-second budget (16.30 seconds including cleanup); the fix does not promise to accommodate arbitrary cumulative injected latency beyond that unchanged budget.

Authority 50 ms exposed a separate ownership failure: `Agent listener rejected: Bound task recovery lost dispatch ownership`. A 20-repeat diagnostic yielded 5 pass / 15 fail, with both waits completing; failed cases lacked the later persisted `toolUse` entry and final parent request. Extra tracing confirmed the controlled target was persisted and continuation was called. This is not the reproduced timeout or a missing hold/settlement signal, and is not repaired by this narrowly scoped wait change. No new regression case or product change was added for it.

## Fix

Replace both test-local abort timers with event waits. Race the latched `held.promise` against the parent run settling. If the run settles first, throw `TTSR queued read run settled before authority hold`, including stimulus/hold flags, model sequence, and rejection cause. After releasing authority, await the same run promise. Both promises retain settlement, so a signal cannot be lost between subscription and emission. The original 15-second case timeout remains the outer bound.

All existing assertions, fixture options, and controlled external events remain byte-for-byte unchanged. No product edit is warranted for the reproduced timeout: it completes the recovery and delivers the expected events.

## Final verification

Verification results are recorded below after running the final case without instrumentation, plus temporary fetch-delay reproductions with the original 15000 ms case timeout.

Final focused repeat, while the whole-file lanes were active:

```text
$ OMP_TEST_CONCURRENCY=7 bun test test/agent-session-task-recovery.test.ts -t "TTSR queued read authority" --rerun-each 200
200 pass
19800 filtered out
0 fail
3400 expect() calls
Ran 200 tests across 1 file. [132.20s]
exit=0
```

Temporary fetch spies rechecked the reproduced slow path against the final waits and unchanged 15000 ms timeout, then were removed:

| Fetch delay | Case duration | Result |
| --- | --- | --- |
| 0 ms | 12867.91 ms | 1 pass, 0 fail, 17 assertions |
| 50 ms | 13191.91 ms | 1 pass, 0 fail, 17 assertions |
| 500 ms | 14456.67 ms | 1 pass, 0 fail, 17 assertions |

`bun check` exited 0 (lint/formatting, workspace type checks, runtime type check). Its Rust lane reported `Skipping check:rs (not in CI and no Rust-affecting changes were found)`. Existing lint warnings remained warnings. No full suite or gate script was run.


Whole-file qualification ran three independent Bun processes concurrently, each in `packages/coding-agent`, each with this command:

```sh
OMP_TEST_CONCURRENCY=7 bun test test/agent-session-task-recovery.test.ts --rerun-each 20
```

All three lanes started at 2026-10-15 09:30:11 UTC. Main lane PID 4004975; peer lane PIDs 4004982 and 4004986. The target case passed in all 20 main-lane iterations while both peer lanes were still executing. Each whole-file iteration passed all 100 cases. Quoted final summaries:

```text
main
2000 pass
 0 fail
 12920 expect() calls
Ran 2000 tests across 1 file. [578.67s]
exit=0
```

```text
peer-1
2000 pass
 0 fail
 12920 expect() calls
Ran 2000 tests across 1 file. [571.40s]
exit=0
```

```text
peer-2
2000 pass
 0 fail
 12920 expect() calls
Ran 2000 tests across 1 file. [576.37s]
exit=0
```

Main-lane iteration evidence (counts computed from each run’s test result lines; duration is the target case):

| Run | File pass / fail | Target case ms |
| --- | --- | --- |
| 1 | 100 / 0 | 356.33 |
| 2 | 100 / 0 | 514.50 |
| 3 | 100 / 0 | 409.77 |
| 4 | 100 / 0 | 307.26 |
| 5 | 100 / 0 | 222.35 |
| 6 | 100 / 0 | 217.93 |
| 7 | 100 / 0 | 194.24 |
| 8 | 100 / 0 | 232.37 |
| 9 | 100 / 0 | 227.83 |
| 10 | 100 / 0 | 223.27 |
| 11 | 100 / 0 | 328.08 |
| 12 | 100 / 0 | 249.28 |
| 13 | 100 / 0 | 228.83 |
| 14 | 100 / 0 | 180.73 |
| 15 | 100 / 0 | 379.44 |
| 16 | 100 / 0 | 336.20 |
| 17 | 100 / 0 | 256.51 |
| 18 | 100 / 0 | 280.20 |
| 19 | 100 / 0 | 228.49 |
| 20 | 100 / 0 | 276.05 |
