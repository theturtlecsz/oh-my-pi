# Jev Router Measurement Report

## Overview

Measurement of the three OMP-298 decisions through OpenRouter's Jev Router (`typesafe/jev-router`) — the only Jev surface OpenRouter exposes — instead of TypeSafe's typed decision API (`POST /v1/systemone`), which answers HTTP 401 to the owner's OpenRouter key.

The router is a chat-completions endpoint, not a probability API, so each decision is reduced to one single-label classification. The router's **choice** is measured; its **calibration is not**. See "What is not measured" below for the per-decision boundary.

Method: one routed chat completion per dataset item, pinned to `typesafe/jev-router`, with the routed model and reasoning effort read from the OpenRouter response and generation record. Accuracy is scored against the same dataset labels the OMP-298 harness used, and the current-side (smol) numbers are produced the OMP-298 way, so accuracy and cost stay comparable.

## Results Summary

### 1. Auto-thinking effort — what routed model and effort the router picks

Sample size: 5 prompts

| Metric | Current (smol) | Jev Router |
| --- | --- | --- |
| Accuracy | 0.0% | 20.0% |
| Answered (named a label) | — | 100.0% |
| p50 Latency (ms) | 617.3 | 2683.4 |
| p95 Latency (ms) | 769.2 | 3766.9 |
| Cost per 1000 calls ($) | $0.0561 | $0.3059 |
| Cost source | provider-usage | completion-usage |
| Unparseable / off-list | — | 0.0% |
| Truncated (no answer at token cap) | — | 0.0% |
| Excluded (label outside scored set) | — | unavailable |
| Transport-failure rate | — | 0.0% |

Routed models picked: deepseek/deepseek-v4.1-flash-20260910 x2, openai/gpt-6-luna-20260922 x2, openai/gpt-6.1-sol-20260929 x1

Routed reasoning effort reported: unavailable

### 2. Unexpected-stop at 0.70

Sample size: 8162 turn ends

The router has no probability output, so the 0.70 threshold is not applied. A routed "yes" is read as the continue label (the message promised to act and stopped) and a routed "no" as the stop label.

| Metric | Current (smol) | Jev Router |
| --- | --- | --- |
| Accuracy | 49.2% | 76.1% |
| Precision (at the router's own decision) | 2.0% | 2.1% |
| Recall (at the router's own decision) | 4.9% | 11.1% |
| Answered (named a label) | — | 99.0% |
| p50 Latency (ms) | 391.5 | 1463.5 |
| p95 Latency (ms) | 576.6 | 3488.7 |
| Cost per 1000 calls ($) | $0.0380 | $0.1787 |
| Cost source | provider-usage | completion-usage |
| Unparseable / off-list | — | 0.0% |
| Truncated (no answer at token cap) | — | 1.0% |
| Transport-failure rate | — | 0.0% |

Routed models picked: openai/gpt-6-luna-20260922 x5198, deepseek/deepseek-v4.1-flash-20260910 x2531, google/gemini-3.8-flash-20260902 x300, deepseek/deepseek-v4.1-flash x81, openai/gpt-6.1-sol-20260929 x30, anthropic/claude-sonnet-5.5-20260928 x12, z-ai/glm-5.3-flash-20260826 x3, moonshotai/kimi-k3-20260715 x3, anthropic/claude-opus-5.5-20260921 x2, openai/gpt-6-astra-20260903 x1, openai/gpt-6-luna x1

Routed reasoning effort reported: unavailable

### 3. Robomp issue pre-gate

Sample size: 0 issues

The router answers the primary classification only. The typed prefilter's yes/no gates and its probability threshold are not reproducible, so this measures classification accuracy and the share of issues the router's label would route away from a full session (`invalid`, `question`).

| Metric | Jev Router |
| --- | --- |
| Primary-label accuracy | not measured |
| Answered (named a label) | not measured |
| Skip-session share (invalid / question) | not measured |
| p50 Latency (ms) | not measured |
| p95 Latency (ms) | not measured |
| Cost per 1000 calls ($) | not measured |
| Cost source | unavailable |
| Unparseable / off-list | not measured |
| Truncated (no answer at token cap) | not measured |
| Transport-failure rate | not measured |

Routed models picked: unavailable

Routed reasoning effort reported: unavailable

## What is not measured

- **Probability maps and calibration.** The router returns text, not distributions. Nothing here measures the typed API's Choice probabilities, its Noul probabilities, or the 0.70 unexpected-stop threshold operating on a probability.
- **Off-list / off-options behaviour.** The typed API's answer-space validation (`off_list`, `off_options`) has no router equivalent; a routed answer with no allowed label is counted as unparseable, one bucket only. A call that hit the completion-token cap and returned no content is counted separately as truncated — it measures the harness's budget, not the router.
- **Auto-thinking labels outside the scored set.** The router prompt offers `low|medium|high|xhigh`; dataset labels outside that set are excluded from both sides' denominators and shown as an Excluded bucket, or aliased to a scored label (`max`→`xhigh`, `minimal`→`low`).
- **Robomp routed-decision equivalence.** The prefilter's yes/no gates (`batch_audit`, `first_person_failure`, `wanted_different_behavior`, `upstream_cause`, `nondefault_exotic_env`) and its answer/session route are not measured — only the primary label and a derived skip share.
- **Router internals.** Jev Router's own routing cost and the sub-request it forwards are not visible; the report reads the routed model id and reasoning effort OpenRouter returns. The cost column is the charged cost OpenRouter reported for the call — the completion's own `usage.cost`, or the generation record's `total_cost` when that had to be read — and is `not measured` when neither surface reported a cost.
- **Cost timeliness.** OpenRouter publishes a generation record several seconds after the completion answers, so a rate-limited or slow run can end with some calls whose record never appeared; those calls still report the completion's own `usage.cost` when it carried one. Nothing here estimates a cost from tokens.
- **Current-side auto-thinking model identity.** The current side runs the model `resolveRoleSelection(["tiny", "smol"], ...)` returns (configured `modelRoles.tiny` first, then `modelRoles.smol`). Its latency and cost are per-call provider values, not a matched-model comparison to whichever model the router picked. The current-side cost source is `provider-usage` when the provider reported a non-zero completion cost, and `unavailable` when that side ran and reported none.

## Verdict

WP5 does not apply: the router is measured, not adopted — no per-request generative router is introduced.
