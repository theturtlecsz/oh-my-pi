# Worker lifecycle and evaluation hardening

Status: architectural direction incorporated on 2026-09-13; implementation coverage remains to be assessed.

Source: the owner's supplied GVS analysis. Its repository URL, revision, license text, and reported test behavior have not been independently verified here. References to `test/workflow.test.ts`, `test/sdk.test.ts`, `src/pi-worker.ts`, and `validation/README.md` are upstream investigation leads, not inspected local files. No upstream code is copied by this document. Before copying code, pin the source revision and verify and retain the applicable license notices.

## Decision and scope

Adopt the lifecycle guarantees and adversarial evaluation discipline through existing OMP runtime, WorkService, context, and evidence contracts. Do not adopt another ledger, scheduler, context service, or unconditional multi-role loop.

The useful architectural change is moving correctness out of conversational judgment. A worker may fail, report incorrectly, or lose its conversation; ordinary code must still know the attempt state, permitted next actions, and evidence that exists.

This hardening supports the current role/handoff qualification and original stabilization work. It does not replace executor qualification, WebUI delivery, the 5090/model stack, or Fleet Knowledge. Broad scheduler and benchmark work remain separate.

## Contracts to make explicit

### 1. Submission closes the mutation window

An accepted structured worker report means **submitted for evaluation**, not task completion. The runtime supplies and validates attempt, grant, packet, and candidate identities; model-supplied identifiers are not authority.

The logical lifecycle is:

1. Validate submission under the current attempt/grant.
2. Atomically refuse new mutations for that attempt.
3. Settle or terminate already admitted operations under an explicit policy.
4. Capture the stable candidate and exact evidence/artifact references.
5. Persist the attempt outcome durably.
6. Execute verification and independent review.
7. Permit only the next authorized transition.

Map these phases to existing state/transaction owners rather than adding a second state machine. A report followed by a write in the same response must not execute that later write. Background processes, delayed callbacks, and external worker adapters must respect the same fence. Prompt instructions alone do not establish it.

Order attempt mutations and the submission barrier. Do not globally serialize unrelated work or safe reads merely because the reference implementation serializes all tools.

### 2. Recovery is machine-readable first

Persist the last durable checkpoint, operation identities, assignment version, observed tool effects, changed-artifact references, existing candidate identity where available, check receipts, and interruption reason. Distinguish submission, partial/interrupted attempt, cancellation, provider failure, and blocker.

Recovery must remain possible without a final response, `/summary` prose, or another successful inference call. Optional synthesis can explain durable records; it cannot replace them or become independent corroborating evidence. Preserve existing independent audit and closeout requirements.

On restart, reconcile unresolved operations by stable identity before retrying. A committed operation whose response was lost must not be repeated as new work.

### 3. One frozen assignment, with retrievable supporting context

Extend the existing stage-context/ContextBundle path with a versioned assignment reference containing objective, acceptance criteria, approved decisions, non-goals, starting candidate, permitted scope, required evaluations, capability profile, budget, known failed approaches, and escalation conditions.

Keep authoritative requirements/grants/receipts separate from disposable working synthesis and historical evidence. Retrieval may enrich context but cannot silently revise requirements or assignment identity. Material plan/scope changes require a new version.

“Bounded worker” means a coherent reviewable work package that Gemini can implement, test, and repair independently. It must not become a series of tiny patches requiring Astra to direct every edit.

### 4. Evidence names exactly what it covers

Bind results to immutable candidate content, acceptance-criteria version, evaluation configuration, and relevant runtime/environment identity. A workflow counter is not a code identity. Candidate or policy changes must invalidate affected acceptance evidence before closeout.

Retained immutable evidence may be reused only under explicit applicability policy; a resume does not require indiscriminately rerunning every historical check. A narrative PASS never establishes reuse.

Keep separate: process completion, checks passed, requirements satisfied, review verdict, publication authority, and owner closeout. WorkService unavailability is an infrastructure condition, not “no active work” or fresh authority inferred from cached summaries. Optional knowledge enrichment outages retain their separate degradation policy.

### 5. Capability profiles preserve enforcement

Version each role's tools and limits. Keep mandatory authority/security controls, enable relevant capabilities deliberately, and deny recursive orchestration unless the role has an explicit delegation grant. Do not blanket-disable extensions/skills when they carry required enforcement.

Budgets, cancellation, retry limits, and terminal transitions are runtime responsibilities. Model budget warnings are assistance, not enforcement. Repeated normalized failures and unchanged candidate content can trigger escalation across attempts; alternating task IDs must not evade limits. These signals do not prove that a task is impossible.

## Deterministic event-sequence acceptance matrix

Drive the actual controller decision paths with replaceable worker/provider, verifier, clock, and transport boundaries. Reuse existing seams first; introduce a narrow seam only where a real contract cannot otherwise be exercised. These tests use scripted events, not paid model calls or a duplicate controller implemented in the test.

| Injected sequence | Required observable outcome |
|---|---|
| Stopped grant receives a delayed `/execute` continuation | No new worker is scheduled and no mutation is admitted |
| Valid completion report followed by a write in the same model response | Later write is denied; submitted candidate is unchanged |
| Submission races an already admitted operation or late callback | Declared settle/cancel policy holds; capture is stable and subsequent effects cannot change the submitted candidate |
| Worker edits, then crashes without a report | Interrupted outcome and recoverable effects/artifacts remain; no accepted completion |
| Final narrative or salvage-summary inference fails | Durable recovery state remains complete enough to reconcile the attempt |
| Auditor PASS names an earlier candidate, criteria version, or check configuration | Acceptance is rejected as stale; historical receipt remains inspectable |
| Completion/outcome event is replayed twice, including after restart | One transition and one authorized downstream effect/publication |
| A committed mutation loses its response | Reconciliation retrieves the prior result by operation identity; no duplicate write |
| WorkService is unavailable during authority validation | Explicit infrastructure failure; no fabricated idle/complete state or cached-authority fallback |
| Manager claims success while a required check fails or reviewer rejects | Controller cannot advance to accepted completion |
| Worker returns incomplete output, or CLI exits zero with denied tools/missing report | Attempt remains incomplete/failed as appropriate; transport success is not acceptance |
| Fixture-backed UI or simulated provider passes while required live integration is absent | Fixture qualification remains separately scoped; the delivery gate cannot advance to live/installed qualification |
| RPC acknowledges a prompt before work finishes | Runner continues to the actual terminal condition and captures resulting edits and usage |
| Cancellation is followed by a delayed result or a replacement grant | Old result cannot revive the stopped attempt or authorize the replacement |
| Worker tries a disabled capability or recursive delegation | Runtime profile denies it through every exposed execution path |

For each row, record: existing owner/seam, relevant existing test, reproduced failure or coverage result, missing contract if any, and the final evidence link. Initial status is **not assessed**, not “missing.” Fix only demonstrated gaps and reconcile native owners before adding work.

Likely inspection seams, not verified coverage claims: `packages/coding-agent/src/task/executor.ts`, `session-system/extensions/workflow/pending-ops.ts`, `auditor-runner.ts`, `audit-tcb.ts`, the execution/recovery tests under `session-system/tests/`, and WorkService execution-grant and close-attempt contracts.

## Test the evaluator and runner

Keep worker-authored development tests and independent acceptance evaluation distinct. Demonstrate that a legitimate repair passes and plausible bad repairs fail:

- Removing authorization must fail denied-operation cases even if the happy-path request now succeeds.
- Disabling grant validation must fail stale/withdrawn-grant cases.
- Suppressing a check, weakening its assertion, or inflating a timeout must not satisfy the trusted evaluation contract.
- Forging success metadata, skipping tests, or pointing at an old green report must not satisfy candidate-bound evidence requirements.
- Substituting fixture data for a required live source must not satisfy an operational delivery criterion, even when the fixture UI and security checks pass. Retained PASS artifacts alone do not replace executing the declared behavioral evaluation against the relevant candidate.

Use trusted grader fixtures and runner self-tests. Verify task delivery, actual terminal completion rather than RPC acknowledgement, edit collection, failure recording, and usage accounting. Unknown usage stays unknown; failed and interrupted attempts remain in totals.

Where graders or holdout fixtures must be hidden, verify a real access boundary against all worker capabilities, including shell/interpreter/subprocess paths. A different directory or prompt instruction is insufficient. Public acceptance criteria may remain visible; hidden grader access and grader integrity are separate contracts.

## Integration order and ownership

| Pass | Work and acceptance | Roadmap placement |
|---|---|---|
| A — Coverage and lifecycle | Inventory existing guarantees; add adversarial sequences; repair demonstrated late-write, stale-continuation, duplicate-result, interruption, and false-completion gaps | EXEC-3 for the bounded role loop; existing OMP stabilization owners for native gaps |
| B — Durable recovery and packets | Prove recoverability without narrative; tighten versioned assignment/context binding; retain one authority and existing stores | OMP-1, shared native Fleet foundations, and FLEET-5/context work |
| C — Evaluator/runner correctness | Known-good/bad repair fixtures, runner self-tests, protected grader access, trustworthy usage/terminal accounting | Relevant existing trusted-evidence/Harbor ownership; correctness work can proceed before benchmarks |
| D — Economics experiments | Compare workflows with fixed model configuration, then compare model allocation on the selected workflow; use repeated tasks and external grading | Deferred, optional experiment tranche; not a release prerequisite and not started by this roadmap update |

Collect passive operational measures during ordinary work: accepted outcomes, false completion, elapsed time, actual usage/cost where reported, repairs, escalations, and manual rescue. Later experiments must separate workflow effects from model effects and include interrupted recovery. No broad benchmark runs, additional ideation role, or second benchmark framework are introduced now.

Fable owns the repository-grounded coverage map and evaluation plan. Gemini owns coherent implementation and repair packages. Kimi independently challenges behavior and coverage and executes declared checks. The supervisor drives routine transitions and reports machine-readable state to WebUI. Astra reviews architectural deviations and completed milestones.

## WebUI and Fleet consequences

WebUI monitoring should expose submission/interruption, infrastructure availability, candidate binding, check/review state, and remaining authorized transitions distinctly. It must not turn an RPC acknowledgement, worker report, or process exit into a green task-completion indicator.

Fleet Knowledge consumes committed outcomes and retained primary evidence, including failures. Missing prose must not erase an episode; repeated summaries must not multiply support. Submission fences and revision-safe receipts strengthen the existing learning and correction design without introducing another memory core.

This document records analysis and planned acceptance conditions. No OMP/GVS test suite or benchmark was run for this roadmap change, and no runtime guarantee is marked implemented by it.
