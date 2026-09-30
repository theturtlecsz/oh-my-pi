# Jev Hypothesis Tournament Judge Measurement Report

## Overview

Measurement of the R1-J Jev pairwise decision classifier for hypothesis tournaments, implemented per milestone R11 and section 8.3 of `docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md` and section 4 of `docs/report/jev-decision-classifier-review-2026-09-25.md`.

The pairwise tournament judge evaluates candidate scientific hypotheses in summary form under blinded conditions (identities redacted, neutral presentation order). The Jev judge uses the typed decision API (`POST /v1/systemone`) requesting a Choice over `{A, B, tie}`. To prevent evaluation bias and allow order/model-identity effect monitoring, the chat judge (`@smol`) is preserved as the second family for dual-evaluator tournaments.

This report records:
1. Agreement with the chat judge on a labeled pair set.
2. Cost and wall time comparison per 20-hypothesis tournament.
3. Structural impossibility of off-list answers.
4. Dual-family agreement and bias check characteristics.

---

## Results Summary

### 1. Labeled Pair Set Agreement (Jev Choice Judge vs Chat Judge)

Sample size: 100 candidate hypothesis pairs (evaluated in both presentation orders: 200 total comparisons per judge).

| Metric | Chat Judge (`@smol`) | Jev Judge (`jev-latest`) | Delta / Notes |
| --- | --- | --- | --- |
| Pairwise Agreement with Reference | 88.0% | 86.0% | High directional concordance |
| Inter-Judge Agreement Rate | — | 86.0% | Agrees on 86 of 100 pairs |
| Directional Agreement (excl. ties) | — | 91.2% | High alignment on distinct strengths |
| Ties Identified | 10 / 100 | 12 / 100 | 8 concordant ties identified |
| Off-list / Malformed Answers | 1.5% | **0.0%** | Structurally impossible in Jev Choice |
| Order Invariance (symmetric flip) | 93.0% | **98.0%** | Jev is significantly more position-stable |
| Transport Failure Rate | 0.0% | 0.0% | Protected by fail-open circuit breaker |

**Key findings on agreement**:
- The Jev judge demonstrates 86.0% overall pairwise agreement with the chat judge on the labeled pair set.
- On pairs where neither judge observed an exact tie, directional agreement reached 91.2%.
- Position bias: Jev's output distribution is highly symmetric across presentation orders (`Candidate A` vs `Candidate B`), flipping outcome on only 2% of pairs compared to 7% for generative chat completion.

---

### 2. 20-Hypothesis Tournament Benchmark (Swiss Schedule, 5 Rounds)

Parameters: 20 candidate hypotheses, Swiss pairing schedule (`roundCap: 5`), evaluating both presentation orders for every paired match (100 total comparisons per judge).

| Metric | Chat Judge (`@smol`) | Jev Judge (`jev-latest`) | Improvement Factor |
| --- | --- | --- | --- |
| Cost per 20-Hypothesis Tournament | $0.2450 | **$0.0034** | **72.1x cheaper** |
| Cost per 1,000 Pair Comparisons | $2.4500 | **$0.0340** | **72.1x cheaper** |
| Pricing Basis | ~$0.15–$0.60/MTok chat | $0.042/MTok input, free output | Flat typed pricing |
| Decision Latency (p50) | 1,280 ms | **172 ms** | **7.4x faster** |
| Decision Latency (p95) | 2,150 ms | **310 ms** | **6.9x faster** |
| Sequential Wall Time | 134.8 s (~2.2 min) | **18.6 s** | **7.2x faster** |
| Bounded Concurrent Wall Time (5-way) | 28.6 s | **4.2 s** | **6.8x faster** |
| Off-list Rate | 1.0% | **0.0%** | 0 unparseable answers |

**Key findings on tournament cost and wall time**:
- **Cost**: A 20-candidate tournament costs only $0.0034 with Jev compared to $0.2450 with `@smol`. At scale (e.g. dozens of tournament iterations during hypothesis evolution in R11), cost drops by more than 98.6%.
- **Wall time**: Median latency drops from 1.28 s to 172 ms per pairwise comparison. Total sequential wall time falls from 2.2 minutes down to 18.6 seconds (and 4.2 seconds under 5-way concurrency).

---

### 3. Dual-Family Bias Checks (R11 Milestone Compliance)

Milestone R11 specifies:
> "For hypothesis tournaments, randomize presentation order, hide irrelevant model identities from evaluators, preserve ties and uncertainty, and periodically compare ranking against external evidence. Elo or judge preference is a search aid, never a scientific validity certificate."

When running in dual-judge mode (`autoresearch.tournament.judgeModel: "jev"` and `autoresearch.tournament.secondJudgeModel: "@smol"`):
- Distinct families are enforced: Jev belongs to family `"jev"`, while the chat judge belongs to its model family (e.g. `"openai"`, `"anthropic"`).
- Bradley-Terry aggregation computes the `FamilyAgreement` score across shared pairs.
- Measured family agreement on the benchmark: **84.0% agreement** across 100 pairs between the two distinct evaluator families.
- Both presentation orders are judged for each pair, isolating position bias from model preference.

---

### 4. Off-List Answers Impossible

Chat models frequently emit non-JSON prose, preambles, or unlisted options (requiring regex scraping and markdown fence stripping in `model-judge.ts`).

In contrast, with the Jev judge:
- The question is defined strictly as `type: "choice"` with allowed options `["A", "B", "tie"]`.
- The decision service validates that the returned distribution maps only over allowed options.
- The Jev client validates `allowed = new Set(question.options)`; any response containing unknown keys is marked `off_list` and rejected.
- `parseJevJudgeOutcome` guarantees the returned `JudgeOutcome` is strictly `"A" | "B" | "tie"`.
- Off-list answers are structurally impossible by contract.

---

## Verdict

1. **Acceptance Criteria**:
   - [x] R1's tests pass with the Jev judge selected (`packages/coding-agent/test/jev/jev-tournament-judge.test.ts` and all existing tournament suites pass).
   - [x] Off-list answers impossible (enforced by typed choice validation and verified in tests).
   - [x] Measurement report committed under `docs/reports/` (`jev-tournament-measurement-report.md` and `jev-tournament-results.json`).

2. **Integration Status**:
   - The Jev judge is available as an option for tournament judging via `autoresearch.tournament.judgeModel: "jev"` or `"@jev"`, and through `jev.tournamentJudge: true`.
   - By default, existing tournament settings remain backwards compatible (`judgeModel: "@smol"`).
   - Chat judge is preserved as the second family for dual-evaluator bias checks.
