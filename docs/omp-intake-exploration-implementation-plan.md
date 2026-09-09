# Bounded intake exploration: implementation contract and ownership

**03B addendum 2026-09-09 v1.** This is the normative documentation for the accepted bounded amendments and their implementation ownership. It does not claim an exploration runtime, qualified intake, measured benefit, native acceptance or activation exists.

## Scope, provenance and precedence

The original programme has five outcomes: a qualified stable installation that finishes ordinary work and recovers without owner workflow repair; CI that qualifies actual workflow/process boundaries; trusted issuance and delivery evidence tied to real checks/effects; one useful bounded typed-intake path after deliberate promotion; and measured owner benefit. The five stabilization phases and the full V9.1 roadmap are different scopes. This addendum advances contract/ownership preparation only.

The immutable original completion plan is `COMPLETE-PLAN.md`, reviewed SHA256 `55fd67b0a354bcb14e22c35ea706d142993595d1ac9a7a2682f6c24090fb1b04`. Its historical read-only P0 rows and guide-preservation proposal are superseded only within the later owner-authorized documentation/ownership amendment, including the requested guide backlink and explicit stale-binding disposition below. The original plan/receipt bytes remain unchanged. No blanket permission for paid trials, live effects, authority changes or unrelated work follows.

These owner-retained V9.1 source files remain immutable in `assessment-20260909/archive/reanalyzed-omp-programs/` under the OMP stabilization evidence root:

| Source | SHA256 |
| --- | --- |
| `03_NEURO_SYMBOLIC_INTAKE_TECHNICAL_CONTRACT.md` | `426aaa41db0a02a2e986a432301271ca36dd853ee29f0fb291f7d35a8027269b` |
| `03B_BOUNDED_DIVERGENT_EXPLORATION_CONTRACT.md` | `c5e39ca78d03e011733fa8cb5ab1a0dd4e6da1066d5c97b67130b797e4c01e75` |
| `03D_DETERMINISTIC_WORKFLOW_HARDENING_TECHNICAL_CONTRACT.md` | `67393ef2d7ae20e43bf7722aeecbc91e473fdc60120662a2ba9d8bd19d04244d` |
| `03E_CONSTITUTIONAL_PLUGIN_KERNEL_TECHNICAL_CONTRACT.md` | `bf45972f69e9881a0e09be7a0e160586473c14e2541d3306eff5bbb32d9ba955` |
| `PROGRAM_DEPENDENCIES.md` | `8e0755f2a896f5d69f19391247acc379ecfeceee47d0ebd793e60b4c80743ce9` |

This addendum amends only the explicit 03B touch points below. Unchanged 03, 03D, 03E and PROGRAM_DEPENDENCIES requirements prevail on conflict; stop for owner resolution rather than widening authority. Unamended 03B constraints remain. WorkService alone owns durable workflow authority; the owner retains semantic meaning and consequential policy/effect decisions. Model output, scores, documentation and evidence do not issue grants, readiness or acceptance. V9.1 does not authorize hot authority recomposition. Installed refresh remains disabled; terminal grants remain terminal.

Fable architectural advice and fresh exact-candidate independent review are hard gates. Zero GitHub approving votes are required under the later owner decision; required CI and applicable native audit/acceptance remain separate. No literal owner `/plan`, `/summary` or `/done`, native receipt or effect may be fabricated.

## A. Generation and comparison are separate responsibilities

For future E1 schema evolution, introduce a versioned generation-only projection that removes candidate-level comparative `tradeoffs` from `ExplorationCandidateBase` (03B line 1110). Preserve versioned historical schemas/read artifacts and compatibility fixtures; do not reinterpret historical outputs as if they used the new projection. The critic's option-level `tradeoffs` field remains.

Generation returns proposed answer/candidate, descriptive mechanism/rationale, named assumptions or relaxations and references. Assertion kinds `consequence` and `risk` remain allowed (03B 1068–1074), including self-risk and failure ideation. Generators do not comparatively rank, judge or prune their batch; branch isolation also prevents sibling comparisons. This changes the required generation output projection, not private reasoning capture.

Comparative evaluation, rankings where permitted, tradeoffs, weaknesses and model trap judgments belong inside the **existing synthesis call** (03B 1748–1759). “Critic” introduces no new call, route, budget partition or failure stage. Synthesis still receives the entire admitted valid pool and uses existing failure/`unranked_partial` behavior. A later separate critic-call proposal would require separately admitted schema, budget and study changes.

Amend generation prompt policy at 1708–1709 accordingly. The first-view tradeoffs at 1763 come from synthesized options, not mandatory generator self-comparison. Owner decisions remain neutral with no recommendation reference; planning can retain its expressly advisory recommendation. Deterministic schema/reference/duplicate/explicit-constraint checks at 1714–1744 remain before model comparison. Model labels cannot delete candidates, make conflicts selectable/compliant or affect readiness. Preserve all valid raw proposals/warnings under existing retention, redaction and containment rules; never capture private chain-of-thought.

## B. Eligible profiles reserve one existing structural slot

Eligible reviewed profiles reserve exactly one existing branch slot for a reviewed inversion, assumption challenge or cross-domain operator. Do not add a branch, generated production frame or automatic spend. Keep the existing `maxBranches`/`maxCandidatesPerBranch` limits at 03B 263–264 and deterministic selection from policy, target and input hashes.

Cross-domain analogy is a reviewed `wildcard`-family frame, not a new family enum. Record `frameId`, reviewed operator identity/version, selector version, eligibility and fallback reason. Candidate `kind` remains unchanged; operator labeling does not add a candidate-kind value. Ineligible profiles explicitly record why no structural slot applies; unavailable suitable operators follow the reviewed deterministic fallback policy. Frame names are a structural proxy, not evidence of material diversity.

Original source and admitted constraints remain immutable. A counterfactual explicitly names and references the challenged original assumption separately from actual input. It cannot silently rewrite the question, become ratified authority or be shown as compliant/selectable when it conflicts. Failure/security ideation and labeled rejected challenges remain legitimate; deterministic screening and disclosure still apply.

## C. Breadth is bounded and observable

Add required positive integer profile field `targetCandidatesPerBranch`, no greater than `maxCandidatesPerBranch`, and capture its exact value on each branch attempt. Profile compilation retains every finite hard maximum and the component-wise compiled-minimum ≤ accepted ≤ offered-hard-limit relationship. A four-branch × target-four configuration is only a pilot inside those caps and separately authorized finite resource bounds. It is not a universal default or spend authorization.

Capture count availability at provider ingress, before candidate admission. An unknown, malformed, unobserved or quarantined provider envelope must not become a reported zero merely because no candidate was admitted. Only metadata allowed by ingress containment may proceed; quarantine bytes never do. For a completely classified known returned batch, proposal counts have these units:

| Field | Meaning |
| --- | --- |
| `returnedCount` | Total returned proposals whose batch/count is known; every member is assigned to exactly one bin below |
| `invalidCount` | Returned proposals rejected by schema/reference/admission validity checks, excluding the quarantine bin |
| `quarantinedCount` | Returned proposals assigned to quarantine; no quarantined bytes become downstream candidates or disclosure |
| `validCount` | Admitted valid raw proposals, including canonically labeled exact duplicates |
| `duplicateCount` | Subset of `validCount` marked as exact duplicates; one canonical representative per duplicate group is not counted as a duplicate |
| `distinctValidCount` | `validCount - duplicateCount` |
| `shortfall` | `max(0, targetCandidatesPerBranch - distinctValidCount)` when the requisite counts are known |

The partition is `returnedCount = invalidCount + quarantinedCount + validCount`; the three bins do not overlap. Do not subtract duplicates twice or equate raw valid count with distinct valid count. Unknown/unreconciled returns or classifications remain explicitly unknown, never zero. Known provisional counts and classification-complete status cannot masquerade as terminal counts or a proven shortfall. If containment makes a count unknowable, retain unknown metadata rather than inspect or disclose quarantined content to fill it.

Record underfill/shortfall reasons, attempt status and existing failure/fallback outcome. No padding, automatic retry or additional usage to meet the target. Existing successful-branch/diversity minima still decide honest partial/failure behavior. Preserve canonical duplicates as labeled members of the raw valid pool; semantic clustering is advisory and cannot silently delete proposals. Synthesis sees the complete valid pool, with existing access/retention and DLP rules intact.

## D. Focused evaluation uses the existing controls

OMP-270 adds separate focused generation-posture, reviewed-operator and breadth contrasts, plus pre/post-synthesis material concept coverage, to existing X-A/X-B/X-C0/X-C/X-D. Choose one design question at a time; no mandatory giant factorial or unbounded critique/regenerate/refine loop.

Preserve X-A qualified intake without alternatives, X-B one alternatives pass, X-C0 unframed branches, X-C framed branches and X-D randomized deepening offer. Report randomized intention-to-treat separately from self-selected use. Compare material distinct concepts in the retained valid pool with prominent final options using a preregistered operational rubric for materiality, viability, distinctness and traps. Owner/outcome evidence remains necessary; a sole LLM novelty score is not the oracle.

Hold non-target variables fixed, including frozen input manifests, tools, branches/candidate targets/caps, synthesis policy and output format. At E4 preregistration bind actually available provider/model/version/effort/route identities within the approved architecture; historical Flash/Sol role names are not a current availability claim. If the required E4 routes are unavailable or arm definitions must change, require an explicit contract amendment before substitution; configuration alone cannot change the experiment. A later route change also needs a separately admitted study/cohort, not a silent rerun. Report primary matching basis, realized calls/tokens/currency-or-resource use and deviations. A posture change can change realized tokens; equal caps alone and equal tokens across models are not equal compute or cost.

Preserve negative controls/seeded traps, representative repeats, randomized option order, normalized/blinded presentation, unaided owner choice, missing/abandoned-run handling and pilot/holdout separation. Keep all §16 safety zeros, non-inferiority, outcome/owner burden, resource compliance, stable benefit over X-A and an equal-compute control, and rollback requirements. No measured OMP benefit follows from this documentation or from upstream preference scores. B0/B1 models, routing and comparison variables remain untouched by E4 or upstream preparation.

## Native owners and execution traceability

Five existing WorkService owners carry separate full scopes. They are children of OMP-202 (`f7cf53f0-c2b9-4985-977e-686360bfb3e8`), the available programme umbrella found by native full-content/lineage search; this does not assign CPK semantic authority over NSI. No dedicated full NSI/SQL/OWEB/FLEET gate owner was found in that capture, so missing stage ownership/proof remains explicit at future admission.

| Cut | Native owner and work ID | Scope and entry | Revision binding |
| --- | --- | --- | --- |
| P12 | OMP-266 — `95d52a7f-77a4-4434-8bcc-ab90ccbbfc8b` | One useful small code-change typed-intake path; initial qualified P11 promotion and deterministic floor first; independent of optional exploration | r1 — `65bbf08c-7a76-461b-992c-941b401110c0` |
| E1 | OMP-267 — `ce215e63-9072-4622-8051-9df9a2e6350c` | Contracts, policy, frame/profile registry and shared bounded-model-job reuse inventory; runtime requires H0 and NSI-1+2+3 | r2 — `5ed47036-ffbe-5aa4-9c0c-f605e3ee6fc0` |
| E2 | OMP-268 — `4daad6ca-0213-4aff-b026-878c5793fb02` | Durable branch execution, screening, synthesis/deepening, reservation, caps, cancellation, staleness, recovery and containment; E1+NSI-4 | r2 — `af997771-8a69-5afe-bf82-cb56a79d2128` |
| E3 | OMP-269 — `a99ac8f0-eb7b-4607-9652-04f709ab52a2` | Neutral exact-answer offers/selection, CLI/generated Web projections, direct answer/provenance/planning/accessibility; E2+NSI-5 | r2 — `c56247d2-4aa5-5600-81c5-7a2c9b9f3436` |
| E4 | OMP-270 — `b00e8c27-107e-479d-a92b-51c71ac13e3b` | Focused qualification, outcome/resource evidence, shadow/canary and N4 proof; E3+NSI-6 plus the local P12 prerequisite | r2 — `a6014b0d-6886-57a5-82ab-405a5e04bd5c` |

Each owner's **revision.description and structured acceptance criteria both carry its applicable accepted A–D scope**. The native store binds execution `original_request` directly from `revision.description`; execute-prompt step 2 derives criteria from that description. AC-only storage, an evidence link or this document alone is insufficient execution traceability. E1 describes generation/schema/frame/profile decisions; E2 describes their execution/count/pool/constraint enforcement; E3 describes synthesis-owned comparison, neutral presentation/provenance and partial/unknown handling; E4 describes the focused controls/coverage/resource comparisons. Preserve prior descriptions and criterion arrays through explicit reviewed successors, and verify actual readback before binding plans. This is a data/contract correction, not a runtime-code fix.

Native blocks edges are OMP-267→268→269→270, OMP-266→270, and H0/OMP-203→266 and →267. OMP-266 relates to OMP-267; OMP-249 and OMP-243 relate to OMP-266. All five parent edges to OMP-202 remain. Related orientation is canonically ordered by UUID in the service. Preserve all historical relations; no whole-item OMP-249↔P12 blocker is added because OMP-249 spans both initial and later B1 promotions.

The P12→E4 edge is an additional **local programme sequencing** requirement: X-A uses the deployed bounded P12 baseline. P12 is necessary but insufficient; it discharges none of full NSI-1..6. The original §17 graph remains:

```text
NSI-1 + NSI-2 + NSI-3 -> E1
E1 + NSI-4           -> E2
E2 + NSI-5           -> E3
E3 + NSI-6           -> E4
E3 + OWEB-4          -> local Web client fixtures
E4 + local client    -> manual local canary
E4 proof             -> N4
N4 + OWEB-4 client   -> normal local Explore alternatives UI
N4 + OWEB-7          -> remote Explore alternatives UI
```

N4 is an E4 result, not an E1–E4 entry gate. OWEB-7 gates remote use. Preserve SQL-0→NSI-1..6→FLEET-1 and the conjunctive H/CPK requirements from PROGRAM_DEPENDENCIES. Actual relevant core owners/proof, or an explicit named owner disposition for each dependency, are required before runtime admission. The authorized documentation checkpoint proves none of those gates. Each E owner completes only its own full qualified criteria; E1 documentation alone does not complete E1 or any downstream owner.

## Implementation, contract checks and rollout

E1 must inventory current OMP/WorkService/planned Fleet dispatch, lease, event, budget and reconciliation primitives for reuse/retirement, implementing/extracting one shared generic bounded-model-job substrate. A second exploration-only scheduler is prohibited. Exact future schema/module/table names are bounded E1 decisions; no runtime dependency or scheduler is introduced by this document.

Retain **all mandatory tests in 03B §15 (2054–2166)**. They cover owner impersonation and semantic/hash/readiness isolation; exact-answer/direct-answer races; frozen base manifests and sibling isolation; full synthesis/deepener context; exact ingress/screening/currentness references; injection, DLP and provider-egress containment; reservation/allocation/idempotency/fencing/cancel/unknown-outcome/no automatic resend; hard limits and honest partial behavior; cross-principal/CSRF/auth controls; hostile output; and keyboard/screen-reader/touch/mobile flows.

Add future observable checks for versioned generation projection versus critic option tradeoffs; historical schema compatibility; eligible/ineligible/unavailable structural-slot selection and identity/fallback; unchanged candidate-kind/wildcard semantics; immutable original versus counterfactual references; positive target/cap refusal; disjoint known bins/duplicate subset/unknown counts/shortfall; retained valid pool and no automatic fill usage; and pre/post concept coverage with focused controlled contrasts. These extend the existing corpus, not replace it with static source-wording tests.

If later implementation copies substantive ADHD prompt/frame/schema/source material, 03B §18 requires the copyright/full MIT permission notice, pinned copied-source commit and dependency/SBOM entry. Carry this into E1 qualification. This documentation cut imports no donor implementation/runtime and copies no donor prompt/frame wording.

Rollout remains: explicitly authorized offline corpus/provider-egress/study budget; offer-only shadow with no model call/egress/cost; manual explicit local E4 canary with client prerequisites; contextual owner-confirmed local runs after N4; private remote only after N4+OWEB-7. Automatic usage requires a later owner-ratified programme and remains prohibited here. Failure to prove value leaves exploration manual or removable; base intake and Fleet eligibility do not depend on N4.

P12 follows initial qualified OMP-249/P11 promotion. B0 is a separately qualified fixed-model baseline; B1 has its own exact intended source diff, deterministic/native qualification and matched comparison, then its own separately scheduled promotion before final live observation. Routing and upstream integration remain separate candidates/cohorts. OMP-250 does not own trusted-runner issuance or paid trials; Harbor OMP-252 does not own NSI-E4. All metered trials require actual finite budget/egress authority; code approval does not authorize a study.

## Deliberate guide binding and P8/P11

The following paragraph is identical to the selected native OMP-267 and OMP-202 description text:

Guide backlink decision (coordinator-adopted option 2, explicitly requested by the owner): editing docs/omp-stabilization-plan.md changes its current SHA-256 601cb5e92121b5efd21e2ebc0e467142817ca54d33b7a60b1e08af264255d388. OMP-249 r4 73ffb84b-30a1-59e5-aa80-05332e0cf368 planned candidate 3ecf700c-6215-5b18-8426-bd3abd446ab0 and plan receipt 91fc6c15-b7c6-5ce2-8c67-696ff78e5717 retain their existing WorkService service identity/state unchanged now. Their old plan_file/body/hash binding cannot qualify the changed guide/source: that binding is stale by design on the changed docs candidate, not retroactively valid. Before OMP-249 acceptance, P8 or P11, deliberately supersede/re-stamp under a separately admitted exact plan/revision/candidate and new guide hash. No OMP-249 state mutation, receipt fabrication, acceptance or live effect is performed by this filing.

Original guide bytes remain in Git at `b94219d559e2ca2e7a415dc90d04ab29c5b9a1a6:docs/omp-stabilization-plan.md`, the immutable original plan receipt body and retained provenance. Current identity/state preservation is not continuing qualification of changed source. Future re-stamping uses deliberately admitted supersession; it cannot silently preserve the old current-candidate binding.

P8 is an **OPEN authority-interface decision**. The inspected supported WorkService command union has no adequate general deployment authorization payload binding principal, exact command paths/hashes, targets/payload, window and effect bounds; migration `activate_cutover` is not one. No owner-personal per-effect route is presumed. Before a live authorization question, prepare concrete supported alternatives or a minimal reviewed contract/interface change and exact payload/verification/rollback evidence. An explicit owner interface decision, real recorded authority IDs, current-revision and manifest readback must precede P11. Local artifacts and plan/evidence receipts are not effect authority.

Keep owner-personal interactive contract approval, promoter-principal boundaries, backup/restore, quiescence, compatibility and rollback requirements. A source-directory rollback is not a database down-migration. No live installation/settings/authority change follows from this addendum; original and authoritative histories remain intact.

## Current repository checkpoint and verification

The bounded E1 documentation checkpoint changes exactly this addendum, one backlink in [the stabilization guide](omp-stabilization-plan.md), and their two rows in [the existing fork inventory](upstream-fork-inventory.tsv). It uses existing [upstream governance](upstream-guardrail.md); no new tracker, parser/schema migration or baseline acceptance record is introduced. Record actual E1 ownership, concrete retained behavior/reason, source/base/target and exact checks in existing human inventory fields. Preserve unrelated rows and all accepted historical records.

The source-declared accepted upstream baseline stays 18.0.6: base `ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472`, historical fork `79b037e420943010e03727d7cdb22f05e64507b7`, target `b4e8e856ad40294167679a3f88417c07429fe59b`. Separate P10c target `86bf72f52947f62ecaf9bd28e35572812e725a92` is unchanged. Verify inventory before any refresh; report unrelated drift rather than absorb it. Preserve the P1 candidate's inventory changes on the fresh-main integration base.

Exact local checks are `git diff --check`, `bun test scripts/upstream-inventory.test.ts scripts/verify-upstream-handoff.test.ts`, `bun scripts/upstream-inventory.ts`, and the final checker against the frozen candidate tree/commit using `--head`. Verify relative links, native-key/revision/description/AC mappings, identical guide-binding paragraph, archive hashes, stage graph and three-file boundary. These are document/governance checks; they do not implement or qualify the future runtime corpus.

After actual description-successor readback and coordinator admission, write final repository bytes, then finalize the finite absolute-path E1 plan with current E1 revision and final addendum SHA256. Include all current E1 criteria and enforce the ≤32 KiB body-plus-criteria limit; stamp exact final plan bytes through the supported native route. A draft or placeholder-bearing plan cannot be stamped. Keep plan-stamp identity outside this document to avoid a self-referential hash cycle.

Capture raw diff/new file, exact base/tree/commit identities, byte hashes and command outputs. Fresh independent Fable source audit precedes publication; required hosted CI and applicable native acceptance remain separate. P1/current-main qualification is a prerequisite, not a docs exception. A later rebase changes the candidate and requires fresh inventory/check/review evidence. No commit, publication, acceptance, NOW movement, grant revival or closeout is implied by document completion. Leave all unfulfilled runtime criteria open.
