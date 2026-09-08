# OMP stabilization: start here

Goal: one installed OMP can finish normal work and recover from defined failures
without you repairing its workflow. Use an independent coding tool to develop it.
Expect multiple sessions. Each session finishes one bounded checkpoint, records
evidence, and leaves one exact next action. A phase may take several sessions.

**Start with phase 1. Do not deploy PR #20 while CI is red.**

The [complete open-work review](bookends-open-work-review.md) accounts for all 23
open Bookends items as of 8 September 2026, including dependencies and gaps.

## What you do each session

Paste this into the coding agent:

> Continue OMP stabilization using docs/omp-stabilization-plan.md. Until PR #20
> merges, read it from branch codex/omp-installation-isolation. Read the current
> WorkService issue and latest handoff first. Complete the next unfinished
> checkpoint only. Reuse delivered fixes, run the relevant checks, and record
> exact evidence and the next action in WorkService before stopping. Keep the
> live installation unchanged unless this session explicitly authorizes cutover.

The agent handles checkout, testing, CI inspection, and routine ledger updates.
Implementation stays in an isolated candidate worktree. Reuse
`/tmp/omp-stabilization-s1bXkP` while it exists and matches PR #20; otherwise create
a fresh worktree from the PR branch. Do not switch or edit the live source-linked
checkout at `/home/thetu/oh-my-pi` to continue this work.
You answer material requirements questions and approve consequential operations
when the existing policy requires it. You do not need to type /plan or /summary
merely to record an implementation plan or progress from an independent tool.

## Where progress lives

- **WorkService:** current scope, acceptance criteria, plan, evidence, and handoff.
- **This guide:** ordered checkpoints and definitions of completion.
- **GitHub:** code, review, and CI tied to exact commits.
- **Release manifest:** exactly which installation was tested.

This guide is not a second backlog. A checkbox below names work to verify; the
latest matching ledger receipt tells whether it is complete. Never infer issue
completion from a checked box, merged PR, model message, or source-only test.

## Session routine — agent owns this

At start:

1. Read current issue revision, dependencies, plan, and latest handoff.
2. Check branch/commit, worktree dirt, PR head, and all required CI results.
3. State the single checkpoint being completed and its observable exit condition.

Before stopping:

1. Save recoverable work; report exact branch, commit, and PR.
2. Record checkpoint IDs completed, commands/results/skips, artifact identities,
   unresolved failures, and exact next action through the supported ledger client.
3. Read back the receipt. If recording fails, preserve the pending operation and
   report the failure; do not claim it was recorded.
4. Leave issue acceptance unchanged until its real audit/completion gate passes.

## Phase 1 — finish installation isolation (OMP-249, PR #20)

- [ ] S1.1 Reconcile delivered fixes and current ledger scope. Preserve OMP-245,
  OMP-247, OMP-251, OMP-261, and OMP-229 as delivered; do not implement them again.
  Reconcile historical OMP-203/214/217 claims with current evidence and delivered
  OMP-209/210/211/212; an old issue description is not a current failure report.
- [ ] S1.2 Record OMP-249's bounded scope, acceptance criteria, exact plan, and
  current PR/evidence. Existing description and deployment prerequisites stay.
- [ ] S1.3 Fix every failing PR #20 check. Reproduce the failure under CI's Bun
  version; keep incomplete/foreign runtime directories refused.
- [ ] S1.4 Run the complete Work Ledger CI job: unit tests, PostgreSQL integration,
  candidate smoke, execution smoke, staging, and installed-process qualification.
- [ ] S1.5 Confirm all required checks are green on the exact final PR head.
  Required checks that are missing, skipped, or still running do not pass.
- [ ] S1.6 Resolve independent review findings and record actual test results,
  manifest, supported versions, and remaining limitations.
- [ ] S1.7 Follow the authorized review/merge/acceptance route. Code delivery and
  live activation remain separate; leave unfulfilled deployment criteria open.

Exit: reviewed code and exact installation evidence; no live cutover assumed.

## Phase 2 — prove one worker can recover (OMP-246, OMP-233)

Use one admitted release and disposable state. One writer owns critical state
changes. An independent reviewer may work in parallel.
Use single-item execution; `/execute --queue` waits for OMP-219 qualification.

- [ ] S2.1 Map each failure below to an existing executable test. Add only missing
  whole-process coverage; scripted model responses must not replace session or
  worker machinery being qualified.
- [ ] S2.2 Kill/restart between stages; recover the same authorized progress.
- [ ] S2.3 Crash after a ledger commit but before its response; reconcile once.
- [ ] S2.4 Lose a push/merge response after the effect succeeds; inspect the real
  remote before retrying and prove no duplicate effect.
- [ ] S2.5 Deliver queued work after cancellation; prove no new work or effect.
- [ ] S2.6 Break auditor discovery; stop/cancel still work and review cannot pass.
- [ ] S2.7 Present incompatible runtime/service versions; reject with a precise
  reason. Trusted runtime changes need applicable fresh authority. Terminal
  grants remain terminal.
- [ ] S2.8 Reproduce OMP-262 plan-stamp inconsistency and OMP-264 relative audit
  paths before admitting their fixes. Preserve write-outcome and path identity.
- [ ] S2.9 Require these journeys in CI and record actual process/effect evidence.
- [ ] S2.10 Reproduce the admission/recovery discrepancy for completed or canceled
  predecessors. Resume must not refuse solely because their historical edges
  remain active. Fix state handling under OMP-233/246, not by deleting history.
- [ ] S2.11 Perform OMP-248's delayed required-loader reproduction through
  `before_agent_start`; record omission/retry/warning behavior before implementing
  degradation visibility or claiming complete instruction loading.

Exit: defined failures recover correctly or stop clearly, without manual repair.

## Phase 3 — prove completion means completion (OMP-249 qualification)

OMP-247 delivered existing completion controls. Test them first. Do not reopen it
or assume that delivery proves every remaining evidence requirement.

- [ ] S3.1 Show that prose about tests, a bare PASS, or a push alone cannot complete
  work through any authorized client in scope.
- [ ] S3.2 Bind verification to runner, command, cwd, exit status, preserved output,
  environment, candidate, and acceptance criteria. If trusted runner authority
  is missing, admit one linked ledger item for that concrete gap before coding.
- [ ] S3.3 Bind audit to immutable candidate and independent reviewer; changed
  candidates invalidate the applicable evidence.
- [ ] S3.4 Test changed PR heads/checks and distinguish reviewed tree, candidate
  commit, merge commit, and installed artifact identities. Include OMP-214's
  still-open installed completion-refusal evidence before any historical cleanup.
- [ ] S3.5 Exercise the real acceptance route. Never manufacture audit receipts,
  owner command references, or direct database updates to get past a blocker.

Exit: service-enforced evidence matches actual results and the admitted candidate.

## Phase 4 — run ordinary work (OMP-249 qualification)

- [ ] S4.1 Prepare an exact canary plan: five known-solvable task types, 20 trials,
  pinned model/effort/tool/Advisor/Observer configuration, disposable repositories,
  proposed model budget, and any disposable GitHub target. Obtain required authority
  before paid or external-effect trials.
  Keep single-item execution and model routing fixed. Preserve OMP-241's candidate
  for separately qualified routing work; do not mix it into a running trial streak.
- [ ] S4.2 Run deterministic known-good and deliberately bad cases separately.
- [ ] S4.3 Complete 20 consecutive accepted trials without unplanned workflow
  repair. Normal review/remediation is allowed; reconstructing grants is not.
- [ ] S4.4 Record outcome, human repair minutes, waiting cause, unexplained stops,
  recovery correctness, and cost per accepted task. A failed trial becomes a
  reproducible regression before restarting the acceptance streak.
- [ ] S4.5 Run the same boundary corpus on a selected upstream integration
  candidate; a clean merge and current inventory alone are insufficient. This is
  separate from completing OMP-230's guarded upstream incorporation; reuse
  OMP-229's guardrail and OMP-228's preserved integration work.

Exit: initial adoption evidence, not statistical proof of universal reliability.
Harbor OMP-250 and paid comparisons OMP-252 remain separate optional work.

## Phase 5 — deliberate live promotion (OMP-249 / activation child)

- [ ] S5.1 Resolve current deployment prerequisites; name exact approved payload,
  installation, config, migration compatibility, and last-known-good fallback.
  Explicitly resolve open OMP-243, canceled/absorbed OMP-244, and delivered OMP-251.
  A smaller first payload requires an explicit disposition; isolation tests do
  not silently satisfy the original instruction/wording deployment requirement.
- [ ] S5.2 Prepare backups, restore evidence, service units, and rollback procedure.
  Use the existing interactive owner approval for contract/schema changes.
- [ ] S5.3 Owner authorizes a cutover window. Drain/stop affected work, preserve
  artifacts and sessions, and reconcile unfinished effects.
- [ ] S5.4 Activate the exact qualified installation; inspect actual loaded CLI,
  extensions, auditor, native dependencies, service, contract, and migration IDs.
- [ ] S5.5 Run bounded live checks and record deployment acceptance. Roll back only
  when persisted schema/config remain compatible; directory rollback is not a
  database down-migration.

Exit: deployed release meets its recorded acceptance criteria. Only then resume
bounded typed intake. General CPK, broad hot reload, Fleet, new hosts, and major
UI work stay deferred unless they remove a reproduced stabilization blocker.

## Technical references

- [Installation commands and rollback details](installation-isolation.md)
- [Readiness and failure diagnosis](workflow-production-readiness.md)
- [WorkService operator/approval rules](work-ledger-operations.md)
- [Upstream compatibility guardrail](upstream-guardrail.md)

External development uses existing `createWorkBackend`/`WorkClient` interfaces.
Scope revisions use the expected revision ID; plans and handoffs use the existing
receipt and pending-operation mechanism. Generic audit/closeout appends do not
replace native acceptance. The agent must surface a real authority/interface
blocker and prepare its bounded remedy; successful code work can remain reviewable
without claiming acceptance prematurely.
