# Jev Router Measurement Report

## Overview

Measurement of the three OMP-298 decisions through OpenRouter's Jev Router (`{{router_model}}`) — the only Jev surface OpenRouter exposes — instead of TypeSafe's typed decision API (`POST /v1/systemone`), which answers HTTP 401 to the owner's OpenRouter key.

The router is a chat-completions endpoint, not a probability API, so each decision is reduced to one single-label classification. The router's **choice** is measured; its **calibration is not**. See "What is not measured" below for the per-decision boundary.

Method: one routed chat completion per dataset item, pinned to `{{router_model}}`, with the routed model and reasoning effort read from the OpenRouter response and generation record. Accuracy is scored against the same dataset labels the OMP-298 harness used, and the current-side (smol) numbers are produced the OMP-298 way, so accuracy and cost stay comparable.

## Results Summary

### 1. Auto-thinking effort — what routed model and effort the router picks

Sample size: {{auto_thinking_sample_size}} prompts

| Metric | Current (smol) | Jev Router |
| --- | --- | --- |
| Accuracy | {{auto_thinking_current_accuracy}} | {{auto_thinking_route_accuracy}} |
| Answered (named a label) | — | {{auto_thinking_route_answer_rate}} |
| p50 Latency (ms) | {{auto_thinking_current_p50}} | {{auto_thinking_route_p50}} |
| p95 Latency (ms) | {{auto_thinking_current_p95}} | {{auto_thinking_route_p95}} |
| Cost per 1000 calls ($) | {{auto_thinking_current_cost}} | {{auto_thinking_route_cost}} |
| Cost source | provider usage | {{auto_thinking_route_cost_source}} |
| Unparseable / off-list | — | {{auto_thinking_route_unparseable}} |
| Transport-failure rate | — | {{auto_thinking_route_transport_failure}} |

Routed models picked: {{auto_thinking_route_routed_models}}

Routed reasoning effort reported: {{auto_thinking_route_routed_effort}}

### 2. Unexpected-stop at 0.70

Sample size: {{unexpected_stop_sample_size}} turn ends

The router has no probability output, so the 0.70 threshold is not applied. A routed "yes" is read as the continue label (the message promised to act and stopped) and a routed "no" as the stop label.

| Metric | Current (smol) | Jev Router |
| --- | --- | --- |
| Accuracy | {{unexpected_stop_current_accuracy}} | {{unexpected_stop_route_accuracy}} |
| Precision (at the router's own decision) | {{unexpected_stop_current_precision}} | {{unexpected_stop_route_precision}} |
| Recall (at the router's own decision) | {{unexpected_stop_current_recall}} | {{unexpected_stop_route_recall}} |
| Answered (named a label) | — | {{unexpected_stop_route_answer_rate}} |
| p50 Latency (ms) | {{unexpected_stop_current_p50}} | {{unexpected_stop_route_p50}} |
| p95 Latency (ms) | {{unexpected_stop_current_p95}} | {{unexpected_stop_route_p95}} |
| Cost per 1000 calls ($) | {{unexpected_stop_current_cost}} | {{unexpected_stop_route_cost}} |
| Cost source | provider usage | {{unexpected_stop_route_cost_source}} |
| Unparseable / off-list | — | {{unexpected_stop_route_unparseable}} |
| Transport-failure rate | — | {{unexpected_stop_route_transport_failure}} |

Routed models picked: {{unexpected_stop_route_routed_models}}

Routed reasoning effort reported: {{unexpected_stop_route_routed_effort}}

### 3. Robomp issue pre-gate

Sample size: {{robomp_sample_size}} issues

The router answers the primary classification only. The typed prefilter's yes/no gates and its probability threshold are not reproducible, so this measures classification accuracy and the share of issues the router's label would route away from a full session (`invalid`, `question`).

| Metric | Jev Router |
| --- | --- |
| Primary-label accuracy | {{robomp_route_accuracy}} |
| Answered (named a label) | {{robomp_route_answer_rate}} |
| Skip-session share (invalid / question) | {{robomp_route_skip_share}} |
| p50 Latency (ms) | {{robomp_route_p50}} |
| p95 Latency (ms) | {{robomp_route_p95}} |
| Cost per 1000 calls ($) | {{robomp_route_cost}} |
| Cost source | {{robomp_route_cost_source}} |
| Unparseable / off-list | {{robomp_route_unparseable}} |
| Transport-failure rate | {{robomp_route_transport_failure}} |

Routed models picked: {{robomp_route_routed_models}}

Routed reasoning effort reported: {{robomp_route_routed_effort}}

## What is not measured

- **Probability maps and calibration.** The router returns text, not distributions. Nothing here measures the typed API's Choice probabilities, its Noul probabilities, or the 0.70 unexpected-stop threshold operating on a probability.
- **Off-list / off-options behaviour.** The typed API's answer-space validation (`off_list`, `off_options`) has no router equivalent; a routed answer with no allowed label is counted as unparseable, one bucket only.
- **Robomp routed-decision equivalence.** The prefilter's yes/no gates (`batch_audit`, `first_person_failure`, `wanted_different_behavior`, `upstream_cause`, `nondefault_exotic_env`) and its answer/session route are not measured — only the primary label and a derived skip share.
- **Router internals.** Jev Router's own routing cost and the sub-request it forwards are not visible; the report reads the routed model id and reasoning effort OpenRouter returns, and the routed model's price is what the cost column reflects.
- **Current-side auto-thinking model identity.** The current side runs the configured smol role through the classifiers; its latency and cost are per-call provider values, not a matched-model comparison to whichever model the router picked.

## Verdict

{{verdict}}
