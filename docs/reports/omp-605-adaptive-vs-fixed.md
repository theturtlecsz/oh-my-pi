# OMP-605: adaptive versus fixed, host evidence

Measured 2026-10-15. **Null result**: paired best compression-ratio effect (greedy minus fixed) is 0; 95% Student-t interval [0, 0], five paired seeds. No quality improvement demonstrated. Adaptive remains off by default: greedy requires explicit `search.policy`. Study completed on a valid comparison; no win required.

Reproduce from checkout root:

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 OMP_605_REPORT=docs/reports/omp-605-adaptive-vs-fixed.json uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_adaptive_campaign_real_task.py
```

Result: 1 passed (30.78 seconds); one existing Starlette/httpx deprecation warning. Test launches actual `python -m omp_work research compare --key <local-item> --repo-root <checkout> --output <json>` with fixture-generated client-config and all four DB flags. Disposable local PostgreSQL, capabilities, and HTTP server are fixture-owned. No owner, external service, or model step. Production Python unchanged.

[Machine evidence](omp-605-adaptive-vs-fixed.json) retains CLI comparison fields, corpus hashes, campaign/trial IDs, decisions and their trusted-observation digests, plus checked evaluator receipts and selection replay measurements. Test verifies CLI stdout equals its JSON before adding verification evidence.

## Corpus and design

Task: maximize raw/compressed byte ratio using actual Python/zlib harnesses over production `python/omp-work/src/omp_work/**/*.py`. Each seed deterministically samples 40 source files; all path/SHA-256 pairs and aggregate corpus hashes are in JSON. No fixture scores or prefilled future metrics. Seven identical unit-cost candidates per arm: roots a/b/c (levels 1/6/9), a1/a2 below a, a1x/a1y below a1, with default/filtered strategy and memory variants. Seeds 0–4 determine corpus samples, not independent tasks.

| Seed | Corpus SHA-256 |
| --- | --- |
| 0 | `2c514bc55ac005e5a701f111b4c8160daaea6e96dcbf0386c6e5263599e9feb7` |
| 1 | `5764ff51d6d737a1df53243ae555b943206c16adedb7e4587808b805b6304a49` |
| 2 | `6ff68e5c13e26df6f9fdc888d91ec7a8c04a86adea27b83d138193ef271a2efb` |
| 3 | `54f53f4f146e684554f1f988840d8efec862bdb4101a17474d44093bd3e249d3` |
| 4 | `52030f508d1be52a97f203eb7cb1ca58ece4d3fa4617f7614fbabb9c021e4275` |

Both arms have five-trial/five-unit caps per seed. Greedy may stop early when its highest-scoring opened node has no unopened child. Fixed follows pool insertion order. All ten campaigns concluded supported; every trial completed with sealed successful job and verified evaluator receipt bound to its persisted observation and evaluator. Replaying every decision uses only its trusted prefix. No trial count or positive effect is imposed.

## Actual costs and outcomes

| Seed | Greedy trials / units | Fixed trials / units | Greedy best | Fixed best | Effect |
| --- | --- | --- | --- | --- | --- |
| 0 | 3 / 3 | 5 / 5 | 4.242681234825 | 4.242681234825 | 0 |
| 1 | 3 / 3 | 5 / 5 | 5.098487392556 | 5.098487392556 | 0 |
| 2 | 3 / 3 | 5 / 5 | 4.330044456116 | 4.330044456116 | 0 |
| 3 | 3 / 3 | 5 / 5 | 5.084468237496 | 5.084468237496 | 0 |
| 4 | 3 / 3 | 5 / 5 | 4.288459419902 | 4.288459419902 | 0 |

Every greedy sequence: `a → a1 → a1x` (15 total trials/units). Every fixed sequence: `a → b → c → a1 → a2` (25 total trials/units). Greedy consumed 10 fewer unit reservations here, but this does not establish a quality win or general cost advantage. Units are configured compute costs, not measured CPU seconds or dollar prices.

| Arm | Selection model cost (USD) | Helper/evaluator model cost (USD) | Measured policy replay time (seconds) |
| --- | --- | --- | --- |
| greedy | 0 | 0 | 0.000239626 |
| fixed | 0 | 0 | 0.000145849 |

Zero model costs are measured ledger results: all 40 jobs are compute jobs with zero model-call resources, empty settlement usage, and 0 workspace usage events. Evaluator executes local Python/zlib without a model. Selection is deterministic local policy code. Replay timings measure selection computation on persisted observations, not original CLI selection latency or its DB/HTTP overhead; original selection wall time is not separately instrumented. Total comparison CLI wall time: 27.251 seconds. No monetary compute tariff measured.

## Replay and uncertainty

Seed 0, decision 2: persisted child `a1` ratio 4.215811468700 exceeds parent `a` ratio 3.401814705371; replay selects `a1x`. Changing only that observed child metric to 2.401814705371, below its parent, changes next selection to `a2`. This is a counterfactual replay of actual receipt-derived observations, not a rerun or fabricated result in comparison arms.

Paired differences: [0, 0, 0, 0, 0]. Mean 0; sample standard deviation 0; 95% interval `mean ± 2.776445105 × sample_stdev / sqrt(5)` = [0, 0]. Null status true. Degenerate interval reflects these paired equalities, not certainty about other tasks. Five seeds on one task support a local decision only; they do not qualify the programme (20 consecutive accepted trials; 72-hour run with at least 30 items across both repositories; at least 100 actual experiment attempts).

## Decision options for Chris

Evidence supports no useful quality benefit on this corpus family. Keep adaptive off by default. Options:

- **Revise:** preregister a revised stopping policy and evaluate on an additional independent production task with five paired seeds. Next useful comparison costs at most 50 unit reservations (10 campaigns × five-unit caps); this run consumed 40. A single paired-seed pilot costs at most 10 units, but cannot establish cross-task benefit. Selection/helper-model charges measured here were $0; the same local evaluator needs no paid model. New task preparation and compute dollar price remain unmeasured; no paid trial authorized.
- **Defer:** retain opt-in capability, spend zero further trials now, and wait for an independent task where adaptive branching matters. Evidence remains this null comparison; next useful comparison has the same ≤50-unit reservation bound.
- **Retire:** stop further evaluation and remove opt-in capability in separately scoped work. Further experiment cost zero; removal effort unmeasured. Current evidence shows equal quality and lower local trial use, with no programme qualification or general benefit claim.
