# OMP-628 auth isolation evidence

Scope: OMP-628-s01-s01-s01, ledger r1. Existing tests and source remain unchanged.
This change adds characterization controls and evidence; it does not claim the
follow-up coordinator isolation repair or the item's 20 consecutive chunk runs.

## Cause and call path

The shared mutable state is the `AuthStorage`/SQLite credential pool created once
in `coordinator.test.ts`'s `beforeAll`, reused by every coordinator case and its
`ModelRegistry`. Its supposedly immutable OAuth fixture can be soft-disabled by
background skill compression started by the first real SDK session. The timing
boundary is session disposal: compression is not drained there, so a pending
refresh can mutate the caller-owned auth after the scan finishes and before a
later case selects the pinned row. Agent-dir skill discovery and the persistent
skill-description cache decide whether that background work starts at all.

Production path traced:

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

## Executed controls

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

## Peer inclusion/exclusion bisect

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
