# Autoresearch M3 Acceptance Record: Self-Improving Research

- **Milestone**: Autoresearch M3. Generated search mechanisms and broader harness candidates receive empirical qualification.
- **Required Packages**: R13 and R14
- **Task**: `OMP-317` (Slice: `OMP-317-s06`)
- **Promised Outcome**: Generated search mechanisms and broader harness candidates receive empirical qualification.
- **Status**: source delivered, not installed-qualified

Waiting for Chris: OMP-317-s07 (child filing and blocking edges)

---

## 1. Package Deliveries and Evidence

### R13 — Mechanism research lab

- **Commits**:
  - `56eac0bb4c`: `OMP-317-s01: flood auto-commit (attempt 1)`
  - `635699db12`: `OMP-317-s02: add the isolated strategy protocol for code and config candidates`
  - `ec40ed8597`: `OMP-317-s03: R13 mechanism candidate lifecycle, development comparison, fresh-cohort confirmation`
  - `OMP-317-s06` (current slice)
- **What it delivered**:
  - `python/omp-work/src/omp_work/research/mechanism.py` classifies traces, detects `plateau:greedy`, drafts a hypothesis, and `generation_job` emits a `propose` strategy for `min_metric` 0.85.
  - `python/omp-work/src/omp_work/research/strategy_protocol.py` loads that strategy in a restricted interpreter and rejects broken code, over-cap resources, a wrong interface, and evaluator tampering without mutating the caller.
  - `python/omp-work/src/omp_work/research/mechanism_eval.py` advances a candidate to `development_compared`, scores it against greedy with research and comparison costs included, and confirms it on a fresh cohort.
  - `python/omp-work/src/omp_work/research/mechanism_lab.py` `run_lab` chains those three stages and writes `lab.json`.
- **Proving test files**:
  - `python/omp-work/tests/test_research_mechanism.py`
  - `python/omp-work/tests/test_research_strategy_protocol.py`
  - `python/omp-work/tests/test_research_mechanism_eval.py`
  - `python/omp-work/tests/test_research_m3_lab.py`

### R14 — Harness promotion

- **Commits**:
  - `7a97be88a0`: `OMP-317-s04: package harness releases with independent acceptance gates`
  - `377854fe36`: `OMP-317-s05: add R14 canary, schema-aware rollback, and evaluator-change promotion decisions`
  - `OMP-317-s06` (current slice)
- **What it delivered**:
  - `python/omp-work/src/omp_work/research/harness_release.py` packages canonical bytes, binds the evaluated bundle digest, and records code review, audit, CI, and native acceptance as separate gates. `propose_release` does not apply live activation.
  - `python/omp-work/src/omp_work/research/promotion.py` decides canary verdicts, refuses a rollback the target schema cannot read, and refuses a mixed harness-plus-evaluator change.
  - `run_promotion` writes `promotion.json` for the confirmed source, the stable greedy run, the canary, the rollback plan, and the evaluator change.
- **Proving test files**:
  - `python/omp-work/tests/test_research_harness_release.py`
  - `python/omp-work/tests/test_research_promotion.py`
  - `python/omp-work/tests/test_research_m3_lab.py`

---

## 2. Outcome Clause to Evidence Mapping

The promised outcome is: Generated search mechanisms and broader harness candidates receive empirical qualification.

| Outcome clause | Repository evidence | Proving test |
| --- | --- | --- |
| Generated mechanism | `run_lab` records pattern `plateau:greedy`, generates `adaptive_restart` at `min_metric` 0.85, and reaches `confirmed`. On dev-1 the candidate opens A1–A4 (winner 0.95) and greedy opens A1–A2 (winner 0.9). The development comparison cost is research 2.0, comparison 1.0, candidate trials 4, baseline trials 2, total 9.0. The fresh cohort conf-1 and conf-2 wins 2 of 2; the same unit costs make that cohort total 18.0. | `python/omp-work/tests/test_research_m3_lab.py` |
| Rejection without corruption | Broken source `def propose(` yields `broken_code`. Module `RESOURCES` `cpu_seconds` 1000 against cap 120 yields `resource_exceeded`. A missing width default yields `wrong_interface`. A call named `verify_receipt` yields `evaluator_tampering`. After each run, and after the crash that falls back to greedy's pick A2, `canonical_json` of the steps and the opened set is unchanged. | `python/omp-work/tests/test_research_m3_lab.py` |
| Qualified release | The candidate bundle is `research/strategies/adaptive_restart.py` plus `research/config/lab.json`. `evaluated_bundle_sha256` equals `bundle_sha256`. Gates code_review (reviewer), audit (auditor), ci (ci), and native_acceptance (owner) are distinct. Status is `proposed_release`. live_activation by actor lab is refused with `activation_requires_owner` and is not recorded. native_acceptance by actor lab on a core-authority bundle is refused with `core_authority_requires_owner`. | `python/omp-work/tests/test_research_m3_lab.py` |
| Stable installation | While `lab.json` is present, `search.run_policy("greedy", fixture, budget=3)` returns [A1, A2] with winner A1 metric 0.9, and the fixture tree is unchanged. | `python/omp-work/tests/test_research_m3_lab.py` |
| Canary, rollback, evaluator | One candidate observation below its baseline yields canary verdict `rollback`. `plan_rollback` from candidate-1 to stable-1 with persisted schemas (1, 2) restores stable-1's bundle. A target whose `schema_read` is (1,) raises `rollback_incompatible:2`. The evaluator move to `"33"*32` / `"44"*32` with reason `recalibrated scorer` invalidates the earlier confirmation with `changed_evaluator`. A manifest that changes both the bundle and that evaluator is refused with `mixed_change`. | `python/omp-work/tests/test_research_m3_lab.py` |

---

## 3. Test Verification

Command, from the repository root:

```bash
uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_m3_lab.py
```

- **Result**: `6 passed in 0.25s`
- This record states source delivery only. The status is source delivered, not installed-qualified. Do not claim an installed qualification.

## Assumptions

- Fixture tree per task: X1 metric 0.9 with child X2 metric 0.1; X3 metric 0.8 with child X4 metric 0.95; every cost 1; budget 4; width 1. dev-1 uses A1–A4, conf-1 uses C1–C4, conf-2 uses D1–D4. `generation_job` with `min_metric` 0.85 opens all four nodes and the winner metric is 0.95. `greedy` opens the first two and the winner metric is 0.9. One development task records research 2.0, comparison 1.0, and total 9.0. Two fresh tasks at the same unit costs record total 18.0 and wins 2.
- The stable install calls `search.run_policy("greedy", fixture, budget=3)` and still opens [A1, A2].
- Evaluator identity on the candidate and the stable manifest is `"11"*32` / `"22"*32`. Versions are policy `adaptive-restart.v1`, model `m1`, skill `s1`. `schema_read` is (1, 2) and `schema_write` is 2. The stable release id is `stable-1` with its own bundle digest.
- Map: R13 trace/jobs s01, R13 strategy protocol s02, R13 candidate evaluation s03, R14 packaging/manifest s04, R14 canary/rollback/evaluator-change s05, lab and promotion proof plus this record s06, filing s07. Waiting for Chris: s07 only (child filing and blocking edges). No slice depends on s07. No contract change, so no approval slice.
- No package.json edit. These tests do not need Postgres. Use tmp_path. Leave no extra repo file. New files have no master.json rows.

## Deferred

The following items are deferred from s01:

- LLM-driven exploration, critique, and generation
- a mechanism job queue
- microVM strategy isolation (the R05 jail contract is the production boundary)
- promotion beyond `confirmed` into the live controller
- MCTS, bandits, and Bayesian mechanisms
- recursive L3 studies
- declared-interaction mixed releases
- autonomous live activation and the unresolved native deployment-authority interface (owner route, plan section 12)
- WebUI harness views
- /research commands
- contract edits
- changelog entries
