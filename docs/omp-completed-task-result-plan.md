# Completed bound-task result recovery: minimum implementation

2026-09-08. Static design for admission; no implementation or acceptance claim.
R2 independently confirms the same cut with both refusal reasons observed; its positive recovery assertion remains failed.
Baseline evidence: `/home/thetu/.local/state/omp-stabilization/omp246-recovery-20260908/task-result-gap-baseline-r1` (36 indexed raw artifacts). Actual original child read/yield/output and completed lifecycle existed; original parent result/event/provider did not; real successful authority response was held; shared process died. Restart refused completed child history. R1 remains a failed positive-recovery assertion; R2 improves observation of real refusal variants only.

## Decision and precise limits

Add **one bounded native-result checkpoint and one processing-start claim**, using the existing parent SessionManager journal. For this first remedy, certify only the existing recovered synchronous task path used by R1; ordinary first-run task completion does not gain a certificate or inferred eligibility. Reuse the existing durable original parent ToolResult as the terminal receipt. Recover only a newly certified native result whose parent processing is provably unstarted. Never rerun the child, reconstruct a successful result from Markdown alone, or replay partially executed result hooks.

Keep scope: same single synchronous flat bundled task, no isolation, exact original call/child/prompt/contract, existing authority and ownership rules. Existing PR23 before-answer and already-durable-parent-result paths remain. Async/batch/other agents, legacy completed histories, missing/corrupt completion records, conflicting/orphaned ownership and partial processing remain explicit refusals. No generic hook transaction framework, database/schema/RPC changes or live effects.

## Why existing evidence is insufficient for automatic replay

`finalizeRunResult` writes `<id>.md` before its final timeout/abort classification. Successful `yield` can coexist with later timeout, aborted cleanup or schema/result failure. A captured `subagent_lifecycle: completed` is useful test evidence, but it is not currently durable completion provenance available to a restarted runtime. Parsing the output file or finding a successful yield cannot recreate every field/error decision of the actual `SingleResult`.

Moreover, missing parent ToolResult does not show that parent processing never ran. Current SDK path is:

`native.recoverPersistedCall` → `processToolResultOutput` (may write spill artifacts) → `ExtensionToolWrapper.processResult` (`tool_result` handlers) → AgentSession external result events → `message_end` handlers → final guarded journal append.

A crash can occur after any handler's effect but before the last append. Legacy absence of a processing marker proves nothing. No new implementation may label R1's old record “unprocessed” retroactively, even though the external reproduction isolated an earlier state.

## New local record types

Extend `task/recovery.ts`, reusing current call refs, hashes, parsers and `TaskToolDetails`:

1. `NativeTaskResultReadyV1`, stored as parent custom entry `task-native-result-ready`:
   - protocol/version, producer `recovered-sync-task-v1`, and original `PersistedTaskResultRef` / full call binding reference;
   - unique completion identity (the new entry id is sufficient when referred to later), contract SHA256;
   - exact child session/init/prompt identity, completed child branch/leaf identity and successful yield result entry id;
   - exact actual native `SingleResult` and projection inputs **or**, preferably, the raw `AgentToolResult<TaskToolDetails>` produced once by existing `TaskTool.#buildResultPayload`; the latter already contains the actual SingleResult under `details.results[0]`;
   - actual output artifact path/size/SHA256 and payload hash; validate confinement with existing child/artifact path helpers;
   - explicit processing protocol discriminator: this record was produced by a path that MUST persist the following claim before any parent result processing.
2. `TaskResultProcessingStartedV1`, parent custom entry `task-result-processing-started`: exact completion entry id/hash, binding/call ref, and version. This is a conservative claim, not successful processing or consumption.
3. Extend new-protocol parent ToolResult's **core-owned entry-level** `taskResult` ref with completion/processing entry references. Retain old PR23 result refs on their existing already-durable path. Do not put trusted provenance only in replaceable extension `details`.

The ready payload must be an immutable serialized snapshot, independent of the object handed to existing processors. Reuse central serialization/clone helpers; validate JSON-safe native payloads and process a fresh copy. Nested result-hook mutation must not change the ready journal object or its hash. Do not freeze processor input in a way that changes ordinary hook behavior. Add a regression for this ownership boundary.

No mutable status file, queue or per-hook receipt. Read records from original parent journal. Completion ready must be on the authorized active branch. Search processing claims for that exact completion/binding across **all retained journal entries**, not only active branch: branching back before a started marker cannot make already-run hooks appear unstarted. Reject competing completions, claims or discarded/ambiguous history. Existing branch/reset/compaction refusals remain.

A ready record positively identifies the new writer/processing protocol. Only under that protocol can absence of its mandatory start claim prove the pipeline has not been entered. Legacy records lacking this ready provenance remain unsupported; do not infer their phase from missing markers or version guessed from the current binary.

## Producer ordering and seams

### A. Certify actual native completion before the observed authority gap

Use the shared native finalizer in `task/executor.ts`; do not reimplement yield parsing, schema enforcement, truncation or outcome classification.

- Materialize its final `SingleResult` as a local variable after all existing success/abort/error decisions and output write outcome are known.
- Add an optional **bound-task-only** `onNativeResult` callback in `FinalizeRunArgs`, threaded from `runPersistedTask` through the shared existing-session executor. Invoke it before publishing the completed lifecycle and returning the result. Original `runSubprocess` and unbound/unsupported calls leave it unset; they do not enter this new completion protocol.
- The callback supplied by TaskTool uses existing `#buildResultPayload` once, freezes the actual projection duration/inputs, and records `NativeTaskResultReadyV1`. The uninterrupted recovered-task return and later cached recovery use that same raw payload; do not reconstruct duration, usage, truncation or error flags from the output file.
- Only certify genuine successful finalization: original task id, non-aborted exit 0, no terminal error, real successful yield, actual validated output/schema, no deferred/unknown completion condition. Output write failure or incomplete child persistence leaves no recoverable ready checkpoint; never treat a warning and absent artifact as proof.
- Ensure child result persistence is actually settled, then flush and bind the observed child branch. Plain `waitForIdle()` or a file's existence is not a general message-end persistence barrier. Reuse AgentSession's targeted persistence tail/idle-event drain at a **settled child** boundary; never wait on the parent handler from inside itself. Do not re-open or steal a live child's SessionManager writer. Keep the prepared child journal reference available through this existing-session path rather than assume `monitor.takeActiveSession()` is still available at finalization. Do not extend certification to normal executor cleanup paths until their distinct persistence/cleanup order has separate proof.
- Write and flush parent ready checkpoint before returning native result or reaching the subsequent WorkService `validateAuthority` await. Recording actual past completion is local provenance; it must not require a new service mutation or fake grant transition. Still require original local session/branch/call ownership for the append. If ownership is lost, preserve child artifacts and refuse certification/attachment to a different branch.

This ordering puts a durable ready record before the admitted proxy can stop the successful completion path. The old external lifecycle frame remains observation; restart now has its own native completion proof. Cached recovery must not rerun finalizer or re-emit child lifecycle/progress events.

### B. Claim once before every parent-processing entry

Introduce one core handoff helper at the existing SDK `recoverSynchronousTask` processing boundary, used for both newly finished recovered children and cached completed-result recovery. Native task code returns its ready reference alongside the raw result; keep provenance separate from replaceable details. Before `processToolResultOutput`:

1. Validate current policy/authority and original ownership as applicable to that path.
2. Require exact ready record, no existing processing claim anywhere in retained parent journal, and no original parent result/conflicting owner turn.
3. Append processing-start claim; flush; recheck ownership/authority. No `processToolResultOutput`, spill write, result hook or result event may happen before this succeeds.
4. Pass the cached raw payload through the **existing** SDK output/result pipeline once. If claim/authority fails, throw out of this SDK callback so parent recovery refuses before processors or result events; do not convert the error into an ordinary result-hook invocation. Every certified producer must use this same entry.

Ordinary TaskTool catch/error returns and `ExtensionToolWrapper.execute` may still invoke `tool_result` hooks after a native exception. They are not safe certified producer paths merely because a success branch writes a marker. Extending this protocol to initial `runSubprocess` completion would additionally require a fail-closed processing-entry guard covering both success and error paths before spill/result hooks, with its own tests; that is excluded from this minimum R1 remedy.

Pure `#buildResultPayload` belongs before this boundary so the ready checkpoint has the exact native result; output spill and all extension result/message hooks belong after it. This is one narrow pipeline-entry claim, not per-hook transactions. If claim exists but final parent result is absent, refuse—even if the process may have died before the first hook actually ran. That conservative false negative is intentional.

Important current source constraint: parent recovery's `currentRequest.expectedLeafId` is pinned. Ready/claim appends advance it. Have the core journal helper validate old expected leaf, append its own exact record synchronously, and advance the scope's expected leaf to that returned id **before** awaiting flush. Recheck afterward. Never refresh expected leaf indiscriminately after an await or ignore all metadata changes; that would adopt an owner/foreign branch mutation.

### C. Select cached completion without child execution

In `TaskTool.recoverPersistedCall`, after current binding/contract/path/policy validation and before its no-child-history classifier:

- If no ready record, preserve PR23's original before-answer eligibility; legacy completed/unknown child history still refuses.
- If matching ready exists, validate the actual original child journal/init/reverse join/preparation and the certified completion/output identity. This is reading an already finalized result, not executing or normalizing old tool calls.
- Require unchanged completed child branch, no later child conversation/reset/effect, no tombstone or competing live child, and live original grant/session/revision/policy. Use existing registry/path/SessionManager ownership checks; no `ensureLive`, child AgentSession creation, monitor, model call, read tool, yield or id allocation on this cached path.
- If processing-start exists without an already durable valid parent result, return precise `task-result-processing-incomplete` refusal. Do not process cached raw result again.
- Otherwise pass ready raw result through the single claim helper and existing SDK output/result pipeline once.

Carry completion identity independently of raw result `details` through `RecoveredTaskResult` / `ParentTaskRecoveryScope`. At final parent append, stamp the core result ref only **after** ordinary message-end hooks and current final policy/authority checks, as PR23 already does. Validate ready/claim identities again there. No transcript JSON supplied by the model or test may create this ref.

### D. Preserve existing durable-result continuation

Existing AgentSession result slot wait + SessionManager flush + exact original ToolResult lookup remains the durability barrier before parent provider dispatch. Preserve its targeted canonical assistant restoration, queue/branch/generation checks and original call/result pairing.

If original result is durable, reuse current PR23 result path: no child dispatch, finalizer, output processing or result-hook replay. New ready/claim records do not override a genuine terminal/owner-interrupted outcome. If processing or final append fails, leave claim and artifacts and refuse automatic processing on next startup. Never delete the claim to retry uncertain hook effects.

## State table

| Durable state | Permitted action |
| --- | --- |
| Original no-answer child; no completion ready | Existing PR23 guarded child resume, unchanged. |
| New native ready; no processing claim; same authority/branch/child | Claim, process exact cached native result once, persist original parent result, continue parent. |
| Processing-start claim; parent result absent | Explicit refusal; hooks/output may already have run. |
| Valid original parent result durable | Existing parent-only continuation; no child/processors. |
| Legacy completed child with yield/output only | Refuse; never retrofit “unprocessed”. |
| Conflicting record, terminal/changed authority, tombstone or later child history | Refuse and preserve evidence. |

## Verification

1. Preserve R1/R2 negative artifacts unchanged. Candidate positive journey starts with new runtime-written records. Same admitted proxy cut must now observe exact ready checkpoint and **no** processing claim before kill, plus actual child read/yield/output/lifecycle and missing parent result. Restart yields one original parent result and actual parent provider continuation, with zero new child requests/read/yield/id allocation. Original native payload matches ready checkpoint, apart from permitted real parent processing. Grant/files/HEAD/remote refs unchanged. Another restart duplicates nothing.
2. Preserve all PR23 nine installed journeys. Initial no-answer and repeated-child kill still use same child; already-durable parent-result cut never enters new processors.
3. Component tests run actual native finalizer/projection and real hook pipeline. Durable yield with final timeout/abort/schema failure must not acquire a successful ready checkpoint; missing output/failed completion persistence cannot become cached success.
4. Prove processing-start is durable before output writer/result hook runs. Have a real test hook perform one observable fixture effect and then fail/hold; reopen original journal and require incomplete-processing refusal and effect count still one. Label component evidence honestly; it is not an installed arbitrary-hook transaction qualification.
5. Branch through supported SessionManager API back before a genuine processing-start entry; recovery still finds that claim in retained history and refuses. No journal text edits.
6. Test wrong completion/binding/child/output hash, duplicate records, old completed histories, tombstones, changed policy/authority and owner input during each awaited flush/check. No result append/provider call after revocation; preserve queues and foreign branch. Core completion/result provenance survives result hooks replacing `details`.
7. Persistence failures at ready or claim block the next effect; no ready record alone is called consumed. A started claim is never erased by automatic recovery. Successful final parent result remains the only proof that this result delivery completed.

Expected production paths: `task/recovery.ts`, `task/index.ts`, `task/executor.ts`, `session/agent-session.ts`, `session/agent-session-types.ts`, `sdk.ts`, plus `task/types.ts` or session entry typing only if needed; focused tests, installed observer assertions, docs/changelog/CI evidence list. Existing `processToolResultOutput` and `ExtensionToolWrapper.processResult` remain single implementations. No new generic wrapper/transaction adapter.

Run focused task/session tests and `bun check`, independently review producer ordering and both newly-completed/cached processing branches, stage fresh artifact, then installed ten-case corpus and exact-head CI. Report actual skips/failures separately. This completes only certified native-ready recovery of a previously resumed task; first-run completion without that certificate remains refused, and arbitrary child effects, partial hooks and S2.3 committed service-response loss remain open.

## Residual implementation choices to settle before coding each seam

- Prefer one ready payload containing the already projected raw TaskTool result, rather than duplicate result builders. Capture original projection time once; finalizer callback must run before completed lifecycle/next authority await.
- Identify the existing settled-child persistence await for the retained existing-session path; add only a narrow internal accessor if necessary. Do not claim SessionManager.flush waits for not-yet-appended message-end handlers.
- Ensure new self-authored journal entries advance only their precise parent scope lease, and that processing claims remain visible across retained sibling branches.
- If an eligible producer path cannot guarantee the mandatory processing claim, exclude that path from the new protocol explicitly. Never use absence of markers in an unqualified path as safety evidence.

Implementation awaits root admission of this plan. No native PASS, service write, old journal upgrade, issue completion or live activation is proposed here.

## Independent review constraints

1. Exact self-append lease advancement also applies to the final original parent
   ToolResult. Advance only to its returned entry ID inside the guarded append,
   before awaiting its flush; recheck that exact lease afterward. Never adopt an
   unrelated current leaf after an await.
2. Classify retained ready/start records before the no-ready fallback. Off-branch
   ready records, orphaned claims and ambiguous history refuse. Supported
   `discardEntryDurably` can physically remove a leaf claim and leave a discard
   marker; branch rewind before that marker must not recreate an unstarted state.
   Treat retained discard evidence conservatively where ownership cannot be
   established. Test supported discard and branch operations without editing JSON.
3. Before resuming a no-answer child, inspect retained history of that same bound
   child for assistant/tool/effect activity and discarded execution evidence.
   Active-branch projection alone must not hide executed work after rewind.
   Preserve the existing unambiguous original no-answer path.
4. Cached-ready processing needs an ownership witness even though it opens no
   child session: exact expected parked registry reference and certified child
   path/leaf. Retain and recheck that witness through authority waits, processing
   and result append. Reuse active parent scope and pre-attach gate to refuse or
   revoke competing revival; no generic registry/transaction framework.
