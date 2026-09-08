# Bounded synchronous task recovery implementation plan

2026-09-08. Design only. Baseline `09858a8b67`; reproduction R3 is `/home/thetu/.local/state/omp-stabilization/task-active-baseline-r3/test_killed_controller_recover0/task-recovery-summary.json`. It records one genuine `task-active-call`, child `RecoveryTask`, zero child response bytes, no parent result, unchanged active grant/effects, and exact `pending-tools` startup refusal. Child is a normal `user` message attributed to `agent`, followed by unbound `work-digest`. Parent header/child init do not supply a durable call join. Never backfill that join from the test's RPC capture.

## Scope and implementation decision

Add one native task recovery branch to the existing persisted-turn continuation seam. Accept only a newly bound, single pending, synchronous, flat, bundled `task` call with no isolation and a child that has not produced an assistant response or executed a tool. Resume the original child's prompt through current AgentSession preparation; obtain a real result through existing task monitor/yield/finalizer; attach that result to the original parent call; persist it before continuing the parent.

No WorkService contract/schema/migration change, no new RPC command, no generic tool recovery registry, no subprocess redesign. The same process hosts controller and child. Existing service authority callback remains authoritative. This is additive local journal metadata, requiring fresh TCB/release qualification. R3's historical unbound child remains refused after the fix; the positive case must begin afresh under the candidate so production writes the binding.

Unsupported: async, batch even with one item, multiple pending calls, other agent kinds, isolated children, absent/mismatched bindings, child assistant/tool/effect history, old unbound preparation, completed child with no persisted parent result, and changed authority/contract. Preserve precise refusals. In particular, do not turn every `pending-tools` response into an automatic retry.

## Narrow data contract

Put task-specific data/parser/hash helpers in `packages/coding-agent/src/task/recovery.ts`, not workflow host. Reuse `stableStringifyJson` from pi-utils and `Bun.CryptoHasher("sha256")`; domain-separate the contract hash. No WorkService imports needed.

Proposed types (names can be adjusted, meaning cannot):

- `PersistedTaskCallRef`: `bindingId`, parent `sessionId`, original `promptEntryId`, **assistant entry id**, original `toolCallId`, canonical effective flat argument hash. Entry ids and ancestry disambiguate branch orphans; names/timestamps do not.
- `TaskRecoveryContract`: bundled agent/source identity; raw assignment and rendered initial prompt; effective `SessionInitEntry` policy fields (base system prompt, tools, output schema/mode, spawns, readSummarize/advisor/restrictToolNames); exact provider/API/model identity, resolved thinking/tier, task depth, and current executable tool names/schema identity. Include effective synchronous/flat/non-isolated mode and relevant approval/spawn policy. Preserve actual values and hash canonical projection, not guessed defaults.
- `PersistedTaskBindingV1`: `{ version: 1, call: PersistedTaskCallRef, mode: "sync-flat", child: { registryId, sessionId, sessionFile, initEntryId, cwd }, contract, contractSha256 }`. Child path must resolve within original parent's artifact root; compare real workspace/session identity. Hash is a consistency binding, not a substitute for current TCB/authority.
- Parent writes a normal custom entry, e.g. `task-run-binding`, containing that binding. Add optional `taskCall` to `SessionInitEntry` and `appendSessionInit` input, containing the same call ref. This is the reverse join; child init id and contract hash in parent prevent pointing at an unrelated transcript.
- `PromptPreparationRecordV1`: core-written custom entry with `sessionId`, stable original `anchorEntryId`, preparation batch id, exact preparation entry ids, and optional task binding id. Initial record identifies original agent-authored input; fresh recovery records refer to the **same** original input. Membership is based on actual core assembly/persistence, not message content/customType.
- Add optional core task-binding reference to actual `TaskToolDetails`, or to its parent result's session-entry metadata, so a persisted original `ToolResult` can be checked against the binding and recognized without reopening/rerunning the child. Do not add a `consumed` boolean.

Keep a single parent binding record per call; a conflicting second binding is refusal. Child initialization and parent binding are not one cross-file transaction: write child first, parent second, flush both before dispatch. A crash with only child half leaves an orphan, which remains refused. No synthetic repair of that window in this slice.

## Normal execution: write the binding before dispatch

1. `TaskTool` checks explicit original execution mode (`async.enabled === false`, `task.batch === false`, flat `agent: task`, bundled definition, no isolation). Do not alter unsupported modes' normal execution. They simply get no automatic recovery eligibility.
2. After actual tool approval and effective policy resolution, capture the parent call through a narrow SDK-backed `ToolSession.captureTaskCall(...)` function. AgentSession owns lookup: await only the original assistant/input persistence slots, then require actual active-branch assistant with exactly this call and matching effective args. Resolve originating prompt from its core prompt association, not by searching for a similarly named task or latest custom string. Flush parent and recheck session/generation/ancestry. If eligible binding cannot be proved, do not manufacture an assistant entry. Do not drain all in-flight event handlers from inside a tool; that can wait on the current tool itself.
3. Normal `runStructuredSubagent` allocates the id once, as today. Pass captured call ref and a typed `onChildPrepared` callback through `ExecutorOptions`.
4. In `runSubprocess`, use the existing `appendSessionInit` return id after actual child creation and tool clamp. Include reverse call ref in that init. Capture actual contract/model/workspace, flush child, then invoke callback to append/flush parent `task-run-binding`.
5. This happens **before child extension `session_start` and initial `driveSessionToYield`**. Existing executor writes init before its explicit extension initialization at roughly 3290–3310; place binding there, not in `onFirstChatDispatch`, whose callback is observational and catches exceptions.
6. Only after successful binding, child starts normal `session.prompt(task, { attribution: "agent" })`. Pass its validated binding ref as an internal prompt option/scope, so normal prompt association can record the child input. Binding failure must surface and prevent that child dispatch. Parent/child files left by failed setup are evidence, never a launch-success receipt.

Useful source constraints: parent tool start can persist before assistant entry (R3 did this); `#waitForSessionMessagePersistence` alone is not proof if no persistence slot existed, so follow it with actual branch lookup/flush. `appendSessionInit` already returns a string id. Capture effective argument normalization after extension revisions/approval, not raw streamed `partialArgs`.

## Core-owned prompt/preparation association

Implement in `session/agent-session.ts`, with record type in `session/session-entries.ts` or small session type module. This must cover initial agent-authored `user` prompts and existing-entry recovery preparation.

- In `#promptWithMessage`, distinguish the primary input from core-prepared context: plan/goal/mode/file-mention context and messages returned by the awaited `before_agent_start` for this exact prompt. Associate only those objects through a private WeakMap/batch object. Never trust an extension-supplied `details.preparation` field.
- Do **not** classify arbitrary queued nextTurn/steer/follow-up messages as harmless preparation. They may be new assignments or owner input. Unassociated conversation remains refusal for this bounded recovery.
- Have normal `#persistMessageEnd` / `#appendSessionMessage` return or report actual persisted entry ids to that private batch. When every expected member has really appended, append core `PromptPreparationRecordV1` with the exact ids. Preserve current persistence ordering and error handling. Missing/partial association remains refusal; it is not delivery success.
- Initial record anchors the actual role `user`, attribution `agent` entry. For later `existingEntry` preparation, anchor is the original entry, and only newly prepared entries go into the new batch. Repeated crash with a complete fresh preparation record remains provably the same task prompt.
- Classifier admits `user/agent` only when an exact validated task binding plus core preparation record names it. It admits trailing preparation only by record membership, active ancestry and actual content/entry consistency. Ordinary owner `user` messages, unbound agent custom messages, duplicate anchors, foreign records, resets/compaction, or any assistant/tool history refuse. Do not special-case `work-digest`.
- Map anchor to restored runtime context using existing message/persistence helpers, preserving deobfuscation behavior. This replaces current assumption that anchor must be the literal final custom message.

Existing `Agent.addBeforeModelCallHook` and `beforeModelCall` run **before input message events** (`agent-loop.ts:1102–1186`); waiting there for those events' persistence is not a valid barrier. This proposal records association from the normal persistence path instead. The pre-response fixture must observe the complete record before SIGKILL. A crash in an incomplete metadata window explicitly refuses in this slice. Do not eagerly double-append prompt messages to force a receipt.

## Resume original child, using existing task machinery

Extend existing request type with a narrowly named opt-in such as `recoverSynchronousTask?: true`; default remains no-tool behavior. Workflow host enables it only on its already validated current execution continuation. Keep the same `validateDispatch` callback, coalescing slot, startup gate and refusal channel. No parallel extension command/action is necessary.

1. AgentSession classifies original parent branch: exactly one pending `task`, matching assistant entry/ref/binding, no later owner conversation or unrelated assistant/tool activity, active original execution anchor. A persisted matching parent result instead takes the completed-result path below. Multiple/unsupported calls refuse before invoking native code.
2. SDK supplies one typed native callback in `AgentSessionConfig`, e.g. `recoverSynchronousTask(request): Promise<RecoveredTaskResult>`. Obtain actual `TaskTool` instance from existing `nativeToolsByName`; metadata wrapper mutates it in place, so `instanceof TaskTool` is viable. Also require current `builtInRegistryToolNames`/active tools still identify native task, preventing an extension replacement from being bypassed. Do not cast `getToolByName`'s `ExtensionToolWrapper` into TaskTool, and do not call native `execute`, which allocates a new task.
3. `TaskTool.recoverPersistedCall` validates mode/args/binding, current explicit denial/spawn policy and effective contract, original file paths and child init reverse join. Read original child branch: only bound agent input plus core-associated preparation, no assistant/start/result/yield or incompatible resets. Old R3 child fails here, intentionally.
4. Reuse `ensurePersistedRoster`, expected `AgentRef`, `AgentLifecycleManager.ensureLive`, and `createPersistedSubagentReviverFactory`. Require exact original id/path/session; refuse tombstones, live competing sessions, missing contract/workspace, or path collision. Do not call `reserveStructuredSubagentId`/`AgentOutputManager.allocate` during recovery.
5. Install authorization/ownership guard **before revived extension initialization**, not only after `ensureLive` returns. Small task-specific seam: existing persisted reviver already receives parent `ctx.session`; before `initializeExtensions`, call a parent core method that checks its one active task-recovery scope against this ref/child, verifies the rebuilt model/tool contract, and installs child guard. Outside a matching recovery scope, current hub revival behavior stays unchanged. Avoid replacing the global factory per task. Parent scope must exist before `ensureLive`; recheck ref/session after it returns.
6. Use actual task monitor and finalization. Add a typed start mode to `driveSessionToYield` (`new prompt` versus `existing bound prompt`) so only its initial `session.prompt` call differs. Factor the scheduled persisted-prompt executor from `requestPersistedTurnContinuation` into an internally awaitable method; task driver awaits this exact operation, not “scheduled” followed by a racy idle check. Reuse `#promptWithMessage(existingEntry)` and current hook/model preparation. No new assignment or replacement input.
7. Reuse `createSubagentRunMonitor`, abort/watchdog handling, real yield ladder, `finalizeRunResult`, structured-output metadata, and `TaskTool.#buildResultPayload`. Add a compact `runPersistedTask` entry that obtains original session and invokes this shared monitored path; do not duplicate `runSubprocess` or `runSubagentFollowUpTurn`. The latter currently injects a follow-up and marks detached, so it is not correct unchanged.
8. Preserve original `parentToolCallId`, task id, agent source, assignment and index 0 in lifecycle/result events. Cold factory currently labels general wake monitors `source: user`; bound recovery's real task monitor must use recorded bundled source. Initial no-answer run has no assistant usage to salvage; do not fabricate charges or infer success from HTTP dispatch.

Guard lifetime covers child model calls **and tools**, parent result commit, and parent continuation. Compose with existing before-model hook and AgentSession `#beforeToolCall`/approval path; never overwrite existing gates. Parent abort/generation change must abort monitor/child even though parent Agent itself is not streaming. Hold parent in-flight ownership while task runs, so owner input queues normally and RPC does not report idle. Check live authority after each await at dispatch boundaries, and honor queued owner input, terminal grant, tombstone, changed branch/session, model/tool contract drift. Parent ownership counts need deliberate handling when calling its existing-entry preparation; do not globally relax busy checks.

## Commit the real original parent result before parent continuation

`TaskTool.#buildResultPayload` remains the one task result projection. Preserve normal output handling: reuse/factor existing success postprocessing in `tools/output-meta.ts` (spill/notice) and `ExtensionToolWrapper`'s result-hook merge into shared helpers if recovery calls would otherwise bypass them. Do not run the task body again just to access wrappers. Apply result hooks once for a newly completed result, not on disk-result reuse.

AgentSession owns final attachment:

1. Fresh authority/local/ref check; verify original call still pending and no same-call result exists on active branch. If an exact matching result already exists, use it; conflicting result refuses.
2. Build actual `ToolResultMessage` with original `toolCallId`, `toolName: task`, actual native task result/content/details/error status and binding reference. Never let the extension or fixture supply a result.
3. Use existing `Agent.emitExternalEvent` for actual `tool_execution_end`, `message_start`, `message_end`; it updates Agent state and enters normal Session persistence. This is a production result bridge for the real resumed invocation, not fabricated success. Do not emit another tool start: original start exists. Await targeted result persistence slot and `SessionManager.flush`, then require actual matching branch result.
4. Only then prepare/continue parent from this `toolResult`, under same grant callback and ownership guard. Preserve original assistant/tool pairing. Existing `Agent.continue` accepts tool-result tails; it rejects assistant tails, so never delete original assistant or use manual `retry()`.
5. On restart after result persistence, recognize that exact binding/result and continue parent without opening/launching child. A later terminal parent assistant suppresses wake normally. Crash after child completion but **before** parent result durability remains explicit unsupported child-history refusal in this slice. Do not quietly implement generic child yield/effect reconstruction to cover that separate window.

`navigateTree` ask re-answer around `agent-session.ts:9189` and `SessionManager.appendMessageToBranch` demonstrate exact call-id results and context rebuilding, but ask intentionally creates a sibling branch from an existing answer. Do not invoke ask APIs or branch the recovered task result onto another transcript. Reuse underlying persistence/context helpers if needed; keep active-branch ownership.

If authority/owner changes before attachment, preserve actual child transcript/output and refuse parent continuation. No successful parent result is manufactured to silence pending diagnostics. Persistence failure never proceeds to another provider request. Result events can precede journal persistence as today, so tests must inspect journal, not only RPC events.

## Required verification and allowed paths

Production paths: `task/recovery.ts`, `task/types.ts`, `task/index.ts`, `task/structured-subagent.ts`, `task/executor.ts`, `task/persisted-revive.ts`, `session/agent-session.ts`, `session/agent-session-types.ts`, `session/session-entries.ts`, `session/session-manager.ts` (optional init/type/id plumbing only), `tools/index.ts`, `sdk.ts`, `workflow/host.ts`; `tools/output-meta.ts` and extension wrapper only for shared result postprocessing if needed. No service/database files, no process-worker files, no prompts built in TypeScript. Keep new static fixture prompts in Markdown.

Tests must defend observable contracts:

- Candidate installed R3 journey writes binding itself before held child request; original parent/child ids and preparation record survive kill. Restart same session/grant resumes same child; scripted provider returns real `read` then real `yield`; actual task result has original call/id and actual read-derived content. No `RecoveryTask-2`, new parent assignment, reservation or Git effect. Parent sees that result, then completes its local turn. Second restart duplicates neither child nor parent result. No native audit action/PASS.
- Repeat SIGKILL during restarted child's held first request **after fresh core preparation association is durable**. Same binding/anchor survives; no arbitrary custom allowlist.
- Hold parent provider request after actual original task result is persisted; kill/restart. Resume from that result with zero child requests. This is the bounded parent-result persistence barrier, not reconstruction of child completion from missing parent result.
- Original R3 old unbound journals still refuse; mismatched assistant entry, foreign branch binding, same name/wrong child session, altered assignment/contract, missing core association, unbound digest, owner-attributed user, tombstone, terminal authority, async/batch/isolation/multiple calls and child assistant/tool history each defend distinct refusal boundaries.
- Component race tests: startup/child revival await with owner abort or cancellation; changed role/tool/approval policy; result persistence failure; duplicate recovery requests coalesce; exact prior persisted result reused. Real AgentSession/SessionManager with spies/provider seam, no global `mock.module`, no source-grep tests.
- Run existing six installed cases plus new cases, focused task/session/host tests, `bun check`, fresh staged artifact and exact-head CI. Preserve source/release identity, process-group evidence, real journals/operation identities and effect snapshots.

## Implementation cautions to settle locally

1. Confirm helper returning actual entry ids handles custom messages and obfuscation without creating second entries. Preparation association must use written ids, not JSON equality alone.
2. Preserve result-hook and output-meta processing once. Small helper extraction is appropriate; copied TaskResult/extension pipeline is not.
3. Current persisted reviver expands role before concrete-model fallback and clamps away missing tools. Bound recovery must reject a mismatched effective model/toolset before startup can dispatch; ordinary hub behavior need not change.
4. A raw provider request cannot prove no server-side effect. This slice's admissible child branch and local protocol proof establish no local child response/tool history. Broader provider-native effects, async jobs and partial completed child work remain separate.

Expected implementation scale: several short typed seams and shared-path extraction, not a new controller. If an implementation starts recreating task scheduling, tool execution, SDK sessions or result projection, stop and reuse the existing owners named above.

## Independent review amendments

The independent Astra xhigh review requires these constraints before qualification:

1. Acquire exclusive child execution scope before `ensureLive`. Initially deny all
   child model/tool dispatch, including revived extension startup; enable only the
   exact bound task driver after validation. Revival coalescing is not execution
   ownership. Competing hub, IRC, prompt, steer or follow-up input must refuse or
   revoke recovery, never silently enter the original task turn.
2. Core preparation registration covers both the parent agent-attributed custom
   execution prompt and the child agent-attributed user prompt. Parent capture
   must use this provenance; child eligibility still requires its exact validated
   task binding. This does not admit arbitrary custom messages as child input.
3. Compose a final tool dispatch guard after all tool-call revisions, awaited
   approval and resolved hooks, immediately before actual `tool.execute`.
   `#beforeToolCall` alone runs too early. The extension wrapper is an allowed
   change for this narrow guard as well as shared result postprocessing.
4. If new cold-revival validation fails after opening a writer or attaching a
   session, dispose only that newly created session and detach only its expected
   reference. Preserve transcripts and parent binding. Do not invent a tombstone
   for ordinary recovery refusal or rely on lifecycle cleanup for a session the
   factory never returned.
5. Stamp the parent result's core binding reference after output metadata and
   result-hook merging, preferably in session-entry metadata. Hook replacement
   of result details must not erase or spoof provenance used on the next restart.

Add focused observable race/refusal coverage for these boundaries alongside the
installed journeys above. These amendments do not expand supported task modes
or repair the explicitly unsupported child-completed/result-missing window.

CI already runs the complete installed recovery module. Extend its existing
`.github/workflows/ci.yml` evidence upload to retain task recovery reports,
parent/child journal snapshots, task-controller RPC/stderr and local provider
observations/requests, including failed runs. These paths are additional allowed
qualification changes. A green job without the required process and identity
evidence does not complete this slice.
