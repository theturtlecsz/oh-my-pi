# Open Bookends work: complete review, 8 September 2026

All **23 open, nonarchived items** in **The Bookends** were reviewed through
WorkService: current description, structured scope/criteria, relationships,
workflow evidence, and latest execution state. All were BACKLOG at this snapshot.
The workspace response contained 413 items, 97 relationships, and 34 projects,
below the API limits; the review did not omit a capped page.

This is a dated planning review, not a replacement ledger. No item was closed,
deleted, merged, or reprioritized by this review. Re-read live records when working
an item. Sixteen items have empty structured criteria but may have requirements
in their descriptions; fill the active item's structured contract before coding.

| Item | Treatment | What still needs doing |
| --- | --- | --- |
| OMP-202 | Later umbrella | Preserve CPK program; do not execute the umbrella as a stabilization task. |
| OMP-203 | Reconcile | Compare old H0/two-repository safety assertions with delivered 209/210/211/212 and current protections/CI. |
| OMP-204 | Later | Broad CPK inventory and performance budgets; not a prerequisite for installation isolation. |
| OMP-205 | Later | General plugin manifests/graphs and shadow composition. |
| OMP-206 | Later | General transition/event/guard refactor; admit only a concrete needed seam during stabilization. |
| OMP-207 | Later | Profiles, projections, and Web/Fleet integration. |
| OMP-208 | After baseline | Measured Advisor/Observer migration; preserve current coverage until replacements qualify. |
| OMP-214 | Reconcile before acceptance | Verify historical completion-gate activation/ancestry and cleanup against current evidence; do not replay old cleanup instructions. |
| OMP-217 | Reconcile now | Verify current CI and any remaining reproducible environment failure; the September-1 red-main description is historical. |
| OMP-219 | Before queued work | Fix/test queue dependency ordering and skip behavior; initial qualification uses single-item execution only. |
| OMP-228 | Standing guardrail | Preserve compatibility tracking and parked conflict-resolution work; do not recreate delivered 229. |
| OMP-230 | Separate upgrade | Complete guarded upstream incorporation later; keep the admitted runtime fixed while measuring reliability. |
| OMP-233 | Stabilize | Reproduce/fix pause scoping, terminal handling, admission/ref identity, and inconsistent predecessor handling during recovery. |
| OMP-241 | Separate routing change | Preserve existing candidate and resolve audit blockage; do not switch model routing during a canary streak. |
| OMP-243 | Deployment prerequisite | Qualify the intended prompt/instruction payload; absorbs canceled 244 and explicitly blocks 249. |
| OMP-246 | Stabilize | Durable continuation and crash-after-effect reconciliation; make the intended whole-process CI coverage explicit in scope. |
| OMP-248 | Diagnose before canaries | Reproduce required-instruction deadline omission through the real loader; implement only an established failure. |
| OMP-249 | Current work | Finish PR #20/CI, exact installation qualification, recorded handoffs, and later owner-scheduled promotion. |
| OMP-250 | Optional later evaluation | Harbor M2 adapter/fixtures; depends on 246 and delivered 245; does not own trusted-runner authority or paid trials. |
| OMP-252 | Later, budgeted | Harbor model comparisons after 250; exact trial plan/budget approval required. |
| OMP-262 | Stabilize | Reproduce error-with-write and duplicate plan-candidate identity; verify refused operations leave state unchanged. |
| OMP-263 | Small cleanup | Correct stale release comments when appropriate; not a runtime-readiness blocker. |
| OMP-264 | Stabilize audit path | Qualify absolute repository identity and fresh host activation before relying on autonomous review. |

## Dependencies that change the implementation order

1. **243 is an open recorded prerequisite of 249.** The original deployment also
   names canceled 244 (absorbed by 243) and completed 251. Isolation code can be
   qualified independently; live promotion must either include qualified 243 or
   explicitly admit a smaller payload through the existing policy.
2. **Terminal predecessor handling differs between admission and recovery.**
   Admission excludes DONE/CANCELED blockers, but the current host recovery
   preflight rejects every active blocking edge without checking predecessor
   state. This is a source-established inconsistency, not yet a reproduced
   whole-process failure. Reproduce/fix under 233/246. Do not delete historical
   244/251→249 or 229→230 relationships to work around it.
3. **250 depends on open 246 and completed 245.** The subsequent 250→252 dependency
   is written in prose but absent from the native relationship graph. Reconcile
   it when admitting that later work.
4. **243/246/248/249/250 still parent to canceled 242.** That preserves history; it
   does not cancel the children or justify resurrecting the umbrella.
5. **No cross-project native blockers were found.** Some obligations span both
   `oh-my-pi` and `omp-webui`; Bookends item 210 handles Web repository work and is
   DONE. One repository's green CI does not satisfy both-repository H0 criteria.

## What the first checklist had missed

- Explicit treatment of instruction payload 243 and timeout diagnosis 248.
- Historical 203/214/217 reconciliation instead of assuming their old descriptions
  were current defects.
- Single-item-only qualification while 219 remains open.
- Preserved 241 routing work and fixed model configuration during measurement.
- Distinction between testing an upstream candidate and completing upgrade 230.
- The terminal-predecessor recovery inconsistency discovered during this review.

## Gaps to admit explicitly when reached

No current open item owns trusted runner-produced verification authority;
OMP-250 explicitly excludes it and DONE OMP-247 does not prove it. Phase 3 must
reproduce that gap and admit one linked item before implementation. A bounded
typed-intake implementation is also not represented by these 23 items; OMP-243's
wording changes are not that feature. Neither gap should be hidden in a broad
CPK refactor or an unrelated existing issue.

Use [the session guide](omp-stabilization-plan.md) for checkpoint order. Current
scope, plan bytes, checkpoint results, and next action belong in WorkService.
