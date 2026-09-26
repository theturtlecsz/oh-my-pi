# Jev Decision-Classifier Measurement Report

## Overview

Measurement results comparing current smol chat classifiers and session triage against Jev typed decisions across three features:
1. Auto-thinking difficulty classification (`classifyDifficulty`)
2. Unexpected-stop classification (`classifyUnexpectedStop`)
3. Robomp issue triage prefilter (`run_prefilter`)

## Results Summary

### 1. Auto-thinking Difficulty Classification

| Metric | Current (smol) | Jev |
| --- | --- | --- |
| Accuracy | {{auto_thinking_current_accuracy}} | {{auto_thinking_jev_accuracy}} |
| p50 Latency (ms) | {{auto_thinking_current_p50}} | {{auto_thinking_jev_p50}} |
| p95 Latency (ms) | {{auto_thinking_current_p95}} | {{auto_thinking_jev_p95}} |
| Cost per 1000 calls ($) | {{auto_thinking_current_cost}} | {{auto_thinking_jev_cost}} |
| Unparseable Rate | {{auto_thinking_current_unparseable}} | {{auto_thinking_jev_unparseable}} |
| Off-list Rate | {{auto_thinking_current_off_list}} | {{auto_thinking_jev_off_list}} |

### 2. Unexpected-Stop Detection

| Metric | Current (smol) | Jev |
| --- | --- | --- |
| Accuracy | {{unexpected_stop_current_accuracy}} | {{unexpected_stop_jev_accuracy}} |
| Precision (at 0.70) | {{unexpected_stop_current_precision}} | {{unexpected_stop_jev_precision}} |
| Recall (at 0.70) | {{unexpected_stop_current_recall}} | {{unexpected_stop_jev_recall}} |
| p50 Latency (ms) | {{unexpected_stop_current_p50}} | {{unexpected_stop_jev_p50}} |
| p95 Latency (ms) | {{unexpected_stop_current_p95}} | {{unexpected_stop_jev_p95}} |
| Cost per 1000 calls ($) | {{unexpected_stop_current_cost}} | {{unexpected_stop_jev_cost}} |
| Unparseable Rate | {{unexpected_stop_current_unparseable}} | {{unexpected_stop_jev_unparseable}} |
| Off-list Rate | {{unexpected_stop_current_off_list}} | {{unexpected_stop_jev_off_list}} |

### 3. Robomp Issue Triage Prefilter

| Metric | Current (Full Session) | Jev Prefilter |
| --- | --- | --- |
| Confident-Bucket Accuracy | {{robomp_current_confident_accuracy}} | {{robomp_jev_confident_accuracy}} |
| Skip-Session Share | {{robomp_current_skip_share}} | {{robomp_jev_skip_share}} |
| Overall Accuracy | {{robomp_current_accuracy}} | {{robomp_jev_accuracy}} |
| p50 Latency (ms) | {{robomp_current_p50}} | {{robomp_jev_p50}} |
| p95 Latency (ms) | {{robomp_current_p95}} | {{robomp_jev_p95}} |
| Cost per 1000 calls ($) | {{robomp_current_cost}} | {{robomp_jev_cost}} |
| Unparseable Rate | {{robomp_current_unparseable}} | {{robomp_jev_unparseable}} |
| Off-list Rate | {{robomp_current_off_list}} | {{robomp_jev_off_list}} |

## Verdict

{{verdict}}
