# Completed WorkService mutation: OMP-268 description correction

Status: **COMPLETED**. The description-only successor revision was applied and its readback verified by OMP-268-s02; this worktree still has no WorkService credentials, so the evidence below is transcribed from the s02 artifact rather than re-queried.

- Work UUID: `4daad6ca-0213-4aff-b026-878c5793fb02`
- Prior revision (history): r2, UUID `af997771-8a69-5afe-bf82-cb56a79d2128` — superseded, not deleted; retained as the previous revision of record.
- Applied revision: **r3**, UUID `84cc75f8-6f9f-4bcc-9683-cf61b917073b`
- Mutated field: `revision.description` only
- Applied description: 6,440 UTF-8 bytes (6,434 characters), SHA-256 `83632d9bc1ada168133bf04b301202c30fd2ecb2bccd346050fe5d23df72a3c5` — exact-readback **PASS** against the target description below.
- Structured acceptance criteria are unchanged: the same five bullets reproduced verbatim under `## Acceptance criteria` in the applied description. No acceptance criterion is claimed complete by this filing.

## Mutation contract (as executed)

- Passed explicit `expected_revision_id="af997771-8a69-5afe-bf82-cb56a79d2128"` (r2); the mutation would have refused had the current native revision drifted.
- Created a description-only successor revision (r3, `84cc75f8-6f9f-4bcc-9683-cf61b917073b`).
- Preserved title, scope, criteria array, state (`BACKLOG`), relations, candidate (`null`), NOW, focus, grants, and effects — see "Readback checklist" below for the substantiated per-field comparison.
- Made no runtime implementation or qualification claim.

## Readback evidence (from OMP-268-s02)

Source: `flood-logs/OMP-268-s02-evidence.json`, `revise_work` result `revision_id=84cc75f8-6f9f-4bcc-9683-cf61b917073b`, `changed=true`; receipt `operation_id=22132273-a43b-403a-afd1-bf1cd2d173c1`, `request_id=56f843aa-27a7-4f7c-92bc-dbacfef5110e`, `state=applied`. Recorded verdict: **PASS**, with every individual check `true`:

- `rev3` — the successor's `revision_number` is `3`.
- `desc_sha` — the applied `revision.description` hashes to `83632d9bc1ada168133bf04b301202c30fd2ecb2bccd346050fe5d23df72a3c5`, matching the target byte-for-byte.
- `title/scope/criteria` — unchanged from r2 (`before`) to r3 (`after`).
- `state` — unchanged, `BACKLOG`.
- `candidate` — unchanged, `null`.
- `item_rest` — remaining work-item fields (workspace, alias, project) unchanged.
- `tree_rest(relations/NOW/focus/grants/effects)` — unchanged.

This substantiates that title, scope, criteria, state, candidate, relations, NOW, focus, grants, and effects were all preserved across the r2→r3 correction. No runtime, NSI-4, qualification, receipt-fabrication, or activation claim is made by this filing or by the applied description.

## Exact target `revision.description`

<!-- BEGIN_TARGET_DESCRIPTION -->
# 03B E2: durable exploration execution, screening, budget and recovery

NSI-E2 is a separate owner for durable execution, deterministic screening, budget enforcement and recovery. Native E1→E2 blocks edge is necessary; qualified NSI-4 remains an additional core gate, and P12 does not discharge it. This prospective BACKLOG filing does not admit runtime implementation. Missing NSI-4 ownership must be resolved at the relevant admission without invented qualification. Preserve the shared bounded-model-job substrate, immutable inputs, branch isolation, retained valid pool, exact reservations and native authority boundaries.

## Acceptance criteria

- Runtime admission proves qualified E1 plus NSI-4 and retained H0 floor under exact separately admitted paths; missing core owner/disposition refuses admission, and P12 is not substituted for NSI-4.
- Durable execution uses the qualified shared bounded-model-job substrate, isolated branch attempts and immutable original inputs; synthesis receives the entire valid screened pool with complete provenance and no generator sibling visibility.
- Deterministic schema/reference/explicit-constraint and exact-duplicate screening preserves valid proposals/warnings; comparative model labels neither delete candidates nor confer readiness.
- Compiled feasibility and accepted hard caps bound branches, candidates, calls/tokens/cost and retries; persist per-branch targets, returned/valid/duplicate counts and shortfalls without padding or automatic spend.
- Real supported failure/cancellation/restart/replay journeys preserve successful branch artifacts and reservation/effect identity; terminal or superseded work cannot revive, partial/failure states remain explicit, and no fabricated receipt is used as qualification.

## Accepted 03B scope — description contract v2 (2026-09-09)

This versioned successor makes the already accepted, owner-applicable behavior explicit in revision.description, which native execution binds as original_request. The acceptance-criteria bullets above are verbatim copies of the unchanged structured array. The following A–D clauses explain their role-specific applicability; they do not admit runtime implementation, alter dependencies or claim any completed criterion.

### A. Generation and comparative judgment

E2 executes the qualified E1 generation/synthesis contract: isolated generators describe candidates, mechanisms, named assumptions and references without sibling comparison, ranking or pruning. Noncomparative consequence/risk assertions and risk-oriented ideation remain legal. Required comparative judgment occurs in the existing later synthesis/critic call supplied with the entire valid screened pool; add no critic call or budget partition. Preserve deterministic schema/reference/explicit-constraint/exact-duplicate screening and valid proposals/warnings; model labels cannot delete candidates or confer readiness.

### B. Reviewed structural slot and immutable inputs

E2 enforces the admitted profile's one existing structural branch slot only when eligible, using reviewed inversion, assumption-challenge or wildcard-family cross-domain frame/operator identities. Persist the deterministic eligibility/selection/fallback record; do not invent frames, add branches or widen budget. Keep original source and constraints immutable and keep labeled counterfactual assumption relaxations distinct from the actual input throughout durable execution and recovery.

### C. Bounded breadth and raw-pool preservation

Persist the positive targetCandidatesPerBranch and ingress returnedCount, invalidCount, quarantinedCount, validCount and duplicateCount under the qualified E1 profile. Enforce returnedCount=invalidCount+quarantinedCount+validCount, duplicateCount as a valid subset, distinctValidCount=validCount-duplicateCount and shortfall=max(0,targetCandidatesPerBranch-distinctValidCount). Unknown values remain unknown rather than zero. Preserve successful raw valid branch artifacts across partial failure and recovery; do not pad, silently delete near-semantic variants, retry automatically or spend more. A 4x4 configuration is only a separately authorized pilot inside compiled feasibility and accepted branch/candidate/call/token/cost caps.

### D. Focused qualification and concept coverage

E2 preserves the branch/profile/frame/input identities, raw valid pre-synthesis pool, synthesis references, realized resource use and count/shortfall evidence needed by E4's separate posture/frame/breadth and pre/post concept-coverage contrasts. E4 owns the framed/unframed/equal-compute experiment and its independent rubric. E2 runtime qualification does not itself authorize those trials, a larger mandatory evaluation/refinement loop, changes to fixed B0/B1 cohorts, or outcome-benefit claims.

Historical descriptions, structured criteria, native dependency edges and authority boundaries remain. This correction changes only this description under an expected-revision successor; no new keys, runtime/source implementation, paid/live/canary effect, NOW/focus/grant change, plan stamp, native audit/acceptance, closure or relation removal occurs.

Scope: E2 durable bounded exploration runtime only, after qualified E1 and NSI-4; no E3 UI, paid evaluation or live activation.

Acceptance:
- [ ] Runtime admission proves qualified E1 plus NSI-4 and retained H0 floor under exact separately admitted paths; missing core owner/disposition refuses admission, and P12 is not substituted for NSI-4.
- [ ] Durable execution uses the qualified shared bounded-model-job substrate, isolated branch attempts and immutable original inputs; synthesis receives the entire valid screened pool with complete provenance and no generator sibling visibility.
- [ ] Deterministic schema/reference/explicit-constraint and exact-duplicate screening preserves valid proposals/warnings; comparative model labels neither delete candidates nor confer readiness.
- [ ] Compiled feasibility and accepted hard caps bound branches, candidates, calls/tokens/cost and retries; persist per-branch targets, returned/valid/duplicate counts and shortfalls without padding or automatic spend.
- [ ] Real supported failure/cancellation/restart/replay journeys preserve successful branch artifacts and reservation/effect identity; terminal or superseded work cannot revive, partial/failure states remain explicit, and no fabricated receipt is used as qualification.
<!-- END_TARGET_DESCRIPTION -->

## Readback checklist

- [x] Successor revision UUID recorded: `84cc75f8-6f9f-4bcc-9683-cf61b917073b`.
- [x] `revision_number` is `3` (r3).
- [x] Readback description SHA-256 is `83632d9bc1ada168133bf04b301202c30fd2ecb2bccd346050fe5d23df72a3c5`.
- [x] Criteria, title, scope, state (`BACKLOG`), candidate (`null`), and relations are unchanged.
- [x] NOW, focus, grants, and effects are unchanged.

All five checks are substantiated directly from the OMP-268-s02 evidence artifact's `checks` block (`rev3`, `desc_sha`, `title/scope/criteria`, `state`, `candidate`, `item_rest`, `tree_rest(relations/NOW/focus/grants/effects)`, all `true`) and its recorded `verdict: PASS`. r2 (`af997771-8a69-5afe-bf82-cb56a79d2128`) remains the preserved prior revision, superseded by r3.
