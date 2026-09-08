# S2.2 original first-run task result certification

## Admitted failure and base

Implement the ordinary first-run completion gap for OMP-246 revision
`f5bb9640-3491-50b7-bdad-2a77f9c102e3`. Base is reproduction commit
`d18d8bdc9b3461fd75949a37143881d4dbf7e73c` on
`codex/omp-first-run-task-recovery` in `/tmp/omp-recovery-stabilization`.
The [reproduction plan](omp-first-run-task-result-gap-plan.md) and its R1 evidence
remain historical, unchanged records.

R1 ran qualified runtime `af2aae1efa356ea6957a70fa9236fbb16c40d38e`, manifest
`e6ced038502814e8320c6f636b03f84da26857a61deeee00285f0e0a24c99882`, with
the separately reviewed changed harness based on PR #24 merge `0dab30c9046`.
It reached the actual first-run completed-child/missing-parent-result cut on
attempt one, with no initial crash or missed cut. One real SIGKILL and two
same-session restarts produced explicit refusals, zero provider requests or
original parent results, and unchanged service/Git effects. Pytest failed the
positive recovery assertion: one failure, zero errors/skips, 35.215 seconds.
The prior ten installed cases passed separately with this harness in 307.79 seconds.

Supported handoff `c76c1969-358f-55fd-9c93-5f64ea6494eb` records R1. Evidence
root is `/home/thetu/.local/state/omp-stabilization/omp246-recovery-20260908/`.
The immutable baseline receipt is `first-run-gap-baseline-r1/evidence.json`,
SHA256 `8e77a82c120d3b06dcbc4117c0095e549111109138b8ffc98bd637137b755f28`.
Its separate process-log supplement establishes original and resumed CLI PIDs;
restart launcher PGID/start ticks were not captured and must not be invented.

## Scope

Extend the existing native ready/processing-start/result protocol only to an
eligible original first-run single synchronous, flat, bound task with matching
execution authority. Reuse original call, child, finalizer and result pipeline.
Do not schedule a replacement parent turn or introduce another task/job registry.
No callback/qualification means existing ordinary task behavior remains unchanged.
Legacy uncertified completion and started-without-result remain refused.

No production delay, fault switch, service/schema change, generic hook transaction
system, live installation change or native acceptance is in scope.

## Exact ordinary-call authority and ownership

Ordinary dispatch currently lacks the persisted-turn request's authority
validator. Add a narrowly typed, optional runtime-only task-result authority
validator to the existing extension `tool_call` result. The workflow host supplies
a closure reusing its existing execution-authority validation for the matching
execution prompt. AgentSession retains it only for the actual task call, session,
generation and prompt origin. Never serialize the callback or accept one from
model/RPC data. Multiple applicable validators must not silently override or
weaken one another; compose their requirements or reject ambiguous ownership.

Activate completion ownership only inside actual approved native execution via
the existing bound-call capture path. Capture alone proves local call provenance,
not WorkService authority. Missing callback keeps the task unqualified; a present
validator's failure must never become implicit permission or fallback success.
Denied approval cannot leave an active completion owner.

The real native regression exposed an additional core boundary: AgentLoop creates
assistant snapshots between authorization and native dispatch. An object-only
WeakMap loses valid authority; an enumerable private symbol can be copied to an
unrelated object and does not authenticate core snapshot lineage. Add only the
necessary core-owned provenance seam in `packages/agent/src/agent-loop.ts` (and
its existing context type if required). Register lineage privately where actual
snapshots are created, with read-only identity resolution for the caller; never
expose public registration or infer lineage from message IDs, content or copied
properties. Canonicalize lineage without unbounded chains. A genuine core snapshot
must resolve to the exact authorized invocation, while a copied or stale carrier
must refuse an already-authorized invocation rather than silently downgrade it to
ordinary execution. This is snapshot provenance plumbing, not an AgentLoop redesign.

Extract only the narrow ready/claim/result-commit owner contract shared with
recovery: exact call binding, expected parent leaf, child identity, processing
reference, local ownership assertion and fresh authority validator. Ordinary
ownership uses original parent-loop signal and generation. Extend existing
bound-call capture/storage rather than adding a second coordinator or registry.

## Original child and native readiness

Reuse the exact driver/pre-attach ownership guard for the qualified original child.
Run its original initial prompt through the existing driver. Drain completion-
critical handlers before output publication and retain the original settled-child
lease through all finalization awaits. Lifecycle/registry adoption must not expose
an unguarded child before certification. Keep existing cleanup and fail closed on
abort, timeout, ownership drift, unresolved persistence/cleanup or child changes.

Thread the existing native-result callback through eligible original
`runSubprocess` execution. Use the actual finalizer and TaskTool projection to
write immutable serialized raw readiness before the completed lifecycle is emitted.
Use explicit producer `original-sync-task-v1`; preserve compatibility with
`recovered-sync-task-v1`. No retrospective reconstruction from output files, yield
history, caller-supplied payloads or legacy marker absence.

Advance the parent lease only to the exact self-appended entry, synchronously
before awaiting persistence. Reuse the immutable raw payload, child/output hashes,
retained-history/discard checks and no-revival witness. Never refresh the lease
from an arbitrary later leaf or replace the original settled-child assertion.

## Claim and parent delivery

Resolve one task-specific processing gate from core-owned call context before
metadata output/spill handling, extension result processing (including caught
native errors), owned ToolResult message-end hooks and final persistence.
Durably acquire the existing processing-start claim once, then recheck ownership
and fresh authority at every required delivery boundary. Later stages recognize
the same live claim; they do not create another claim or repeat processing.

Ready/claim failure must not fall through ordinary catch handling into result
hooks or provider advancement. Distinguish a task that never entered certification
from a failed certification attempt: the former retains ordinary error behavior;
the latter cannot downgrade to unguarded processing, including after a partial or
failed ready write. Keep ownership registered through result durability or explicit
failure cleanup. Hook execution cannot be treated as safely retryable after a crash.

Stamp core completion/claim references at the exact guarded final append after
hooks, independently of replaceable result details. The original parent loop must
wait for actual result persistence before its next provider request. On restart,
first-run readiness with no retained processing claim uses existing cached-result
delivery: no child revival, provider request, tool execution or new allocation.
Started-without-result still refuses; a durable original parent result remains the
terminal delivery receipt. Preserve original call/result wire pairing and queues.

## Verification

Keep R1 raw artifacts and reviewed harness identities immutable. Update the new
positive installed observer to require actual runtime first-run ready provenance
and no processing-start claim at the cut. A claim-present, late or partial cut
remains a miss; retain at most three fresh attempts. Do not weaken the cut to make
the new test pass or relabel the old failed runtime. Existing authority GET and
first-run completion observers remain distinct, accurately named boundaries.

Required observable regressions:

- First-run completed gap recovers the original result and actual parent wire
  continuation once, with zero child replay and a quiet subsequent restart.
- Uninterrupted qualified original completion runs output/result hooks once and
  persists the original call/result pair before the next provider request.
- Ready/claim persistence and authority failures cannot invoke fallback hooks,
  publish an unguarded result or advance the parent provider.
- A real hook effect followed by incomplete processing stays refused after restart
  without repeating that effect.
- Owner input, approval or authority changes, child mutation/revival and persistence
  races preserve exact ownership; no arbitrary leaf adoption is permitted.
- Nested hook mutation cannot alter the immutable ready payload or its hash.
- Existing recovered certificates and prior ten installed journeys remain valid;
  legacy refusal and unqualified async/batch/SDK behavior stay intact.

Use meaningful actual SDK/host/pipeline contracts, not source-grep tests or static
metadata echoes. Run focused affected tests, `bun check`, inventory validation,
independent source and observer review, then a fresh clean immutable staged runtime
and all eleven installed cases. Record exact candidate, runtime/manifest and harness
identities, command/cwd/exit/output, failures/skips and raw effect evidence. Require
all exact-head protected CI checks and actual hosted qualification before code-only
delivery; do not conflate local, synthetic CI and merge commit identities.

## Files and closeout

Expected source areas are the existing extension types/runner/wrapper and shared
event types (`extensibility/shared-events.ts`), SDK, session types/AgentSession,
tools session interface and metadata processing (`tools/output-meta.ts`), task
types/recovery/executor/index, and workflow host validator. These two concrete
plumbing files contain the existing ToolCallEventResult and the pre-hook metadata
pipeline; they do not expand recovery to other tools. Tests stay alongside those domains and in installed
recovery support. Update extension/installation/coverage docs, package changelog
and fork inventory as required; CI may retain additional first-run raw artifacts.
Do not broaden this into arbitrary tool recovery or new ledger endpoints.

The additional agent-core scope is limited to `packages/agent/src/agent-loop.ts`,
`packages/agent/src/types.ts` only if a context type is needed, relevant agent-core
contract tests and its changelog. Existing barrel already exports agent-loop.
Defend real core snapshot acceptance and copied/stale carrier refusal through
observable native dispatch contracts, not source text or identity-only echoes.

Use supported WorkService plan/evidence readback and standing automatic continuation
authority. Astra high owns complex implementation; Astra xhigh reviews design,
source and evidence; Astra low runs routine verification. Preserve the live checkout.
No full S2.2, native acceptance, owner audit reference or activation is claimed by
this bounded delivery.
