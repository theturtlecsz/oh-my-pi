# OMP-605: adaptive versus fixed, host evidence

Measured 2026-10-15. **Null result**: paired best compression-ratio effect (greedy minus fixed) is 0; 95% Student-t interval [0, 0], five paired seeds. No quality improvement demonstrated. Adaptive remains off by default: greedy requires explicit `search.policy`. Study completed on a valid comparison; no win required.

Reproduce from checkout root:

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 OMP_605_REPORT=docs/reports/omp-605-adaptive-vs-fixed.json uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_adaptive_campaign_real_task.py
```

Result: 2 passed (30.73 seconds); one existing Starlette/httpx deprecation warning. Test launches actual `python -m omp_work research compare --key <local-item> --repo-root <checkout> --output <json>` with fixture-generated client-config and all four DB flags. Disposable local PostgreSQL, capabilities, and HTTP server are fixture-owned. No owner, external service, or model step. Production Python unchanged. This slice serves amended OMP-605 r3 AC3 (real-task comparison and costs) and AC6 (honest null decision with owner options).

[Machine evidence](omp-605-adaptive-vs-fixed.json) retains CLI comparison fields, corpus hashes, campaign/trial IDs, decisions and their trusted-observation digests, plus checked evaluator receipts and selection replay measurements. Test verifies CLI stdout equals its JSON before adding verification evidence. A separate check rebuilds the corpus from this checkout and compares it with the committed report, detecting stale evidence after source changes or rebases. Regenerate JSON and refresh this Markdown after any corpus drift; campaign IDs and timings are run-specific.

## Corpus and design

Task: maximize raw/compressed byte ratio using actual Python/zlib harnesses over production `python/omp-work/src/omp_work/**/*.py`. Each seed deterministically samples 40 source files; all path/SHA-256 pairs and aggregate corpus hashes are in JSON. No fixture scores or prefilled future metrics. Seven identical unit-cost candidates per arm: roots a/b/c (levels 1/6/9), a1/a2 below a, a1x/a1y below a1, with default/filtered strategy and memory variants. Seeds 0–4 determine corpus samples, not independent tasks.

| Seed | Corpus SHA-256 |
| --- | --- |
| 0 | `b7dfc6942be51f85731afdf14b8f4a4d9da9864ee34df49315ee14f986867aac` |
| 1 | `be3872e1001c77bf782ef77859fb1caabbf1455f9cc51a846c585b4f091bbbcd` |
| 2 | `6f09a96c094559c23a9df53aa1d2e68c02584e480a6711862fbf768914b2091e` |
| 3 | `4a7a40b4c031a810db47002de91490db4544c28dfc774b35056b30a0dc107ea1` |
| 4 | `dc90290741d33444694bf881fae882508f45b61ff0001a31a7b14459e5b27287` |

Both arms have five-trial/five-unit caps per seed. Greedy may stop early when its highest-scoring opened node has no unopened child. Fixed follows pool insertion order. All ten campaigns concluded supported; every trial completed with sealed successful job and verified evaluator receipt bound to its persisted observation and evaluator. Replaying every decision uses only its trusted prefix. No trial count or positive effect is imposed.

## Actual costs and outcomes

| Seed | Greedy trials / units | Fixed trials / units | Greedy best | Fixed best | Effect |
| --- | --- | --- | --- | --- | --- |
| 0 | 3 / 3 | 5 / 5 | 4.373052545492 | 4.373052545492 | 0 |
| 1 | 3 / 3 | 5 / 5 | 4.777292326789 | 4.777292326789 | 0 |
| 2 | 3 / 3 | 5 / 5 | 4.577954643681 | 4.577954643681 | 0 |
| 3 | 3 / 3 | 5 / 5 | 5.027800930442 | 5.027800930442 | 0 |
| 4 | 3 / 3 | 5 / 5 | 4.240014871019 | 4.240014871019 | 0 |

Every greedy sequence: `a → a1 → a1x` (15 total trials/units). Every fixed sequence: `a → b → c → a1 → a2` (25 total trials/units). Greedy consumed 10 fewer unit reservations here, but this does not establish a quality win or general cost advantage. Units are configured compute costs, not measured CPU seconds or dollar prices.

| Arm | Selection model cost (USD) | Helper/evaluator model cost (USD) | Measured policy replay time (seconds) |
| --- | --- | --- | --- |
| greedy | 0 | 0 | 0.000255240 |
| fixed | 0 | 0 | 0.000208192 |

Zero model costs are derived from observed absence of model calls and usage, not separately metered monetary ledger charges: all 40 jobs are compute jobs with zero model-call resources, empty settlement usage, and 0 workspace usage events. Evaluator executes local Python/zlib without a model. Selection is deterministic local policy code. Replay timings measure selection computation on persisted observations, not original CLI selection latency or its DB/HTTP overhead; original selection wall time is not separately instrumented. Total comparison CLI wall time: 26.729 seconds. No monetary compute tariff measured.

## Replay and uncertainty

Seed 0, decision 2: persisted child `a1` ratio 4.339514689174 exceeds parent `a` ratio 3.494047152304; replay selects `a1x`. Changing only that observed child metric to 2.494047152304, below its parent, changes next selection to `a2`. This is a counterfactual replay of actual receipt-derived observations, not a rerun or fabricated result in comparison arms.

Paired differences: [0, 0, 0, 0, 0]. Mean 0; sample standard deviation 0; 95% interval `mean ± 2.776445105 × sample_stdev / sqrt(5)` = [0, 0]. Null status true. Degenerate interval reflects these paired equalities, not certainty about other tasks. Five seeds on one task support a local decision only; they do not qualify the programme (20 consecutive accepted trials; 72-hour run with at least 30 items across both repositories; at least 100 actual experiment attempts).

## Decision options for Chris

Evidence supports no useful quality benefit on this corpus family. Keep adaptive off by default. Options:

- **Revise:** preregister a revised stopping policy and evaluate on an additional independent production task with five paired seeds. Next useful comparison costs at most 50 unit reservations (10 campaigns × five-unit caps); this run consumed 40. A single paired-seed pilot costs at most 10 units, but cannot establish cross-task benefit. Selection/helper-model costs derived from observed zero calls/usage here were $0; the same local evaluator needs no paid model. New task preparation and compute dollar price remain unmeasured; no paid trial authorized.
- **Defer:** retain opt-in capability, spend zero further trials now, and wait for an independent task where adaptive branching matters. Evidence remains this null comparison; next useful comparison has the same ≤50-unit reservation bound.
- **Retire:** stop further evaluation and remove opt-in capability in separately scoped work. Further experiment cost zero; removal effort unmeasured. Current evidence shows equal quality and lower local trial use, with no programme qualification or general benefit claim.
