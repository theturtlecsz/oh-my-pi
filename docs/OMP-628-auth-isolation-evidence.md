# OMP-628 auth isolation evidence

Scope: OMP-628-s01-s01-s02-s03, ledger OMP-628 r1. This revision consumes
s02's SDK fix and default-session regression, extends the s01 audit, and changes
only this document and its fork-inventory row. SDK source was temporarily
reversed for the negative control and restored byte-for-byte in `finally`;
tests and fixtures were unchanged. Historical controls, bisect results and
master-report dispositions remain below. New execution evidence appears under
“s03 reversal and restored verification”; historical pending scope is superseded
there, not presented as the current SDK behavior.

## Cause and call path

The shared mutable state is the `AuthStorage`/SQLite credential pool created once
in `coordinator.test.ts`'s `beforeAll`, reused by every coordinator case and its
`ModelRegistry`. Its supposedly immutable OAuth fixture can be soft-disabled by
background skill compression started by the first real SDK session. The timing
boundary is session disposal: compression is not drained there, so a pending
refresh can mutate the caller-owned auth after the scan finishes and before a
later case selects the pinned row. Agent-dir skill discovery and the persistent
skill-description cache decide whether that background work starts at all.

Pre-fix production path traced (historical):

1. `security/coordinator.ts:createDefaultSecuritySession` passes the host's
   `authStorage` and `modelRegistry` to `createAgentSession`, without a private
   `agentDir`. `disableExtensionDiscovery` does not disable skill discovery.
   `bindProcessState: false` does not isolate credentials or compression.
2. `sdk.ts:createAgentSessionScoped` defaults `agentDir` through `getAgentDir()`;
   it deliberately uses `modelRegistry.authStorage`, checks identity when both
   options are supplied, and does not own externally supplied auth storage.
3. SDK builds `SkillDescriptionCatalog` at `<agentDir>/skill-descriptions.db`
   with `createSkillDescriptionCompressor(modelRegistry, settings)`.
   `system-prompt.ts` renders visible skills when a skill reader is available.
   `SkillDescriptionCatalog.render` schedules uncached work asynchronously.
4. `extensibility/skill-descriptions.ts:createSkillDescriptionCompressor` selects
   smol/tiny independently of the foreground scan model. It calls
   `ModelRegistry.getApiKey`, then `completeSimple` with `registry.resolver`.
   Thus a mock foreground scan does not make compression a mock request.
5. `config/model-registry.ts:getApiKey` delegates to `authStorage.keys.get`;
   `resolver` uses the same auth cascade. A 401 reaches the stream auth-retry
   driver, whose resolver requests forced OAuth refresh. `auth/select.ts`
   preflights that refresh through `OAuthRefresher.refresh`.
6. With SQLite refresh leases, `auth/refresh.ts:refreshStored` catches definitive
   failure and calls `store.tryDisableAuthCredentialIfMatches` with the durable
   row id, serialized credential and lease owner. The successful CAS sets
   `auth_credentials.disabled_cause`, removes the row from the active pool and
   emits the disabled event. The selector also has
   `disableDefinitiveFailure`/`pool.disableIfMatches` for definitive failures
   outside that lease-owned mutation. These are soft-disable paths, not deletion.
7. Neither the SDK dispose wrapper nor `AgentSession.dispose` joins the skill
   catalog's `waitForPending()`. `security/auth.ts:selectOAuthAccount` later sees
   no active account and throws `Security OAuth credential 1 is not available
   for openai-codex`. A caller-owned storage instance remains usable after
   session disposal, including by already-started compression.

## Historical executed controls

`test/security/coordinator-auth-isolation.test.ts` uses a fresh private directory,
agent-dir and real SQLite storage per case. `isolateHost` restores environment and
directory resolution; settings and the SDK agent registry are private fixtures.
No source text is inspected by tests.

- Expired pinned row: real `oauth.accessById` enters the instance refresh hook.
  Entry/release barriers prove the account and active row still exist before
  rejection. Releasing `invalid_grant` produces a disabled tombstone with the
  same id and cause, no active row, and failed pinned selection after reload.
- Runtime and config key overrides: both suppress `oauth.accounts` and
  `accessById`, producing the same selection error while the durable row stays
  active and the disabled-row list stays empty. Removing each override restores
  successful selection and access by the original id without refresh.
- Actual SDK startup with a visible skill: the real compressor, registry auth
  methods, stream auth retry and SQLite CAS execute. An instance `getAvailable`
  spy restricts model selection to a local mock transport; the foreground model
  is separate. The mock compression response supplies 401. The instance
  `refreshOAuthCredential` hook blocks before rejecting; `usageFetch: typeof
  fetch` supplies local usage responses and its `preconnect` member without casts.
  An explicit usage report exercises that seam before startup. No provider
  transport or real fetch is used. A call-through compressor-factory spy records
  pending work only, so it can be joined in cleanup.
  Selection succeeds at refresh entry and again after `session.dispose()`.
  Release/join then exposes the disabled durable row and failed pinned selection.
  This proves compression can mutate shared auth after disposal even though the
  original access token has an hour remaining.

Every case releases/joins its work, restores spies, disposes/unregisters the SDK
fixture, unregisters the mock API, closes storage and removes its private root.
No sleeps, retries, skips, timeout increases, network mocks or `mock.module`.
The direct refresh control uses an expired row because `accessById`'s current
`tryOAuth` path does not force a still-fresh row merely from `forceRefresh: true`;
that separate source behavior is outside this task.

Command from `packages/coding-agent`:

```sh
bun test test/security/coordinator-auth-isolation.test.ts
```

Result: 4 pass, 0 fail, 31 assertions. Early fixture-development runs failed
(missing installed dependencies, then incorrectly wired transport/skill-reader
fixtures); those were corrected without editing source, existing tests or timeouts.
Dependencies were installed with `bun install --frozen-lockfile` and native
binaries refreshed with `bash session-system/refresh-natives.sh`; neither changed
tracked files.

## Historical logs read

Relevant chunk output and coordinator reruns were read from
`~/flood/flood-logs/`:

| Log | Observation |
| --- | --- |
| `OMP-595-s07-s05-s03.test.1.log` | Chunk 56/90: first scripted scan returned partial instead of completed, then five cases failed pinned-account selection; isolated rerun passed all seven. |
| `OMP-603-s05-s04.test.1.log` | Chunk 56/90 passed; overall TypeScript run reported 203 chunks passed, zero failed. |
| `OMP-603-s05-s04.test.2.log` | Chunk 56/90: restart recovery failed pinned-account selection; isolated rerun passed all seven. |
| `OMP-622-s06.test.1.log` | Chunk 56/90: five cases failed pinned-account selection starting at initial-persistence failure; isolated rerun passed all seven. |

The logs report selection failure, not a durable-row snapshot or refresh trace.
They cannot by themselves distinguish suppression, deletion and soft-disable, or
identify a particular historical network response. The controlled SDK execution
above proves the compression/refresh soft-disable mechanism and reproduces the
same downstream error; it does not pretend to recover missing historical traces.

## Historical peer inclusion/exclusion bisect

All commands below ran from `packages/coding-agent`, with existing tests unchanged
and `--parallel=1`. No gate, full suite or runner/bucket changes. Baseline
`bun test test/security/coordinator.test.ts`: 7 pass, 0 fail.

### full

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  56 pass;  0 fail; Ran 56 tests across 10 files. [3.94s]

### left

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts --parallel=1
```

Exit 0;  26 pass;  0 fail; Ran 26 tests across 5 files. [2.26s]

### right

```sh
bun test test/security/coordinator.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  37 pass;  0 fail; Ran 37 tests across 6 files. [2.61s]

### include contracts

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts --parallel=1
```

Exit 0;  13 pass;  0 fail; Ran 13 tests across 2 files. [940.00ms]

### include gate

```sh
bun test test/security/coordinator.test.ts test/security/gate.test.ts --parallel=1
```

Exit 0;  10 pass;  0 fail; Ran 10 tests across 2 files. [1220.00ms]

### include history

```sh
bun test test/security/coordinator.test.ts test/security/history.test.ts --parallel=1
```

Exit 0;  11 pass;  0 fail; Ran 11 tests across 2 files. [1340.00ms]

### include importers-store

```sh
bun test test/security/coordinator.test.ts test/security/importers-store.test.ts --parallel=1
```

Exit 0;  13 pass;  0 fail; Ran 13 tests across 2 files. [1321.00ms]

### include preflight

```sh
bun test test/security/coordinator.test.ts test/security/preflight.test.ts --parallel=1
```

Exit 0;  22 pass;  0 fail; Ran 22 tests across 2 files. [1270.00ms]

### include publication

```sh
bun test test/security/coordinator.test.ts test/security/publication.test.ts --parallel=1
```

Exit 0;  13 pass;  0 fail; Ran 13 tests across 2 files. [1276.00ms]

### include remediation

```sh
bun test test/security/coordinator.test.ts test/security/remediation.test.ts --parallel=1
```

Exit 0;  10 pass;  0 fail; Ran 10 tests across 2 files. [1249.00ms]

### include seeded-fixture

```sh
bun test test/security/coordinator.test.ts test/security/seeded-fixture.test.ts --parallel=1
```

Exit 0;  8 pass;  0 fail; Ran 8 tests across 2 files. [964.00ms]

### include slash-command

```sh
bun test test/security/coordinator.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  12 pass;  0 fail; Ran 12 tests across 2 files. [1402.00ms]

### exclude contracts

```sh
bun test test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  50 pass;  0 fail; Ran 50 tests across 9 files. [4.01s]

### exclude gate

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  53 pass;  0 fail; Ran 53 tests across 9 files. [3.62s]

### exclude history

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  52 pass;  0 fail; Ran 52 tests across 9 files. [3.69s]

### exclude importers-store

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  50 pass;  0 fail; Ran 50 tests across 9 files. [3.64s]

### exclude preflight

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  41 pass;  0 fail; Ran 41 tests across 9 files. [3.69s]

### exclude publication

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  50 pass;  0 fail; Ran 50 tests across 9 files. [3.43s]

### exclude remediation

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  53 pass;  0 fail; Ran 53 tests across 9 files. [3.69s]

### exclude seeded-fixture

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0;  55 pass;  0 fail; Ran 55 tests across 9 files. [4.26s]

### exclude slash-command

```sh
bun test test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts --parallel=1
```

Exit 0;  51 pass;  0 fail; Ran 51 tests across 9 files. [3.48s]

No peer is necessary or sufficient to reproduce a failure in these runs: full
chunk, both halves, every individual inclusion and every single-peer exclusion
passed. This narrows the available evidence to state/timing within the reused
coordinator fixture, rather than attributing the failure to a peer by proximity.
Passing samples do not establish absence of a scheduling-dependent peer effect
or satisfy the item's separate 20-run acceptance criterion.

New controls also ran together with the unchanged chunk:

```sh
bun test test/security/coordinator-auth-isolation.test.ts test/security/contracts.test.ts test/security/coordinator.test.ts test/security/gate.test.ts test/security/history.test.ts test/security/importers-store.test.ts test/security/preflight.test.ts test/security/publication.test.ts test/security/remediation.test.ts test/security/seeded-fixture.test.ts test/security/slash-command.test.ts --parallel=1
```

Exit 0: 60 pass, 0 fail, 207 assertions across 11 files. Final standalone command
above also passes after formatting and cleanup-barrier refinements.

## Historical s01 r1 scope and pending acceptance

The following records the s01 audit before s02; its pending work is addressed
by the s02 fix and s03 verification below. Source read for that audit: `packages/coding-agent/src/sdk.ts` catalog
construction and disposal, `src/security/coordinator.ts` default session, and
`src/security/auth.ts` selection/resolver (all `src/` paths in this paragraph
are under `packages/coding-agent/`). SDK constructs the catalog with
`createSkillDescriptionCompressor(modelRegistry, settings)`, outside the
caller-owned `getApiKey` used by the foreground agent. Cold-cache compression
therefore uses shared registry auth; a late definitive refresh rejection can
soft-disable the reused SQLite credential after session disposal. A warm cache
can mask that path. Private foreground model/auth selection alone does not
isolate this background consumer.

Default security session supplies host storage/registry and an exact-account
`getApiKey`, but no private agent directory. Exact OAuth resolver targets the
pinned durable row for both initial access and refresh, rejects provider or
durable identity changes, and returns no sibling credential on last chance.
Those boundaries remain required. Disabling a row elsewhere makes pinned
selection fail closed; changing auth semantics to rotate accounts would defeat
that contract rather than fix shared-state ownership.

Existing observable controls (read, not rerun here):

- `packages/coding-agent/test/security/coordinator-auth-isolation.test.ts`:
  definitive rejection leaves a disabled tombstone and failed pinned selection;
  runtime/config overrides hide OAuth reversibly without disabling the row;
  real SDK cold-cache compression with a local 401 and refresh barriers leaves
  selection working through disposal, then disables the row after release.
  This is a mechanism reproduction, not a foreground `security_publish` scan
  acceptance run.
- `packages/coding-agent/test/security/auth.test.ts`: explicit row selection
  avoids rotation; initial access and error-triggered refresh resolve that same
  row; last-chance lookup returns no sibling; provider/identity changes fail
  closed, including loss of workspace identity. Provider-owned auth is accepted
  only on supported routes and remains pinned to provider/API. These contracts
  do not prove that all background consumers use the caller resolver.

Pending item acceptance belongs to follow-up work: isolate reused state in
`coordinator.test.ts` (private agent dir/auth storage/singleton reset), or fix
source ownership/race; preserve every case and assertion without skips or
merely raising timeouts. Then show the containing 10-file chunk passing 20
consecutive runs with `--parallel=1`, plus the file alone. Historical standalone,
chunk and bisect successes above are samples, not that acceptance proof.
Other background-auth consumers and unrelated report findings remain outside
this narrow audit. No source repair, new test, changelog, runner, contract or
external-system change is made here.

## s03 reversal and restored verification

Executed 2026-10-15 on branch `OMP-628-s01-s01-s02-s03`, starting at
`5cb802da7c`, Bun `1.4.2 (744846f84)`. The s02 fix makes
`SkillDescriptionCatalog` omit `compress` when `options.getApiKey` is supplied;
ordinary sessions still construct the real compressor. This removes the
unpinned background consumer from caller-owned auth sessions rather than
weakening exact-account selection or hiding a disabled row. Shared SQLite auth
and refresh timing remain the cause described above; a private foreground
resolver alone did not constrain compression's separate registry cascade.

### Negative control and restoration

Only this expression in `packages/coding-agent/src/sdk.ts` was temporarily
reversed; comments, tests, fixtures and all other source stayed unchanged:

```ts
// Fixed
...(options.getApiKey ? {} : { compress: createSkillDescriptionCompressor(modelRegistry, settings) }),
// Reversed
compress: createSkillDescriptionCompressor(modelRegistry, settings),
```

An in-memory Python `try`/`finally` saved the original bytes, replaced that one
expression, ran the command below through `subprocess.run` (120-second outer
watchdog; no test timeout changed), and restored the saved bytes in `finally`.
The watchdog did not fire. No scratch source or test instrumentation was added.

From `packages/coding-agent`:

```sh
bun test test/security/coordinator-auth-isolation.test.ts
```

Two preliminary reversal invocations were invalid proofs: the first stopped at
missing workspace module `@oh-my-pi/pi-natives/vcs`; after
`bun install --frozen-lockfile`, the second stopped at a missing native addon.
Each reported 0 pass, 1 fail, 1 error and restored SDK bytes in `finally`.
`bash session-system/refresh-natives.sh` installed the package-matching 18.4.8
binaries. Both prerequisite commands exited 0 without tracked changes. No test
environment overrides, fixtures, retries, assertions or timeouts were changed
to obtain the following result.

The third reversal invocation executed all five cases and exited 1:

```text
436 | expect(compressorFactory).not.toHaveBeenCalled();
error: expect(received).not.toHaveBeenCalled()
Expected number of calls: 0
Received number of calls: 1
(fail) coordinator shared-auth isolation controls > the default security session keeps pinned auth out of background compression
4 pass
1 fail
38 expect() calls
Ran 5 tests across 1 file. [1028.00ms]
```

This is the forbidden background-auth path assertion, reached after real
publication, not an early scan failure or hang. Ordering follows the unchanged
regression's awaited barriers and assertions, all before line 436:

1. Cold-cache compression invokes the call-through factory and captures its work
   in `pending`. Unpinned registry `getApiKey` is held at `authGate` before the
   underlying auth runs; the foreground session uses pinned `oauth.accessById`.
2. The foreground calls real `security_publish`'s wrapper. It records entry,
   resolves `publicationEntered`, then opens `authGate`. Only publication entry
   permits the captured compression auth request to proceed.
3. The test awaits publication entry, releases the fixture refresh rejection,
   then joins `Promise.allSettled(pending)` while publication's return is held.
   Thus captured compression/auth/refresh settles before publication release.
4. `publicationRelease` opens; real publication executes canonical bundle,
   SARIF and store writes, and its wrapper joins captured work again before
   returning. The coordinator wait then reaches its terminal state.
5. Assertions pass for publication entry, exactly one publication call, terminal
   `completed` with one finding, and stored bundle `completed` with one finding.
   Only then does the zero-compressor assertion fail (one call).
6. Cleanup releases every gate, joins pending work and coordinator drain,
   disposes captured sessions, unregisters them, restores spies and closes the
   private fixture. The earlier SDK-disposal control also passed, independently
   asserting real 401/refresh soft-disable and failed pinned selection.

The negative control stops at the first forbidden-compressor assertion; later
zero-unpinned-call and durable-identity assertions are not claimed as executed
in that failing case. Barrier ordering proves captured auth was joined before
completed publication and failure. The unchanged fixture restricts compression
to the local mock transport; no external provider response is claimed.

All three reversals restored identical SDK SHA-256 before/after:
`7aeffe606b0032181723b8a13a0985e77b02ec12232c0182178e7520365bf299`.
Byte equality was asserted in `finally`; `git diff -- packages/coding-agent/src/sdk.ts`
was empty after restoration. No source or test change is part of this revision.

### Restored standalone results

Each command ran individually from `packages/coding-agent`, after restoration:

| Command | Exit | Pass / fail | Assertions | Reported time |
| --- | --- | --- | --- | --- |
| `bun test test/security/coordinator-auth-isolation.test.ts` | 0 | 5 / 0 | 55 | 1117.00ms |
| `bun test test/security/coordinator.test.ts` | 0 | 7 / 0 | 28 | 1010.00ms |
| `bun test test/security/auth.test.ts` | 0 | 8 / 0 | 26 | 763.00ms |

The restored regression reaches the later assertions: no compressor or unpinned
request, pinned access used, default session disposed/unregistered, and next
preflight preserves the full durable account identity.

### Twenty consecutive chunk invocations

From `packages/coding-agent`, one loop, stopping on first failure:

```sh
for run in {1..20}; do bun test test/security/{contracts,coordinator,gate,history,importers-store,preflight,publication,remediation,seeded-fixture,slash-command}.test.ts --parallel=1 || exit; done
```

Loop exited 0. Every invocation reported 56 pass, 0 fail, 176 assertions across
10 files. No failed run was discarded or counter reset.

| Run | Exit | Pass / fail | Reported time |
| --- | --- | --- | --- |
| 1 | 0 | 56 / 0 | 3.55s |
| 2 | 0 | 56 / 0 | 3.57s |
| 3 | 0 | 56 / 0 | 3.58s |
| 4 | 0 | 56 / 0 | 3.57s |
| 5 | 0 | 56 / 0 | 3.49s |
| 6 | 0 | 56 / 0 | 3.92s |
| 7 | 0 | 56 / 0 | 3.65s |
| 8 | 0 | 56 / 0 | 3.60s |
| 9 | 0 | 56 / 0 | 3.42s |
| 10 | 0 | 56 / 0 | 3.37s |
| 11 | 0 | 56 / 0 | 3.39s |
| 12 | 0 | 56 / 0 | 3.33s |
| 13 | 0 | 56 / 0 | 3.38s |
| 14 | 0 | 56 / 0 | 3.42s |
| 15 | 0 | 56 / 0 | 3.48s |
| 16 | 0 | 56 / 0 | 3.64s |
| 17 | 0 | 56 / 0 | 3.48s |
| 18 | 0 | 56 / 0 | 3.48s |
| 19 | 0 | 56 / 0 | 3.58s |
| 20 | 0 | 56 / 0 | 3.49s |

### Static check and scope conclusion

From repository root, `bun run ci:check:full` exited 0. It ran `check:ts`:
oxlint, oxfmt checks, sequential workspace package type checks and the
session-system runtime type check. Oxlint emitted warnings in existing files;
no errors or formatting failures occurred. No unrelated files were repaired.
This is the explicitly requested static check, not the full test suite or gate
script; neither full test suite nor gate script ran in this revision.

The s02 source fix plus these s03 runs now supply the formerly pending narrow
acceptance evidence: unchanged coordinator file passes alone, and its unchanged
10-file chunk passes 20 consecutive invocations with `--parallel=1`. The reversal
proves this regression detects the unpinned compressor after completed
publication with captured auth joined; restoration proves the passing path
preserves pinned credentials for a subsequent preflight. This does not promote
any historical deferred master-report disposition or claim isolation of other
background-auth consumers. No tests, source, contracts, runners, skips,
retries, timeouts, changelogs or external systems were changed in the deliverable.

### Inventory and deliverable verification

From repository root:

```sh
bun scripts/upstream-inventory.ts --write
bun scripts/upstream-inventory.ts
```

Both exited 0: the writer produced 6399 rows; the read-only check passed.
The evidence row's behavior description was refreshed to include the reversal,
restoration, standalone results, 20-run sequence and static check, while retaining
historical controls and all 30 dispositions. Inventory paths are unique; no
union-merge duplicate required removal. Only this evidence document and its
inventory row differ from the starting commit. No scratch artifacts remain.

## Historical s01 r1 master-report selection and dispositions

Read-only input: `/home/ompbot/master-report/master.json`. Current verdict means
the literal report value at audit time, not a fresh adjudication of its pinned
source. No report records were modified. Selection covers the SDK, regression
test, evidence document and inventory paths across this auth-isolation work;
SDK/test are included even though this docs-only revision does not edit them.
A citation to a touched file is not proof that every finding citing it is fixed.

Reproduce from repository root (Python standard library; prints every selected
ID, current verdict and matching citation, preserving report order):

```sh
python - <<'PYREPORT'
import json
paths = (
    "packages/coding-agent/src/sdk.ts",
    "packages/coding-agent/test/security/coordinator-auth-isolation.test.ts",
    "docs/OMP-628-auth-isolation-evidence.md",
    "docs/upstream-fork-inventory.tsv",
)
with open("/home/ompbot/master-report/master.json") as source:
    records = json.load(source)
for record in records:
    hits = [e for e in record.get("evidence", [])
            if "/src/" in e and any("/src/" + p in e for p in paths)]
    if hits:
        print(json.dumps({"id": record["id"], "verdict": record["verdict"],
                          "matching_evidence": hits}, ensure_ascii=False))
PYREPORT
```

Input SHA-256: `0b524573ee206be4d49faad0272ed6769395561c3687d161264deafedc14dad6`. Selected 30 of 1151 records. No selected record cites the regression-test or evidence-document path; matches come from SDK and inventory citations. The following preserves even truncated verdict text as stored in the report. Each disposition applies only to this revision.

### E0059

Current verdict (verbatim):

> Mechanism: The fork edited the lockfile without the documented regeneration step, and the only guard is a check CI evaluates but does not build. Consequence: `nix flake check` with builds fails on the fork; Nix-based dev/package builds see a stale dependency set (consumers of work-client are session-system only, so the omp package itself is likely unaffected - unverified). Limit: scripts/release.ts:14 regenerates nix/bun.nix at release time, so an upstream-style release would repair it.

Disposition: deferred, unresolved by this change. Nix lock regeneration/build coverage is unrelated to auth ownership; no Nix or CI repair.

### E0060

Current verdict (verbatim):

> Mechanism: Worktree confinement for flood workers is a prompt instruction without mechanical enforcement, and worktree agents have no credentialed channel to submit Work Ledger mutations, so a tracked Markdown file became a side channel for effects and pending status. Consequence: Unversioned, unreviewed changes to the operator's orchestration and unbudgeted model calls occur outside any grant; the Work Ledger is not the single status record because pending revisions live only in git Markdown with nothing that applies, expires or reconciles them. Limit: The pending-mutation contract (exact bytes, SHA-256, expected-revision refusal, readback) is rigorous; out-of-tree changes were disclosed with backups; the owner may have authorized OMP-336 outside the pins.

Disposition: deferred, unresolved by this change. Worktree enforcement and ledger reconciliation are unrelated; no orchestration or external repair.

### E0069

Current verdict (verbatim):

> supported narrow demonstration-pipeline claim; proposed medium This CLI really emits fixed candidate scores/arithmetic usage and may persist synthetic traces. It neither establishes real research outcomes nor disproves separate native autoresearch, research store or B-only omp-knowledge adapter implementations. Source docstring identifies a demo store; CLI output lacks an explicit simulation marker. No actual external ledger write, inference spend or production consumption demonstrated. Preserve P169-08 complementary detail; do not collapse every Cognee feature into this legacy demo.

Disposition: deferred, unresolved by this change. Synthetic demo records are unrelated; no pipeline or consumer qualification.

### E0076

Current verdict (verbatim):

> Mechanism: Milestone evidence was produced against shape-compatible demo adapters and recorded as proof of the capability rather than of the interface shape. Consequence: Readers of the accepted ADR are led to believe Cognee/Enola knowledge reuse and durability were proven, when the in-repo evidence path exercises a hard-coded store and an in-memory dict; decisions (platform freeze) rest on that overstated maturity. Limit: The ADR's own 'Soft leftovers' concede A1 was a §3 subset (not 72h) and that PG integration beyond 'harness façades' is outstanding; the cited PASS files (ACTIVE/PIVOT-HORIZON-*-PASS.md) are outside the repo and may describe additional real runs. A real Cognee adapter exists separately in python/omp….

Disposition: deferred, unresolved by this change. ADR demo-harness proof remains outside scope; auth controls do not qualify platform slices.

### E0081

Current verdict (verbatim):

> Mechanism: The tool writes a valid-looking default into human-owned columns, so the check cannot distinguish reviewed rows from unreviewed ones. Consequence: Upstream-update reviews rely on behavior descriptions that are often placeholders, weakening the fork-behavior accounting the guardrail is meant to provide. Limit: The machine-verified divergence set (path/scope/state/blob) is enforced on every PR (ci.yml:239-270) and is the core of the guardrail.

Disposition: deferred, unresolved by this change. Inventory placeholder validation is outside scope; describing this document row does not harden the validator.

### E0099

Current verdict (verbatim):

> Reuse concrete project-overridable credential endpoint concern; no-trust-word search is not itself proof. Python interpreter sink and unrelated module enforcement claims remain separate until their real call paths are checked.

Disposition: deferred, unresolved by this change. Project-settings trust/provenance is outside scope; no settings authority repair.

### E0103

Current verdict (verbatim):

> Mechanism: Cancellation and deadlines were added per-path (daemon client, exposure) instead of being part of the BlobBackend/ProviderFileManager contract that every publication flows through. Consequence: A slow or hung third-party host or provider Files endpoint stalls the outgoing model request with no 'fail toward inline' deadline, and aborting the turn cannot stop an upload, so an image the user chose not to send may still be published. Limit: Bun's fetch may apply its own default timeout (unverified); the agent loop may abandon the transform on abort (not examined), which would stop the wait but not the upload.

Disposition: deferred, unresolved by this change. Image publication/purge cancellation is unrelated; no lifecycle repair.

### E0112

Current verdict (verbatim):

> supported; proposed medium recovery failure with configured upload side effects fallbackContext advances to another destination then wrapper retries only if coarse source class changes. URL-to-URL advance is discarded despite publication work; inline/class-changing paths work and content-emitted responses intentionally never retry. Additional uploads follow enabled destination policy; this is not proof of an unauthorized destination or unconditional data exposure. Outer retries/backend incidents not executed.

Disposition: deferred, unresolved by this change. Image destination fallback is unrelated; no retry repair.

### E0116

Current verdict (verbatim):

> Mechanism: Consent is a single feature boolean while the destination list mixes private capability-URL serving with anonymous third-party upload, and fallback between them is automatic and silent. Consequence: Screenshots or tool-result images containing secrets or private data can be uploaded to a public anonymous host for 24 h (undeletable) whenever the tunnel destinations are unavailable, although the user only chose to 'serve images as URLs'. Limit: The feature is opt-in and the list is visible/orderable in the settings UI (ordered: true, options BLOB_BACKEND_CHOICES); provider-files and tunnels precede the anonymous host; litterbox retention is time-limited.

Disposition: deferred, unresolved by this change. Image upload destination consent is unrelated; no consent change.

### E0122

Current verdict (verbatim):

> Mechanism: The uploader contract has no cancellation/deadline parameter, so neither the turn's abort nor a per-destination timeout can reach the HTTP calls. Consequence: A slow or hanging third-party host delays or blocks request dispatch for every turn carrying the image, bounded only by runtime fetch defaults (unverified); user abort cannot stop the upload phase; many images fan out as simultaneous uploads (rate limiting -> mixed destinations within one request). Limit: Bun may apply a default fetch timeout; typical image counts per request are small; the agent loop may stop awaiting on abort at a higher level (not verified).

Disposition: deferred, unresolved by this change. Upload timeout/cancellation/concurrency is unrelated; no transport repair.

### E0152

Current verdict (verbatim):

> Mechanism: Plan docs were not updated as the implementation diverged. Consequence: Readers and future agents are pointed at nonexistent files/classes and overestimate what Cognee does. Limit: The doc openly marks qualification as unexecuted.

Disposition: deferred, unresolved by this change. Fleet-knowledge plan drift is unrelated; no plan/API reconciliation.

### E0203

Current verdict (verbatim):

> Mechanism: Provenance is recorded in the manifest for mirrored assets only, not for vendored code, and no licence file was mirrored. Consequence: The MIT attribution condition for redistributed upstream text is not visibly met in the tracked tree, and drift of the vendored helper cannot be detected against upstream. Limit: The upstream repository is not available offline; the helper's origin is inferred from its style and spec :114/:638.

Disposition: deferred, unresolved by this change. ECC provenance/licensing is unrelated; no attribution repair.

### E0251

Current verdict (verbatim):

> Mechanism: Output of an external scaffolding skill was committed as if it were a report artifact. Consequence: Files titled 'Architecture Verification Report' carry no verification; they can be mistaken for evidence and add churn (timestamps regenerated between A and B). Limit: Their text is explicit that they are stubs; nothing in tracked code cites them.

Disposition: deferred, unresolved by this change. Architecture evidence stubs are unrelated; retaining historical auth evidence does not populate them.

### E0254

Current verdict (verbatim):

> Mechanism: One safety behaviour is gated by two independent flags, and the default-on flag reuses an object created only under the default-off flag. Consequence: In default configuration /share uploads an unredacted snapshot (system prompt, tool outputs, any tokens seen in the session); only AES-GCM with a link-held key protects it, so every link holder sees secrets while the setting suggests redaction is on. Limit: Snapshot is end-to-end encrypted with the key only in the URL fragment (share.ts:490-556), so the share server cannot read it; users who enable secrets get redaction.

Disposition: deferred, unresolved by this change. Share redaction defaults are unrelated; no obfuscator or settings change.

### E0393

Current verdict (verbatim):

> Mechanism: The audit's CRIT-05 tracking (recurring first-handler-wins authority pattern) appears to have over-attributed the origin finding P082c-F01's citation of 'sdk.ts' as a site needing direct review of the taskResultAuthority mechanism, when the mechanism's actual implementation and any identity check reside in session/agent-session.ts, not sdk.ts. Consequence: If the coordinator/master report treats this batch's review of sdk.ts as having resolved the CRIT-05 question for this file, that is correct only in the negative sense (sdk.ts contains no such logic to critique) — the real question about whether taskResultAuthority/emitToolCall has a caller-identity check must be resolved by reading session/agent-session.ts and extensibility/extensions/runner.ts directly, which remai…. Limit: sdk.ts does construct the recoverSynchronousTask path that calls request.validateAuthority() and request.assertProcessingOwnership() multiple times (lines 3600-3613), showing the wiring at least invokes an authority-check hook by name at this call site during task recovery — but whether that hook pe….

Disposition: deferred, unresolved by this change. Preserve negative SDK-origin qualification; this audit does not inspect or resolve task-result authority in agent-session/runner.

### E0477

Current verdict (verbatim):

> Mechanism: Acceptance is defined as a harness that both simulates faults on a model and computes its own verdict (partly as a constant), instead of fault injection against the real authority store. Consequence: The A1 pass reads as durability proof but cannot fail for any WorkService/PostgreSQL regression; it misleads status reporting for a data-integrity promise. Limit: The real PostgreSQL store has its own idempotency/recovery tests (e.g. test_postgres_operations.py:382 idempotency_conflict; installed recovery suites run in CI); the harness may have been intended as a specification model.

Disposition: deferred, unresolved by this change. In-memory Horizon durability demonstration is unrelated; no persistence qualification.

### E0489

Current verdict (verbatim):

> Mechanism: Installed targets are hand-maintained per-file enumerations (script, CI fallback, docs) instead of being derived from the same test_installed_*.py glob that defines the protected paths. Consequence: The parent-death cleanup contract (harness PDEATHSIG plus launcher SIGTERM forwarding, session-system/runtime/run.ts:219-222) can regress unnoticed. Orphaned installed servers and ports under the flood gate, the failure OMP-332 was written for, would reappear without a signal. Limit: Developers may run the whole directory locally with the env vars set; no saved results exist either way. The harness PDEATHSIG logic is also exercised indirectly whenever other installed tests tear down.

Disposition: deferred, unresolved by this change. Installed SIGKILL regression coverage is unrelated; no target-list or skip change.

### E0562

Current verdict (verbatim):

> Mechanism: Programme specification versions are distributed as ad-hoc ZIPs outside version control, and only an early one was committed without a supersession pointer. Consequence: Readers of either pin get an outdated dependency/gate map (no H0-H5/CPK conjunctive gates, no execution-grant engine) and cannot verify the normative v9.1 sources that B's own plan cites; digests can be compared only for members unchanged since v7. Limit: B plan :11 states the V9.1 files are kept immutable under the OMP stabilization evidence root (custody exists, just not in-repo); B audit :36 records a fresh archive SHA and manifest match; v7 itself says its anchors are evidence, not future-state assumptions (00_READ_ME_FIRST.md:57-60), and 07:12-1….

Disposition: deferred, unresolved by this change. Programme archive/version drift is unrelated; no archive reconciliation.

### E0611

Current verdict (verbatim):

> Retain mismatch between automatic format routing and supplied Gemma format documentation when native tool support is false. Model behavior is unverified; manual format override remains available.

Disposition: deferred, unresolved by this change. Gemma dialect selection is unrelated; no grammar/model policy change.

### E0662

Current verdict (verbatim):

> Mechanism: Eligibility checked when stop queues work, but pending flag dispatches another capture without live eligibility/session revision check. Consequence: Finally starts another private model/learning turn after opt-out or conflicting mode; may write managed skills/memory from current snapshot. Limit: Immediate future agent_end events do honor disabled setting; aborted primary turns do not queue; disposed session wrapper refuses. Race concerns already queued eligible work.

Disposition: deferred, unresolved by this change. Queued autolearn opt-out race is separate from compression refresh; no capture lifecycle repair.

### E0682

Current verdict (verbatim):

> Duplicate of ADC-01 ordinary adviser approval-context loss; retain CC3 group provenance under one master finding. No new runtime evidence from duplicate review; CC3-005 restricted scan roster remains distinct nuance.

Disposition: deferred, unresolved by this change. Ordinary adviser approval-context loss remains separate; no adviser approval propagation change.

### E0687

Current verdict (verbatim):

> Supported restricted-scan authority mismatch in inherited advisers, conditional on enabled/resolved roster. Distinct from ADC-01 missing approval context. Primary scan, MCP, extensions and LSP have real restrictions. Disabled/unresolved advisers avoid issue; default adviser tools are read-only. Configured write/bash grant and different provider/account are conditional exposure, no live request or mutation demonstrated.

Disposition: deferred, unresolved by this change. Related security surface, unresolved: inherited adviser roster/provider/account authority is broader than cold-cache compression auth; exact foreground pinning does not qualify advisers.

### E0690

Current verdict (verbatim):

> Mechanism: Capability settings/disabled providers and active rule snapshots are process globals, while SDK initializes/replaces them for each session and rule URL handler has no session context. Consequence: Opening session B changes rule://name content/completions for session A and can change disabled-provider/extension filtering during A concurrent discovery. Same process cannot preserve distinct per-project instructions/exclusions. Limit: Single top-level CLI session sees intended snapshot; subagents deliberately inherit parent. SDK itself documents additional top-level sessions in one process for shared AsyncJobManager, so per-process singleton assumption is not universal.

Disposition: deferred, unresolved by this change. Process-global discovery/rule snapshots are separate shared state; no session-policy isolation repair.

### E0709

Current verdict (verbatim):

> Mechanism: Runnable examples and quick reference were not migrated with SDK/catalog API changes. Consequence: 07-context-files iterates Promise from discoverContextFiles without await and fails before session creation. 02-custom-model/09-api-keys-and-oauth import removed discoverModels; 09 additionally calls nonexistent ModelRegistry.create. getModel is imported from pi-ai after catalog extraction. README repeats outdated APIs and recommends Node tsx despite Bun-native dependencies. Limit: Several individual examples already await auth/skills/templates and use newer extension APIs; source is instructional but advertised runnable. Source check alone establishes Promise iteration and absent constructor method, without paid calls.; Legacy extension pi-ai shim explicitly bridges getModel;….

Disposition: deferred, unresolved by this change. SDK example API drift is unrelated; no example repair or execution.

### E0937

Current verdict (verbatim):

> Mechanism: createAgentSession replaces session.dispose with async () => and calls originalDispose() without forwarding AgentSessionDisposeOptions. Core dispose explicitly consumes reason, mnemopiConsolidateTimeoutMs and drainTimeoutMs. Consequence: SDK-created sessions ignore requested shortened drain/consolidation budgets and record generic dispose instead of supplied postmortem reason. Core-only tests/constructors can pass while installed SDK behavior differs. Limit: Underlying AgentSession honors options and coalesces repeated disposal. Wrapper calls beginDispose before its first await.

Disposition: deferred, unresolved by this change. Disposal option forwarding is separate from unjoined compression; naming post-disposal refresh does not repair caller deadlines/reason.

### E0939

Current verdict (verbatim):

> Supported dynamic background-result redaction bypass; retain high conditional on configured secret appearing in delivered output. Owner routing, session epoch and foreground/user redaction work on inspected paths. Static developer prompt bypass is deliberate; dynamic tool-origin result uses same bypass. No real secret/provider call executed.

Disposition: deferred, unresolved by this change. Background-result secret redaction is unrelated; no message transform repair.

### E0941

Current verdict (verbatim):

> Mechanism: customTools accepts CustomTool or ToolDefinition, but isCustomTool reads string __isToolDefinition while customToolToDefinition writes a Symbol marker. Plain ToolDefinitions have neither marker. Both are passed through the CustomTool adapter. Consequence: A ToolDefinition execute(id,params,signal,onUpdate,ctx) receives (id,params,onUpdate,customCtx,signal); cancellation and context-dependent tools fail or lose their expected context. Reusing the exported converter result also double-adapts it. Limit: Extensions registered through registerTool already use ToolDefinition correctly. CustomTool-only SDK inputs work through intended adapter; tools ignoring extra arguments can mask failure.

Disposition: deferred, unresolved by this change. SDK custom-tool classification/argument order is unrelated; reading SDK does not repair tool adaptation.

### E0946

Current verdict (verbatim):

> Supported persistent image-history redaction gap; retain high conditional on secret in rasterized region and outbound image-capable model. Normal inline rendering is explicitly obfuscated first. Text-only/image-blocked paths and secrets remaining in text head/tail have different exposure; no raster or provider trial executed. Previous archive source must be considered on recompaction.

Disposition: deferred, unresolved by this change. Persistent image-history redaction is unrelated; no archive/raster repair.

### E0951

Current verdict (verbatim):

> Mechanism: Each transform resolves current model shape and plans image counts/billing from it, but both tool and system caches validate only text hash. SDK retains one transformer across model changes. Consequence: Identical history after model change reuses prior model frame geometry/detail while reporting current-shape savings. Actual frames can differ from planned image budget; later provider clamping may omit frames from text already replaced by images. Limit: Planner correctly re-resolves model and protects most recent/error/image-carrying tool results; cache eviction removes no-longer-live ids. Defect requires cached text reused under changed shape.

Disposition: deferred, unresolved by this change. Model-dependent inline image cache shape is unrelated; no cache invalidation change.

### E1071

Current verdict (verbatim):

> Mechanism: The worker converts explicit off to None before constructing RpcClient. The RPC client omits --thinking for None; coding-agent startup then inherits session, model or global defaults. Consequence: ROBOMP_THINKING=off or a trusted /thinking off directive can resume higher effort, defeating an explicit latency, cost and behavior choice. Limit: Models and defaults already using off show no difference. Parsing resolves off correctly; the loss occurs at the final forwarding/restore boundary.

Disposition: deferred, unresolved by this change. Explicit thinking-off forwarding is unrelated; no worker/RPC default change.

## s02 pinned-rejection control

Sections above this heading are retained history. Executed 2026-10-15 on branch `OMP-628-s01-s02`, worktree base `50385312ca`, Bun `1.4.2 (744846f84)`. This revision consumes the existing SDK fix (`SkillDescriptionCatalog` omits `compress` when `options.getApiKey` is supplied) and does not edit source, runners, contracts, skips, retries, or timeouts. It extends `packages/coding-agent/test/security/coordinator-auth-isolation.test.ts` with one control on the same default-session path as the publication regression.

The chunk failures come from one shared SQLite `AuthStorage` pool. Background skill compression can soft-disable the pinned row after session disposal, so a later case cannot select it. The selector's sibling fallback and the auth-retry `lastChance` step would hide that by minting another account in the same pool. The new control keeps both rows in one private store. The pinned grant is expired, so the default session's exact resolver refreshes it; the refresh hook throws `OAuth refresh failed: 400 invalid_grant: refresh token revoked`. The sibling grant is fresh and its refresh would mint `sibling-minted-token`. The foreground model would publish a canonical finding if it received either bearer.

Before the pinned refresh is released, preflight has already pinned `workspace-fixture`, refresh has been called once for that credential id only, both accounts are still selectable, and the disabled list is empty. After release the scan terminal phase is `failed`, `findingCount` is 0, and both the operation and the stored bundle carry `The pinned security OAuth credential could not be resolved`. The bundle plan account is still the pinned identity. `oauth.accessById` is called only with the pinned id. Registry `getApiKey`, `getApiKeyForProvider`, `getApiKeyAndHeaders`, `getApiKeyWithCredentialForProvider`, `keys.get`, and `oauth.access` are not called. The compressor factory is not called. Foreground and smol mock calls are 0. The persisted session has no entries. After reload, pinned selection throws `not available`, the sibling account is unchanged and still selectable, the only active row is the sibling, and the disabled tombstone is the pinned id with `invalid_grant`.

The publication regression in the same file still completes and its post-disposal preflight keeps the same pinned credential. `test/security/coordinator.test.ts` is unchanged.

### Focused results

Each command ran alone from `packages/coding-agent` after the final test text:

| Command | Exit | Pass / fail | Assertions | Reported time |
| --- | --- | --- | --- | --- |
| `bun test test/security/coordinator-auth-isolation.test.ts` | 0 | 6 / 0 | 86 | 1017.00ms |
| `bun test test/security/coordinator.test.ts` | 0 | 7 / 0 | 28 | 1217.00ms |

### Twenty consecutive chunk invocations

From the repository root, one ephemeral Bun process imports `buildChildEnv` and `withTempAgentDir` from `scripts/ci-test-ts.ts`. Each iteration calls `withTempAgentDir` with `buildChildEnv()` as its base environment, so the child receives a gate-scrubbed env and a private agent dir. The child is a fresh awaited `bun` process with cwd `packages/coding-agent` and an explicit ten-file argv. There is no retry. A non-zero exit, an unparsed footer, a non-zero fail count, or a file count other than 10 stops the loop and prints that child's stdout and stderr. An earlier 20-iteration sequence also exited 0 on every iteration; its footer parser read stdout while Bun writes the summary on stderr, so those iterations are not in the table. The sequence below is the parsed run. No failed run was discarded.

```sh
bun /tmp/omp-628-s01-s02-chunk.ts
```

```ts
import { spawn } from "node:child_process";
import { buildChildEnv, withTempAgentDir } from "/home/ompbot/flood-repos/oh-my-pi-wt/OMP-628-s01-s02/scripts/ci-test-ts.ts";

const root = "/home/ompbot/flood-repos/oh-my-pi-wt/OMP-628-s01-s02";
const files = [
	"test/security/contracts.test.ts",
	"test/security/coordinator.test.ts",
	"test/security/gate.test.ts",
	"test/security/history.test.ts",
	"test/security/importers-store.test.ts",
	"test/security/preflight.test.ts",
	"test/security/publication.test.ts",
	"test/security/remediation.test.ts",
	"test/security/seeded-fixture.test.ts",
	"test/security/slash-command.test.ts",
];

for (let run = 1; run <= 20; run++) {
	const label = String(run).padStart(2, "0");
	const outcome = await withTempAgentDir(async (_agentDir, env) => {
		const childEnv: Record<string, string> = {};
		for (const [key, value] of Object.entries(env)) {
			if (value !== undefined) childEnv[key] = value;
		}
		const child = spawn("bun", ["test", ...files, "--parallel=1", "--timeout=30000"], {
			cwd: `${root}/packages/coding-agent`,
			env: childEnv,
			stdio: ["ignore", "pipe", "pipe"],
		});
		const stdoutChunks: Buffer[] = [];
		const stderrChunks: Buffer[] = [];
		child.stdout.on("data", chunk => stdoutChunks.push(Buffer.from(chunk)));
		child.stderr.on("data", chunk => stderrChunks.push(Buffer.from(chunk)));
		const exitCode = await new Promise<number>((resolve, reject) => {
			child.once("error", reject);
			child.once("close", code => resolve(code ?? 1));
		});
		return {
			exitCode,
			stdout: Buffer.concat(stdoutChunks).toString("utf8"),
			stderr: Buffer.concat(stderrChunks).toString("utf8"),
		};
	}, buildChildEnv());
	const plain = outcome.stderr.replace(/\x1b\[[0-9;]*m/g, "");
	const footer = plain.match(
		/(\d+) pass\s+(\d+) fail\s+(\d+) expect\(\) calls\s+Ran (\d+) tests across (\d+) files\. \[([^\]]+)\]/,
	);
	const summary = footer
		? `${footer[1]} pass / ${footer[2]} fail / ${footer[3]} expect / ${footer[4]} tests / ${footer[5]} files / ${footer[6]}`
		: "unparsed";
	console.log(`${label} exit=${outcome.exitCode} ${summary}`);
	if (outcome.exitCode !== 0 || !footer || footer[2] !== "0" || footer[5] !== "10") {
		console.log("--- failure stdout ---");
		console.log(outcome.stdout);
		console.log("--- failure stderr ---");
		console.log(outcome.stderr);
		process.exit(1);
	}
}
console.log("20/20 exit=0");
```

Loop exited 0. Every invocation reported 56 pass, 0 fail, 176 assertions, 56 tests across 10 files.

| Run | Exit | Pass / fail | Assertions | Files | Reported time |
| --- | --- | --- | --- | --- | --- |
| 01 | 0 | 56 / 0 | 176 | 10 | 3.41s |
| 02 | 0 | 56 / 0 | 176 | 10 | 3.48s |
| 03 | 0 | 56 / 0 | 176 | 10 | 3.26s |
| 04 | 0 | 56 / 0 | 176 | 10 | 4.29s |
| 05 | 0 | 56 / 0 | 176 | 10 | 5.25s |
| 06 | 0 | 56 / 0 | 176 | 10 | 5.40s |
| 07 | 0 | 56 / 0 | 176 | 10 | 5.43s |
| 08 | 0 | 56 / 0 | 176 | 10 | 5.56s |
| 09 | 0 | 56 / 0 | 176 | 10 | 5.06s |
| 10 | 0 | 56 / 0 | 176 | 10 | 5.09s |
| 11 | 0 | 56 / 0 | 176 | 10 | 6.07s |
| 12 | 0 | 56 / 0 | 176 | 10 | 5.57s |
| 13 | 0 | 56 / 0 | 176 | 10 | 5.24s |
| 14 | 0 | 56 / 0 | 176 | 10 | 5.09s |
| 15 | 0 | 56 / 0 | 176 | 10 | 5.12s |
| 16 | 0 | 56 / 0 | 176 | 10 | 5.23s |
| 17 | 0 | 56 / 0 | 176 | 10 | 5.17s |
| 18 | 0 | 56 / 0 | 176 | 10 | 4.82s |
| 19 | 0 | 56 / 0 | 176 | 10 | 4.80s |
| 20 | 0 | 56 / 0 | 176 | 10 | 4.76s |

### Static check

From the repository root, `bun run ci:check:full` exited 0. It ran `check:ts`: oxlint (65 warnings, 0 errors in existing files), oxfmt check, sequential workspace package type checks, and the session-system runtime type check. No formatting failures. This is the requested static check, not the full test suite or gate script.

`bun scripts/upstream-inventory.ts --write` refreshed the fingerprints for this evidence document, the isolation test, and `packages/coding-agent/CHANGELOG.md`. Their behavior descriptions name this control. `bun scripts/upstream-inventory.ts` then exited 0 with 6402 diverging paths. One row per path.

### Edited-path master-report selection

Read-only input: `/home/ompbot/master-report/master.json`, SHA-256 `0b524573ee206be4d49faad0272ed6769395561c3687d161264deafedc14dad6`. No report records were modified. Selection is evidence containing `"/src/" + path` for the paths this revision edits:

- `packages/coding-agent/test/security/coordinator-auth-isolation.test.ts`
- `docs/OMP-628-auth-isolation-evidence.md`
- `packages/coding-agent/CHANGELOG.md`
- `docs/upstream-fork-inventory.tsv`

14 of 1151 records matched. None cite the regression test or this evidence document. Inventory citations are E0059, E0060, E0069, E0076, E0081, E0152, E0203, E0251, E0477, E0489, E0562. Changelog citations are E0093, E0682, E0703. Verdicts already recorded above stay deferred. E0093 and E0703 are additional changelog citations:

### E0093

Current verdict (verbatim):

> Mechanism: A sound replay/qualification harness was added without a committed evidence corpus or a job that runs it on recorded transcripts, and the default was switched on the strength of synthetic tests. Consequence: The claim that the structured supervisor is qualified against historical advisor behaviour is not supported by evidence in the pins; regressions on real transcripts would not be caught. Limit: The harness includes negative controls that detect a dropped class and canary rollback (supervision-qualification.test.ts), and legacy rollback is one setting away (supervision-retirement.test.ts:380-407).

Disposition: deferred, unresolved by this change. Advisor replay qualification is unrelated; the changelog entry does not add a corpus or job.

### E0703

Current verdict (verbatim):

> Mechanism: Top-level no-data guard counts only active accounts and reports, excluding already-fetched actionable disabled credential summaries. Consequence: When final credential becomes disabled, normal omp usage returns no-credentials message before formatUsageBreakdown can show why login failed; helper test promising tombstone-only section does not exercise entrypoint. JSON does retain summaries. Limit: Renderer itself handles tombstone-only provider correctly and JSON includes disabledCredentials. Loss is specific to human CLI early-return boundary.

Disposition: deferred, unresolved by this change. The human `omp` usage early-return is separate from pinned security-scan rejection; no CLI guard change.

