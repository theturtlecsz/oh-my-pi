# S2.2 completed child / missing parent result reproduction

2026-09-08. Reproduction plan only; no production repair or issue completion is authorized by this document.

**Next bounded reproduction remains S2.2: child completed durably, original parent task result not yet durable.** Do not jump directly to S2.3 or treat nine installed passes as full S2.2 completion. Prepare the isolated observer while PR23 final checks run; execute this new fault case only after PR23 code delivery. Local nine-case qualification and hosted run 34204437968 on 80d4 passed with zero skips. Final fixture-only head 0e2976e25a awaits CI34206087726. This preparation uses branch codex/omp-task-completion-recovery at that exact base; PR23 stays frozen in its separate worktree.

## Scope reconciliation

Supported WorkService reads confirmed:

- OMP-246 remains BACKLOG, revision 2 `f5bb9640-3491-50b7-bdad-2a77f9c102e3`. Scope explicitly requires ordered Phase 2 qualification, real crash-before-fix evidence, existing session/journal reuse, and no new controller framework. AC2 requires same authorized progress at defined process-stage boundaries without silent lost continuation or duplicate execution. AC3 separately names WorkService commit-before-response reconciliation.
- Latest read handoffs `eb41717c-daaa-52b7-826f-bb5a344c6f2c` and `e864c4bc-0f4e-5d6a-9286-0b9ce1f0e674` retain full S2.2/native acceptance/live activation open. Their recorded run state predates root's newest local nine-pass report; do not claim those receipts already record the new result.
- OMP-249 remains BACKLOG, revision 4 `73ffb84b-30a1-59e5-aa80-05332e0cf368`. Exact-head checks, applicable native acceptance and owner-scheduled deployment remain requirements; code merge alone does not fulfill them. Latest read pointer is `81af6ba8-1371-5cd2-91a3-f63ae57c1268`.

The guide's Phase 2 exit permits defined failures to recover or stop clearly, but that does not silently narrow OMP-246 AC2 or close the expressly open stage corpus. Existing task plan deliberately excludes completed child with missing parent result. That excludes it from PR23 implementation, not from the overall ordered recovery qualification. Unknown/effectful histories may remain explicit refusals; this next experiment is one known read-only task with actual successful yield, not a demand to recover arbitrary tools/async work.

Current distinct proofs: queued intent before injection; persisted unanswered controller; real criteria-seal progress; real task active before child answer; repeated child kill after prepared context; parent result already durable before parent request. Last two do not cover the interval **between child completion and original parent result durability**.

## Concrete reproduction, no production changes first

After PR23 delivery, reuse an explicitly recorded admitted release with its reviewed production bytes, current `task-active` fixture, same synchronous flat bundled task, local `read` then normal successful `yield`, real disposable WorkService/PostgreSQL and local bare Git. Keep actual managed CLI, SessionManager, registry, monitor, result hooks/finalization and grants. No replacement extension/tool, journal edit, phase edit or invented result.

Add a small loopback HTTP forwarding proxy between the disposable WorkClient and actual disposable WorkService. Point only that case's supported `client.json.base_url` at proxy before setup; keep bearer handling unchanged and never record token headers. Inspector/health reads go directly to real service so the test itself does not block. All requests/responses forward unchanged until the selected boundary.

Natural production barrier exists in `TaskTool.recoverPersistedCall` (`packages/coding-agent/src/task/index.ts`, around 1619–1639): after `runPersistedTask` returns, code flushes the child journal, then awaits `request.validateAuthority()` before returning the parent task payload. The host authority validator makes `WorkClient.execution` GETs, routed as `/v1/workspaces/<workspace>/execution[/<grant-or-key>]` (`packages/work-client/src/index.ts:963`). Parent AgentSession performs further authority validation before constructing/emitting its original ToolResult.

Use existing initial task-active SIGKILL and restart journey; on recovered child's successful completion:

1. Once the exact child's new output artifact exists, hold subsequent execution-authority GET response bytes at the proxy. Forward GET to real service and retain its actual successful response before withholding headers/body. Hold all qualifying authority reads in this phase to prevent a concurrent check slipping through; do not identify the call by arbitrary request count.
2. Before killing, require **all** witnesses: original binding/reverse init/core preparation valid; child real `read` and successful `yield` tool results durable; output artifact matches actual yield; genuine `subagent_lifecycle` completion for original task/call observed; original parent task result absent from both persisted parent branch and RPC result events; exact authority GET response held; no parent provider continuation. Artifact existence alone is insufficient because finalizer writes it before final lifecycle/error classification.
3. Re-read authoritative grant/workflow directly: same active grant/item/revision/version/reservation and no new workflow mutation. Capture workspace contents/HEAD/dirty state, remote refs, both journals, original binding/entry/call IDs, completed lifecycle, held HTTP request path/status/body digest, launcher/CLI process identities and process group. This barrier claims child completion and missing parent result, not completion of every parent result hook.
4. SIGKILL actual launcher/CLI group. Assert signal/exit and that original CLI is gone. Leave provider/service/proxy alive. Disable only this proxy withholding phase for restart; retain same configured URL and admitted runtime. No repair prompt, `/retry`, fresh assignment or reconstructed journal.
5. Restart same actual parent session. Record exact outcome. Expected current behavior from source: explicit `Child already has assistant/tool/effect history or unbound preparation; result-gap reconstruction is unsupported`, with no child request, no new task id, no parent result and unchanged grant/effects. Preserve this negative even if refusal is intentional. Repeat restart to test preservation/no duplicate effect.

If completed lifecycle and absent parent result cannot both be observed under held authority response, barrier was not reached. Refine the external observation; do not replace result hooks or introduce a generic hook transaction mechanism. Capture attribution before deciding any production remedy.

## Exit for this reproduction

An independently reviewable real-process record proves the exact child-completed/parent-result-missing window and its actual restart behavior. A clear refusal may pass a **refusal-safety** assertion; it is not automatic recovery of completed work and does not alone close S2.2. Root records result/limits through existing WorkService plan/handoff and reads back receipt. Any subsequent remedy gets its own minimal plan for this bound completed task and original result; no generalized effect replay or hook transaction framework is pre-authorized by the experiment.

Reuse current `capture_task_active_recovery`, provider read/yield sequence, `require_task_binding`, complete-JSONL parsing, process-group capture, state/effect checks, and evidence uploads. Keep existing nine cases unchanged. New barrier/report belongs to Python fixture support; no production fault toggle is needed. Runtime artifact remains immutable; a harness-only baseline run reports separate runtime/harness identities honestly.

## Why this is not S2.3

The held response is a read-only authority GET. No WorkService command commits at the cut. Therefore this is remaining S2.2 task-stage recovery, not evidence for AC3/S2.3.

S2.3 remains next separately named checkpoint after S2.2's recorded disposition. It should reuse the same proxy to forward a real `/v1/commands` mutation, observe actual committed response and service operation identity, withhold that response, SIGKILL issuer, and resume from its **client-written unresolved operation claim**. The original operation id/transition must reconcile once and survive another resume. Do not write a synthetic resolved claim or count a read-only GET cut as a committed-response-loss test. Existing component operation idempotency and smoke's synthetic crash-gap record do not satisfy this whole-process proof.

No live installation/config/database, paid provider, GitHub effect target, terminal grant, acceptance criterion or contract/schema changes belong to either disposable reproduction. Root may progress routine planning/testing under standing charter; this analysis does not mark S2.2 complete or alter checkpoint order.
