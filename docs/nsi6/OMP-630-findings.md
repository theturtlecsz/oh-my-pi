# OMP-630: why the historical shadow metrics match

The stored 2026-10-15 `docs/nsi6/shadow.json` is a version-1 **labeled replay**,
not a model-backed pipeline comparison. Both arms report false-ready **13/19**,
criterion coverage **72/92**, and critical detection **72/92**. Provenance has
only **1/4** valid proofs: `ready-current-proof` succeeds; `stale-source` and
`tampered-provenance` are refused; `stale-revision` has a historical ready
assessment but explicitly invalid freshness. These are historical control
results, not rerun results.

## Shared supplied draft and bounded rules

In `python/omp-work/src/omp_work/nsi_eval.py`, `replay()` passes the same
`evaluator_input(case)` to each arm through `observe()`, which deep-copies input.
`evaluate_current()` validates `raw["bounded"]["draft"]` and runs
`evaluate_bounded_intake()`. `evaluate_typed()` validates that same draft, then
calls `bounded_to_nsi()`, `nsi_bounded_evaluation()` and `intake_contract()` from
`python/omp-work/src/omp_work/contracts/nsi/v1/compat.py`.

Compatibility is intentionally bounded: `_issues_from_claims()` mirrors the
bounded rules for contradictory constraints, missing verification oracles, and
material authority/dependency unknowns. `verify_bounded_evaluation_parity()`
checks that compatibility output matches the existing bounded evaluator,
including the two-question cap. This is not independent extraction of missing
requirements from source text. `_current_findings()` recovers complete bounded
findings beyond that cap; typed evaluation already retains complete gaps.

The raw records differ: current stores issue count and bounded questions; typed
stores snapshot, projected contract, gaps, conflicts and typed questions.
`score()` does not compare those representations. It scores projected readiness,
criterion IDs, finding targets, question target counts and semantic draft edits.
Those projections match on this corpus even though their raw records differ.
Both arms use the supplied draft for semantic edits. Invalid draft validation
also occurs before the arms diverge. The new offline test recomputes both
stored score projections and verifies equality apart from elapsed time.

## Missing pre-scoring proof composition

`replay()` calls `score()` on each bounded observation **before** it evaluates
provenance, and stores proof outcomes separately. Thus provenance refusal or
staleness cannot change either arm's scored readiness. In particular,
`stale-source`, `stale-revision` and `tampered-provenance` remain falsely ready
in both historical arms. `nsi_eval_postgres.evaluate_fixture()` exposes the
refusals and revision/binding validity, but replay never composes them with
arm readiness.

`observe()` forces `model_tokens=0` and `execution="labeled replay"` even if an
evaluator returns other accounting. `aggregate()` also forces zero model
tokens; provenance totals do likewise. These counters establish no model
comparison, tokens consumed, cost, or budget compliance.

## Shared projection and separate future evidence

`omp_work.nsi6_observation.project_observation()` now composes an actual arm
assessment with required proof **before** the unchanged `score()`. Invalid,
refused, stale or mismatched proof cannot leave the pipeline ready. Failed
components retain failure status; crashes and skips get no safety evidence or
coverage, while all gold denominators remain. No proof scenario adds no proof
obligation. Raw assessment, proof and diagnostics remain available, and token
accounting is retained rather than zeroed.

`python/omp-work/tests/test_nsi6_readiness_projection.py` reproduces the three
historical false-ready paths from stored proof evidence and real offline arm
evaluators. Its corrected readiness assertion fails if the old replay
observation is substituted for the projection. It also checks valid-proof
eligibility, binding/freshness rejection and failure denominators. This verifies
composition offline; it does not claim new model-backed results.

Keep version-1 replay and its tests as historical control. Authoritative
model-backed evidence belongs under `shadow.json.pipeline_comparison`, with
separate verification of real per-arm calls, tokens, cost and a stated finite
USD cap. Credentialed execution and final sealing belong to s04/s07; this task
does not alter or re-seal `shadow.json`, change scoring formulas, or request
promotion.
