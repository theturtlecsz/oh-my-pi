# OMP-640 — process lifecycle around `killed N dangling process`

Owner: OMP-640-s01. Scope: the `session-system/tests` run under the gate
(`bun test session-system/tests --parallel=1 --timeout=30000`, empty `HOME`, no
`uv`). This document records the observed mechanism, the actual child process
behind the diagnostic, and why the two named tests were the first to emit it.

## Environment

- Bun `1.4.2` (`bun --version` → `1.4.2`; banner `bun test v1.4.2 (744846f84)`).
- Linux, `bunfig.toml` `[test] preload = ["./scripts/session-system-test-preload.ts"]`
  gives each run a private `mkdtemp` `TMPDIR` (`/tmp/omp-session-test-*`), removed
  on `process.on("exit")` and `afterAll`.
- Gate command from the archived failure:
  `bun test session-system/tests --parallel=1 --timeout=30000`.

Commands used to reproduce the diagnostic in isolation:

- `bun test --timeout 1 session-system/tests/omp-262-plan-identity.test.ts`
  — the exact archived signature (dangling line + `ENOENT` on `plan.md`).
- `bun test session-system/tests/update.test.ts` — the mapper fence path
  (passes; the mapper is reaped by its own `finally` on the non-deadline path).

## The two causal paths and their actual children

The diagnostic is not tied to a test *name*; it is tied to a child that is still
live at the moment Bun enforces a test's deadline. Two session-system paths
reach a long-lived child:

1. **`omp-262-plan-identity.test.ts` → `stamp payload parity`.**
   `runExecution` calls `createWorkflowHost(...)` (`host.ts`), and
   `createWorkflowHost` runs a **synchronous** git probe at `host.ts:740`:

   ```ts
   const gitRooted = spawnSync("git", ["-C", process.cwd(), "rev-parse", "--show-toplevel"], { encoding: "utf8" }).status === 0;
   ```

   This is the only child the path spawns. Instrumenting `Bun.spawnSync`
   (a wrapper around the real call, logging `pid`/`status`/`signalCode`/`argv`)
   while running this file shows three such calls, each returning **killed by
   the deadline** rather than a status:

   ```
   +1111ms Bun.spawnSync pid=1649104 status=null signal="SIGTERM" argv=["git","-C","/home/ompbot/flood-repos/oh-my-pi-wt/OMP-640-s01","rev-parse","--show-toplevel"]
   +1118ms Bun.spawnSync pid=1649108 status=null signal="SIGTERM" argv=[...]
   +1120ms Bun.spawnSync pid=1649109 status=null signal="SIGTERM" argv=[...]
   ```

   Owned PID/argv: the child PID is the `git` process; argv is
   `git -C <repo-root> rev-parse --show-toplevel`; cwd is `process.cwd()`;
   exit is `status=null`, `signalCode="SIGTERM"`.

2. **`update.test.ts` → `live-mapping fence (OMP-157)`.**
   The fence spawns a python mapper that `mmap`s a fixture file and then
   `time.sleep(600)`:

   ```ts
   const mapper = Bun.spawn(["python3", mapperPy, mappedReal], { stdout: "pipe", stderr: "ignore" });
   ```

   Owned PID/argv: `python3 <tmp>/mapper.py <realpath-of-mapped-file>`
   (plus `shield` in the second case). On the normal path the test already does
   `finally { mapper.kill(); await mapper.exited; }`.

The archived cascade's first dangling line lands here
(`/home/ompbot/flood/flood-logs/OMP-612-s04.test.1.log:1066`), immediately after
`stamp payload parity` case 3 and immediately before an unhandled
`could not read plan file /tmp/omp-262-exec-00y9KW/plan.md: ENOENT`.

## Chronology: spawn, deadline, exit, hooks

For one test that overruns its `--timeout`:

1. The test body spawns a child (`git` via `spawnSync`, or `python3` mapper) and
   keeps it live.
2. The test body blocks/awaits past the deadline (in the archived run, the
   synchronous git probe stalls; in isolation, an artificial `sleep` past a
   short `--timeout` produces the identical line).
3. Bun's deadline enforcement fires on a separate thread: it **kills the live
   child** and prints `killed N dangling process` **at that instant**.
4. The test body then **resumes**; its `await`/blocking call returns with the
   child already dead. For `spawnSync`, the return is `status=null`,
   `signalCode="SIGTERM"` (step 1 log above).
5. Only **after** the body settles does the awaited `afterEach` run — the
   fixture dirs (`mkdtemp` under the private `TMPDIR`) are removed there.

Two facts distinguish "first stall" from "later kills":

- The **first** dangling line is the *stall itself*: a test exceeded its
  deadline while holding a live child, so Bun killed it.
- **Later** dangling lines are other, independent tests reaching their own
  deadline under the same condition. They are not caused by the earlier kill.

## Why later spawns fail (evidenced)

Later spawns do **not** fail because the runner was poisoned by the earlier
dangling kill. Controlled reproductions:

- Same file, next test after a dangling kill: `Bun.spawnSync(["git","--version"])`
  and `Bun.spawnSync(["git","init","-q"])` both **exit 0** (see
  `session-system/tests/process-lifecycle.test.ts`, case 4).
- Cross file (`/tmp` probe): file A stalls past a 300 ms deadline and is
  dangling-killed; file B then runs `git --version` and `git init` — both exit
  **0**, under both the default runner and `--parallel=1`.
- `node:child_process.spawnSync` after a dangling kill also exits 0.

What actually produces the observed later failures is the **body resuming after
the deadline into a fixture the hook already deleted**:

- `host.ts` then does `readFileSync(planPath)` and gets
  `ENOENT: .../omp-262-exec-*/plan.md`, which the test surfaces as
  `stamp_execution_plan failed: could not read plan file ...`.
- Equivalently, a `spawnSync` whose `cwd` has been removed **throws**
  `ENOENT: no such file or directory, posix_spawn 'git'` (reproduced directly),
  and a later `git config` in a removed dir exits non-zero with **empty stderr**.

So the correct statement of the cascade is: *a test that overruns its deadline
leaves a live child; Bun kills that child and prints the diagnostic, then the
test body resumes against state its `afterEach` already tore down, so it fails
with `ENOENT`/failed assertions. It does not poison the runner — every later
`git`/`spawnSync` still starts.*

## Why `try/finally` is insufficient, and why `afterEach` is the fix

Instrumentation with `spawnSync`/`Bun.spawn` wrappers and a probe test that
registers a live child, then overruns its deadline:

- With cleanup only in the test body's `try/finally`, a **`finally` never runs**
  on the deadline path: the probe's `events` array stays empty while Bun prints
  `killed 1 dangling process`. Bun's deadline kill happens before any pending
  `finally`, so a `finally`-based `kill()`/`await exited` cannot fire.
  (This is why the mapper's `finally { mapper.kill(); await mapper.exited; }` in
  `update.test.ts` is insufficient once the test reaches its deadline.)
- With cleanup in an awaited `afterEach`, the hook **does run** and reaps the
  child (`afterEach: pid=... alive=true` → `exited code=...`).
- A test that **returns normally** (no deadline) whose `afterEach` reaps the
  child prints **no** dangling line at all — Bun's dangling check for the
  normal path happens after `afterEach`.

Conclusion: register every child at spawn and reap it in an awaited
`afterEach` (`fixtures/owned-children.ts`). That is the only hook that runs on
both the normal-return and the deadline paths, so no test ever ends with a live
child and no deadline overrun is needed to clean up. `session-system/tests/process-lifecycle.test.ts`
exercises the normal path (child reaped, next `git init`/`git config` exits 0),
a deliberate assertion failure with a live child (reaped; file finishes
cleanly), and a child-live test driven past a short deadline (diagnostic prints,
the next `git` spawn still exits 0) — proving a deliberately left child does
not make the next test's git spawn fail.

## Reproduction / verification

```sh
# exact archived signature (dangling line + ENOENT on plan.md)
bun test --timeout 1 session-system/tests/omp-262-plan-identity.test.ts

# healthy baseline: 8 pass
bun test session-system/tests/omp-262-plan-identity.test.ts

# the proof suite: 4 pass, no "killed N dangling process"
bun test session-system/tests/process-lifecycle.test.ts
```
