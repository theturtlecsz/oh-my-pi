# OMP recovery coverage map

S2.1 inventory for OMP-246 and OMP-233, inspected at merge commit
`579b374cd3d2f71e59498b3ed866dc45aea743cd` (tree
`9506c61a4adbcd61567bb688642748a40bb50bd5`). This maps the failure contracts in
[the stabilization guide](omp-stabilization-plan.md); it is not a second backlog
or evidence that S2.2–S2.11 passed. Current WorkService handoffs own completion.
The commands below identify executable coverage, not results from this inventory.

## What the existing tests prove

- **Unit or component:** real functions with substituted collaborators, or real
  AgentSession/SessionManager in one process. Useful for local invariants; cannot
  establish survival of process death.
- **Service integration:** real disposable PostgreSQL, generally FastAPI
  TestClient in the test process. Proves transactions and service decisions;
  recreating an app object is not killing a service or controller process.
- **Execution smoke:** `execute-cycle-smoke.ts` starts PostgreSQL and WorkService
  and fresh harness subprocesses. Its harness substitutes SessionManager,
  extension message delivery, model transport, and auditor `runSubprocess`.
  These are real service/host scenarios, not installed session/worker recovery.
- **Installed process:** `test_installed_runtime_isolation.py` runs the admitted
  launcher, actual RPC CLI, service process, and native smoke subprocesses.
  Existing cases prove isolation and artifact refusal. They use `--no-session`,
  do not kill/restart execution, and do not qualify continuation or GitHub effects.

## Failure-to-test mapping

| Checkpoint and observable contract | Existing executable coverage | Remaining whole-process evidence |
| --- | --- | --- |
| **S2.2:** killing and restarting between stages resumes the same authorized progress | `execute-cycle-smoke.ts` recovery scenarios beginning near line 1334: happy recovery, duplicate recovery, corrupt claim, dirty worktree, HEAD/revision/project drift, exhausted continuation budget. `auditor-runner.test.ts`: `session_start relocates an active grant before recovery delivery (OMP-213)` and `execution delivery checkpoint race and crash-retry use one guarded continuation`. Commands **E**, **H**. | Kill a real controller/worker at named stages, restart the same persisted session, and observe continuation consumption or a precise refusal. Existing smoke counts captured `sendMessage` calls rather than messages consumed by AgentSession. It does not execute the original lost-message window. |
| **S2.3:** a ledger commit followed by a lost response reconciles exactly once | `pending-ops.test.ts`: `resolved create claim survives delivery ack and identical create reuses stored result with one POST`; unresolved claim retention. `test_workflow_service.py`: `test_receipt_idempotency_exact_retry_returns_original_row`, `test_completion_evidence_idempotency_and_claim_race`. Execution smoke's `crash-gap` scenario, near line 1347, writes a synthetic resolved claim after a successful command. Commands **P**, **W**, **E**. | Forward a real client command to the actual service, withhold its committed response, kill the issuing process, and recover from the client-written unresolved journal. Assert the same operation is replayed and the same service transition/receipt returned. Do not write a replacement pending-operation file in the test. |
| **S2.4:** a successful push/merge with a lost response is discovered before retry, with no duplicate effect | `commit-step.test.ts`: `pushCandidate pushes the exact frozen commit and repeated checks stay idempotent`, containment and diverged-remote refusal against real local bare Git remotes. `git-reliability.test.ts`: unchanged audited head merges; a changed head after precheck is refused. The latter uses a local `gh` fixture. Commands **G**. | Kill the calling process after a real remote effect but before its acknowledgement; restart and prove remote inspection, exact candidate identity, and one effect. Local bare Git can prove push behavior. A local GitHub simulator does not establish GitHub API recovery; any disposable GitHub trial needs its separately recorded authority. |
| **S2.5:** queued work delivered after cancellation starts no new work or effect | `auditor-runner.test.ts`: guarded continuation race, `session_stop with dead execution grant or terminal work emits no continuation`, dead-grant startup cleanup. Execution smoke covers cancellation/terminal recovery. `session/yield-queue.test.ts` drops stale entries at injection. Commands **H**, **E**, **Y**. | Hold an actual continuation between durable intent, queueing, injection, and consumption; cancel through WorkService; release it or restart. Assert no new provider request, task subprocess, tool effect, or grant transition from the stale work. Host tests checking before enqueue do not cover cancellation after enqueue. |
| **S2.6:** broken auditor discovery leaves stop/cancel usable and prevents review success | `execution-halt.test.ts` has owner-interjection pause, cancel, `stop_execution`, and resume refusal with unavailable auditor. `auditor-runner.test.ts` covers missing role/schema/credentials and failed preflight before reservation. Commands **H**. | Exercise those actions in an installed controller with a disposable, deliberately incompatible auditor configuration. Observe terminal/paused service state and zero successful audit/launch. Keep the admitted release immutable; do not corrupt the live auditor. |
| **S2.7:** incompatible runtime/service versions fail precisely; trusted-runtime changes need fresh applicable authority; terminal grants remain terminal | `test_workflow_service.py`: contract mismatch handshake; stale-service read/write split; `test_execution_grant_pause_resume_and_terminal_judge_drift`; service-refresh stale-source/drift matrix; pre-review replan and stale-service pause/stop. `test_service_readiness.py` tests readiness without masking database failure. Runtime-stage tests refuse Bun mismatch, changed Python identity, and invalid manifest content. Installed isolation tests refuse a corrupted disposable release. Commands **W**, **R**, **I**. | Combine actual incompatible installed host/service processes with an existing grant. Prove exact refusal, unchanged terminal grants, and fresh authority where required. Existing service-refresh test constructs a new in-process service object for its restart step. Isolation qualification is not grant recovery qualification. |
| **S2.8:** OMP-262's plan-stamp result matches its persisted write outcome; OMP-264 audit repository identity remains usable | `auditor-runner.test.ts` tests replan in executing phase and dirty-path refusal. Service tests cover replan, candidate collision and stale evidence. No identified test runs OMP-262's exact manual-plan → execution-stamp-without-active-grant sequence. OMP-264's source path is present: admission stores `basename(primaryRoot)` and execution closeout forwards `exec.grant.repository`. Commands **H**, **W** are adjacent coverage only. | Reproduce both separately before changing code. For OMP-262 compare returned result and live workflow candidate/receipt before and after the stray stamp. For OMP-264 run the real audit path from the sealed repository identity; current fake auditor reports never execute its `git -C` instruction. Do not infer either checkpoint passed from source inspection. |
| **S2.9:** CI requires the actual recovery journeys and retains process/effect evidence | `.github/workflows/ci.yml` runs PostgreSQL integration, candidate smoke, execution smoke, then installed isolation; retains installation manifest and JUnit. Package scripts **W**, **E** and command **I** expose these layers. | Add each new journey to required CI once implemented. Record process identities, fault barriers, signal/exit status, journal/operation IDs, observed effects, installed manifest and output. Existing required smoke does not silently become whole-process recovery evidence. No recovery journey was added by this inventory. |
| **S2.10:** completed/canceled predecessors' active historical edges alone do not prevent admission or recovery | `work.ts::snapshotQueue` checks predecessor state when excluding blocked work. Recovery preflight in `host.ts` rejects every active incoming `blocks` edge without reading predecessor state. Execution smoke's blocker case near line 1534 uses an unfinished predecessor. Commands **E**, **H** have adjacent rejection coverage. | Reproduce a terminal predecessor with its historical edge retained; compare admission and same-grant recovery. Cover completed and canceled states separately from an actually unfinished predecessor. Do not remove history to satisfy the test. |
| **S2.11:** delayed required instruction loading is observed through `before_agent_start` before degradation behavior is changed | `sdk-context-file-refresh.test.ts` exercises actual SDK session prompt updates. `agent-session-before-agent-start-attribution.test.ts` covers hook message attribution and aborted next-turn queues, with a substituted runner. `system-prompt.ts` has a shared five-second preparation deadline; `sdk.ts` currently awaits `contextFilesPromise` directly before tool creation. Command **L** is adjacent coverage. | Delay the specifically suspected required loader, enter the real SDK → prompt builder → `before_agent_start` path, and capture omission, retry and warning behavior. SDK context loading and SYSTEM.md/custom prompt loading take different paths. A comment describing a fallback is not proof that contextFiles were dropped; an isolated prompt-builder result is not the requested end-to-end reproduction. |

## Executable command index

Run from the candidate repository root using the recorded candidate/qualification
Bun pin; record any override of `packageManager`. Native addons must be available.
PostgreSQL integration needs native
PostgreSQL 18. **I** requires an independently staged release and its actual
manifest digest; without them the installed tests skip and prove nothing.

```sh
# E — service plus substituted execution-host harness
bun run test:session:execute

# H — host/component guard and auditor tests
bun test session-system/tests/auditor-runner.test.ts session-system/tests/execution-halt.test.ts session-system/tests/checkpoint-delivery.test.ts

# P — durable operation-journal component coverage
bun test session-system/tests/pending-ops.test.ts

# W — complete supported PostgreSQL integration entrypoint
bun run test:py:work-ledger:integration

# G — local Git effects and GitHub simulator contracts
bun test session-system/tests/commit-step.test.ts session-system/tests/git-reliability.test.ts

# Y — real queue/session components, without process death
bun test packages/coding-agent/test/session/yield-queue.test.ts packages/coding-agent/test/agent-session-async-delivery.test.ts

# R — artifact admission and service readiness components
bun test session-system/tests/runtime-stage.test.ts
uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_service_readiness.py -q

# I — after setting OMP_INSTALLED_RELEASE and OMP_INSTALLED_MANIFEST_SHA256
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_installed_runtime_isolation.py -q

# L — prompt refresh/hook components, not the delayed-loader reproduction
bun test packages/coding-agent/test/sdk-context-file-refresh.test.ts packages/coding-agent/test/agent-session-before-agent-start-attribution.test.ts
```

## Reuse before adding a recovery fixture

OMP-246's description remains accurate for `host.ts::deliverExecutionMessage`:
it persists a pending outbox entry, calls `pi.sendMessage` with `nextTurn`, and
immediately persists `delivered`. The hidden next-turn queue remains in memory.
Its message identity contains grant and reservation versions, but not explicit
item/revision/session consumption bindings. This is a concrete candidate failure
path, not yet a reproduced whole-process failure.

Receipt-backed delivery already exists. `AgentSession.queueExtensionDelivery`
stamps the receiving session and returns a YieldQueue receipt after actual
injection. YieldQueue tests distinguish injection from completion of the turn.
`workflow/checkpoint-delivery.ts` already records successful and failed delivery
without blocking a running tool handler. Reuse those primitives. Awaiting an
injection receipt inside the handler whose return permits injection can deadlock
(the existing OMP-97 coverage models that boundary). Injection also does not by
itself establish durable execution consumption or exactly-once external effects.

The first new whole-process fixture should reuse these existing seams:

1. Extract the existing `InstalledRelease`, process lifecycle/RPC helper, health
   polling and disposable service setup from
   `python/omp-work/tests/test_installed_runtime_isolation.py` into a shared
   test-support module when the first recovery case needs them. Reuse
   `pg_native.py::native_postgres` and its disposable authority bootstrap.
   Keep artifact verification in the admitted launcher.
2. Run the installed CLI in RPC mode with a real persisted session, replacing
   the isolation test's `--no-session`. Resume the same session file and runtime
   state after SIGKILL. Retain the actual AgentSession, SessionManager, workflow
   host, operation journal, scheduler and task subprocess implementation.
3. Place a loopback HTTP response barrier between the configured WorkClient and
   the real disposable WorkService. Forward the actual command, observe its
   committed response and operation ID, withhold response bytes, then kill the
   controller. This is the smallest reusable crash boundary: it needs no fake
   service success, synthetic journal, or fabricated production grant.
4. Restart with the same durable state, release/replay transport, and inspect
   both service state and the session journal. Require one transition and the
   original operation result; then distinguish queued, injected and consumed
   continuation. Add a second restart to prove no extra reservation or effect.
   This first boundary is only one stage of S2.2 and the core S2.3 scenario;
   it does not complete every recovery checkpoint.
5. Where model output is needed to reach a stage, use a bounded local provider
   protocol server as transport input, with request/output capture. It must not
   replace session or worker machinery, directly invoke host actions in place
   of real dispatch, or manufacture auditor acceptance. Keep provider-readiness
   and paid canary claims separate. A test that never starts the actual task
   subprocess cannot claim worker-process recovery.

Additional fault points belong to their later checkpoints: queue/injection
barriers for cancellation, a local Git receive boundary for push response loss,
and real worker termination. Admission's GitHub protection lookup may need the
existing local `gh` fixture for disposable tests; label it simulated metadata
and never present it as real GitHub effect evidence. Prefer observable network
or process barriers; if a scheduler barrier is necessary, delegate to the real
method and pause at its boundary rather than replacing its behavior.

For the specific S2.2 outbox reproduction, use the real
`work action:"begin_execution_review"` pending-checkpoint path. Its
`yieldForDeliveries` calls the production `deliverExecutionMessage` before
returning a tool result. Hold the local provider's next tool-continuation
response while that turn remains active. Before killing, require evidence in
the real session journal that the outbox has been marked delivered but the
hidden execution prompt has not been injected. Restart the same session without
an owner repair prompt and observe whether the authorized continuation returns
once. If that barrier does not actually isolate the queue/injection gap, refine
it before claiming reproduction. Reach the pre-audit checkpoint through normal
host/service dispatch; a scripted audit PASS is unnecessary. This first case
proves a controller boundary only, not worker termination or all S2.2 stages.

Each executed scenario must retain command/cwd, admitted release digest, process
IDs, session/grant/item/revision identities, barrier observations, signal and
exit status, service operation/receipt identities, preserved output, and effect
counts. Assertions read runtime results or files the system wrote, never source
text. A missing prerequisite is an explicit non-pass, not a qualified skip.
