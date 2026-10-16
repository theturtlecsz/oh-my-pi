# OMP-640 process lifecycle evidence

Scope: OMP-640-s01 supplies explicit child/pipe ownership and regression proof.
Existing tests, production code and preload remain unchanged. Applying the
fixture to existing test files and five full gate runs belong to other tasks.

## Environment and commands

Observed 2026-10-16, Linux, Bun `1.4.2 (744846f84)` in worktree
`/home/ompbot/flood-repos/oh-my-pi-wt/OMP-640-s01`.
Root `bunfig.toml` loads `scripts/session-system-test-preload.ts`, allocating a
private `TMPDIR`; individual tests also remove their own fixture directories.

Passing focused commands:

```sh
bun test session-system/tests/process-lifecycle.test.ts
bun test session-system/tests/omp-262-plan-identity.test.ts session-system/tests/update.test.ts
bun scripts/upstream-inventory.ts
```

The lifecycle file passes eight cases with no dangling-process diagnostic.
The two existing files pass 29 cases. These are focused runs, not evidence that
the entire session suite passes repeatedly under the gate's empty HOME/no uv.

## Actual children on both paths

### Identity: synchronous git, not a forgotten asynchronous mapper

`omp-262-plan-identity.test.ts` `stamp payload parity` calls `runExecution`,
then `createWorkflowHost`. `session-system/extensions/workflow/host.ts:740`
uses `node:child_process.spawnSync`:

```ts
spawnSync("git", ["-C", process.cwd(), "rev-parse", "--show-toplevel"], { encoding: "utf8" })
```

A disposable preload wrapped the real `Bun.spawnSync` (used by Bun's Node
compatibility implementation), recording only argv, PID, exit and signal.
It also traced the existing `fs.promises.rm` calls, reinstalling that trace
after the test's `vi.restoreAllMocks()`. It changed neither command nor return
value. The preload was deleted after this experiment. Command:

```sh
bun test --preload ./session-system/tests/fixtures/omp-640-probe.ts --timeout 1 session-system/tests/omp-262-plan-identity.test.ts
```

This deliberately failing diagnostic run produced this chronology, milliseconds
since preload initialization (imports account for most elapsed time):

```text
+689 spawn ["git","-C","<worktree>","rev-parse","--show-toplevel"]
     killed 1 dangling process
+689 remove /tmp/omp-session-test-arersc/omp-262-exec-csvVeQ
+689 exit pid=2388336 exitCode=null signalCode=SIGTERM
     stamp_execution_plan failed: could not read plan file
     /tmp/omp-session-test-arersc/omp-262-exec-csvVeQ/plan.md: ENOENT
+699 removed /tmp/omp-session-test-arersc/omp-262-exec-csvVeQ
+699 outer afterEach hook
+701 next git spawn; killed 1 dangling process
+701 exit pid=2388337 exitCode=null signalCode=SIGTERM
+702 next git spawn; killed 1 dangling process
+703 exit pid=2388338 exitCode=null signalCode=SIGTERM
```

Thus the diagnostic can identify a *synchronous* child whose caller has not
returned. Later calls really create new git PIDs and return a signal instead
of status 0. This is not evidence of a failed `posix_spawn` syscall. Fixture
removal starts **before** the synchronous call returns; the still-running
body then reads the very directory being removed. The exact removed path and
failed read match here, rather than an equivalent removed-cwd example.

### Update fence: mapped Python child, already reaped on normal completion

`update.test.ts` starts `python3 <fixture>/bin/mapper.py
<fixture>/repo/mapped.bin` (second case adds `shield`). The Python script
opens and mmaps the file, prints `ready <procfs-pid>`, then sleeps 600 seconds.
Both cases already have `finally { mapper.kill(); await mapper.exited; }`.

The same disposable preload wrapped real `Bun.spawn` to record the returned
PID and immediately read **that PID's** `/proc/<pid>/cmdline`; it observed
`proc.exited` without changing child behavior. Focused command:

```sh
bun test --preload ./session-system/tests/fixtures/omp-640-probe.ts --test-name-pattern 'live-mapping fence' session-system/tests/update.test.ts
```

Observed first child PID `2389151`, cmdline `python3
/tmp/omp-session-test-sxwp3O/omp-update-test-34XqxV/bin/mapper.py
/tmp/omp-session-test-sxwp3O/omp-update-test-34XqxV/repo/mapped.bin`.
It exited `143/SIGTERM` at +138ms, before the second update command and
+207ms hook. Second child PID `2389360`, same argv shape plus `shield`, exited
`143/SIGTERM` at +381ms, before +383ms hook. Both cases passed.
These are observed child handles and procfs identities, not attribution from
the preceding test name. During the synchronous update command the mapper
is intentionally alive; a test deadline there can require emergency cleanup
before JavaScript reaches its existing `finally`.

## Runtime source and hook ordering

Pinned source for the installed revision
[`744846f844374847c902b5e7fd59b4342a51ef99`](https://github.com/oven-sh/bun/commit/744846f844374847c902b5e7fd59b4342a51ef99):

- [`Execution.rs:300–334`](https://github.com/oven-sh/bun/blob/744846f844374847c902b5e7fd59b4342a51ef99/src/runtime/test_runner/Execution.rs#L300):
  `handle_timeout` invokes `kill_dangling_processes_on_timeout`; an expired
  serial entry calls the VM auto-killer and prints the dangling count.
- [`js_bun_spawn_bindings.rs:1937–1969`](https://github.com/oven-sh/bun/blob/744846f844374847c902b5e7fd59b4342a51ef99/src/runtime/api/bun/js_bun_spawn_bindings.rs#L1937):
  the synchronous wait honors the active test deadline, kills pending children
  and closes readable pipes. This can happen inside a synchronous API call;
  it does not require a separate watchdog thread executing JavaScript.
- [`spawn_sync:277–308`](https://github.com/oven-sh/bun/blob/744846f844374847c902b5e7fd59b4342a51ef99/src/runtime/api/bun/js_bun_spawn_bindings.rs#L277):
  after the isolated loop is torn down and child reaped, the timeout callback
  runs **before** the synchronous result returns to JavaScript. This explains
  teardown starting before the identity body resumes.

The committed nested `test-deadline` scenario experimentally establishes the
asynchronous ordering too: child alive → 100ms test timeout/emergency kill →
awaited `afterEach` reaps child and finishes readers → next test starts and
observes exit → suspended body's `finally` eventually runs → git case passes.
The next case deliberately waits long enough to observe that continuation.
The separate `sync-deadline` scenario holds a registered asynchronous child
while a bounded synchronous Bun child sleeps past the same 100ms deadline.
Bun reports two killed children; the next synchronous git init/config still
exits 0. Thus both asynchronous and synchronous deadline paths are exercised.
A body `finally` is therefore **late**, not guaranteed never to run. An awaited
hook does not retroactively suppress an already printed timeout diagnostic.

Normal return, assertion failure and a 30ms **operation** deadline follow a
different ordering: body settles → awaited hook reaps child → next case
observes exit and unlocked pipes → git init/config/readback exit 0. No dangling
diagnostic. Keeping the operation deadline below Bun's test timeout makes
cleanup possible before Bun's emergency action.

## First stall versus later failures: evidence limits

The archived first identity timeout was followed by fixture `ENOENT`s and
later 30-second git-related timeouts. The controlled identity run above proves
why timed-out sync probes return failure: Bun kills their actual git PIDs at
the active deadline; it also proves the resulting fixture teardown race.
The later failures reproduced here are **new probes reaching their own
expired deadlines**, not missing executables or proven deleted git cwd. Git's
`-C` target in this path remains the worktree, not the removed plan fixture.

An independent next case still runs git successfully after an emergency kill
in the committed regression. That disproves deterministic runner poisoning
in this reproduction; it does **not** rule out an intermittent runtime issue
in the archived cascade. Neither the first archived 30-second stall nor a
causal link from its kill to later independent stalls has been reproduced.
There is no evidence here to claim a Bun defect, deleted later git cwd, or a
complete explanation of that intermittent stall cascade. The evidenced root
cause addressed by this fixture is teardown racing unfinished work: cleanup
must own child exit and pipe completion, rather than rely on eventual body
`finally` execution. Archived logs alone cannot establish more.

## Ownership and bounded cleanup proof

`fixtures/owned-children.ts` registers each handle at spawn and immediately
owns its stdout/stderr readers. `output()` uses those readers. `reap()` sends
SIGTERM only to registered live handles, escalates individually after a bounded
grace, awaits exit, then awaits reader cancellation and read-loop settlement.
Read/cancellation failures surface; handles stay registered until cleanup
succeeds. Only after awaited cleanup do tests remove fixture directories.
The tree-killing process utility is unsuitable because this task explicitly
excludes descendant/group sweeps; ordinary stream helpers do not expose the
reader and cancellation completion needed for this ownership boundary.

All nested runners use the same registry and an independent spawn timeout
(4s), below enclosing test timeout (10s); child fixtures have their own 1.5s
safety timeout. A stalled runner with no grandchildren is deliberately killed
and awaited by its parent. Another case starts a SIGTERM-resistant child,
verifies SIGKILL escalation, settled output and released reader locks before
fixture removal. No process-name or descendant signaling is used.

## OMP-640-s02: applying the boundary to the two leaking files

Before (observed in s01 above): `stamp payload parity` let teardown delete
`plan.md` while the timed-out `runExecution` continuation still read it
(ENOENT), and `live-mapping fence` held a mapper whose kill/`exited` lived in
a body `finally`. That `finally` is late, not absent: the mapper stays alive
through blocking `Bun.spawnSync` update runs, so a test deadline reaches Bun's
emergency kill ("killed N dangling process") before JavaScript resumes. The
`finally` also never released the readiness reader, and ran in addition to,
not instead of, any hook.

After:

- `omp-262-plan-identity.test.ts`: the git child is spawned inside
  `createWorkflowHost` (called code), so no handle exists to register and none
  is invented. Every `runExecution` continuation is tracked; `afterEach`
  awaits them (bounded at 2s, failing loudly) before `vi.restoreAllMocks()`
  and fixture removal.
- `update.test.ts`: mappers (shielded and unshielded, E0551 carve-out
  assertions unchanged) are spawned through `OwnedChildren` at spawn time.
  Readiness is an async-polled file (8s bound), not a stdout line, so the
  registry owns the only stdout reader. The per-case `finally` blocks are
  gone; one awaited `afterEach` reaps before directories are removed. The
  refusal case reaps mid-test only because the second run needs the mapping
  gone. Case timeouts (60s) now exceed the sum of blocking commands plus the
  readiness bound, so a hang is cleaned up by the hook before Bun's kill.
- `process-lifecycle.test.ts` `continuation-deadline`: a registered child is
  left alive, a synchronous probe is interrupted by the 100ms deadline, and the
  continuation reads its fixture afterward. Output shows `plan:read` before
  `hook:reaped` (no ENOENT), then the next case's git init/config exits 0.

Causal reduction: each leaking case now ends with its owned children exited
and readers settled before fixture deletion on success, assertion failure and
operation deadline; the only remaining "killed N" output is for deliberate
test-deadline scenarios inside the nested lifecycle runner.
