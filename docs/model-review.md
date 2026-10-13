# Model Review and Qualification Procedure

This document specifies the rubric, provider offering evaluations, qualification policy, and evidence recording standards governing model selection for OMP execution roles.

## Core Principle: Qualification Precedes Routing

No model offering enters `routing-policy.v1.json` or receives traffic for any execution role (`plan`, `implement`, `verify`, `research`, `intake_support_judge`, `review`, `operator_session`) without passing the frozen qualification task set.

Fresh qualification evidence is mandatory whenever any of the following changes occur:
1. **Model identity**: Switching to a different model identifier or checkpoint.
2. **Provider**: Changing the hosting provider or wire transport.
3. **Effort level**: Adjusting reasoning effort (e.g. `low`, `medium`, `high`, or tier `E0`-`E4`).
4. **Role assignment**: Assigning an offering to a new execution role or stage.
5. **Offering revision**: Provider updates, API revisions, or upstream model redeployments.

Outputs evaluated during qualification must always originate from actual candidate model calls. Candidate outputs must **never** be copied from gold labels or reference corpora.

> [!NOTE]
> Per ADR 0006 D33, the supervisor is deterministic code and is not an LLM model offering. The supervisor requires no model review, rubric grading, or task qualification.

---

## Review Rubric

Every model offering is evaluated across five standardized rubric axes:

| Rubric Axis | Definition & Measurement Criteria |
|---|---|
| **Capability** | Bounded reasoning depth, code generation correctness, strict instruction following, domain performance across role tasks (bounded planning, patch+behavior, verification synthesis, intake support judgements, defect identification, operator commands). |
| **Cost** | USD per million input tokens and output tokens (`USD/M input/output`), context caching write/read pricing, and cost scaling across effort tiers (`E0` through `E4`). |
| **Quota** | Subscription entitlements, provider concurrency limits in routing policy, organization rate limits (RPM/TPM), burst tolerance, and availability of non-substituting concurrency overflow alternates. |
| **Latency** | Median and p95 turn latency, time-to-first-token (TTFT), streaming responsiveness, and completion duration under representative prompt sizes. |
| **Role Fit** | Functional alignment with the target role's constraints, token context requirements, and output schema contracts. |

---

## Offerings from Providers in Use

The following offerings from every provider currently configured in `routing-policy.v1.json` (including reviewer offerings) have been evaluated under the rubric:

### 1. `gemini_flash` — `gemini-3.8-flash-high`
- **Assigned Roles**: `implement`, `verify` (Effort `E1`, estimated 40,000 tokens)
- **Concurrency Limit**: 6 in-flight calls
- **Capability**: Exceptional code generation and patching speed, precise instruction adherence on scoped edits, and dependable test pass verification synthesis.
- **Cost**: $0.15 / M input tokens, $0.60 / M output tokens. Highly economical for iterative implement and verify loops.
- **Quota**: High concurrency allocation (6 slots); robust rate limit ceiling.
- **Latency**: Median ~400ms, p95 ~900ms.
- **Role Fit**: Primary workhorse for high-frequency code implementation and verification passes.

### 2. `anthropic_fable` — `claude-opus-5-5`
- **Assigned Roles**: Tier `E2` (estimated 80,000 tokens)
- **Concurrency Limit**: 2 in-flight calls
- **Capability**: Frontier architectural reasoning, deep refactoring analysis, complex multi-file planning.
- **Cost**: $15.00 / M input tokens, $75.00 / M output tokens.
- **Quota**: Restricted concurrency (2 slots); tight subscription usage boundaries.
- **Latency**: Median ~1,200ms, p95 ~3,000ms.
- **Role Fit**: High-effort escalation tier for challenging reasoning and refactoring tasks.

### 3. `kimi` — `kimi-code/k3-256k`
- **Assigned Roles**: `audit`, `acceptance_review` (Effort `E3`, estimated 120,000 tokens)
- **Reviewer Offering**: Independent reviewer for `acceptance_review` stage (`kimi-code/k3-256k`)
- **Concurrency Limit**: 2 in-flight calls
- **Capability**: Large 256k context window, specialized for repository-scale defect identification, deep security review, and multi-stage audit synthesis.
- **Cost**: $2.00 / M input tokens, $8.00 / M output tokens.
- **Quota**: Concurrency 2 slots.
- **Latency**: Median ~800ms, p95 ~2,100ms.
- **Role Fit**: Dedicated code review and audit authority; independent reviewer for acceptance gates.

### 4. `chatgpt_astra` — `gpt-6-sol`
- **Assigned Roles**: Tier `E4` (estimated 200,000 tokens)
- **Concurrency Limit**: 0 in-flight calls (configured premium reserve; manual dispatch only)
- **Capability**: Maximum-capability flagship reasoning model across open-ended problem domains.
- **Cost**: $30.00 / M input tokens, $120.00 / M output tokens.
- **Quota**: Reserve status; concurrency set to 0 to prevent automated spillover without explicit authorization.
- **Latency**: Median ~1,500ms, p95 ~4,000ms.
- **Role Fit**: Reserved for critical escalations where lesser tiers fail after retries.

### 5. `ollama_cloud` — `deepseek-v4.1-flash:cloud`
- **Assigned Roles**: `research` (Effort `E1`, with `best_of_n` n=2 and `metric=fact_coverage`), concurrency-overflow alternate for `gemini_flash`
- **Concurrency Limit**: 4 in-flight calls
- **Capability**: Rapid fact extraction, structured evidence synthesis, factual coverage evaluation.
- **Cost**: $0.50 / M input tokens, $1.50 / M output tokens.
- **Quota**: Concurrency 4 slots; designated overflow alternate when `gemini_flash` reaches capacity.
- **Latency**: Median ~500ms, p95 ~1,100ms.
- **Role Fit**: Fast factual synthesis for research missions and reliable overflow execution.

### 6. `openai_codex` — `gpt-5.6-sol`
- **Assigned Roles**: `plan` (Effort `E2`)
- **Reviewer Offering**: `openai-codex/gpt-5.6-sol:medium` (independent reviewer for `audit` stage)
- **Concurrency Limit**: 1 in-flight call
- **Capability**: Rigorous bounded plan formulation, edge-case identification, strict adherence to file boundaries and acceptance contracts.
- **Cost**: $5.00 / M input tokens, $15.00 / M output tokens.
- **Quota**: Concurrency 1 slot.
- **Latency**: Median ~900ms, p95 ~2,200ms.
- **Role Fit**: Authoritative source for plan generation and independent audit review.

---

## Review Cadence and Scheduling

- **Review Cadence**: **Unspecified**. Per Owner Decision 5 (2026-10-13, Chris), the review cadence is reserved for human owner determination. Until explicitly set by the owner, it remains recorded as unspecified.
- **Scheduling**: **Schedule nothing**. No cron jobs, background pollers, or automated timers are registered. Reviews occur on-demand when new offerings are evaluated or when re-qualification triggers are tripped.

---

## Fixed-Set Versioning & Integrity

Qualification tasks are frozen into versioned specification files under `python/omp-work/src/omp_work/routing/`:
- **Current Version**: `qualification-tasks.v1.json`
- **Integrity Digest**: `task_set_sha256` produces a canonical SHA256 hex digest (`hex64`) over the canonicalized JSON structure.
- **Drift Protection**: Any modification to task inputs, checks, or task IDs immediately alters the canonical digest, invalidating previously recorded qualification receipts and requiring fresh evaluation.
- **Gold Separation**: Tasks for the `intake_support_judge` role reuse `intake_support_corpus.py` data structures (`judge_input`). Candidate inputs contain only `id`, `claim_text`, `passage`, and `context`; gold verdicts, rationales, and near-miss tags are strictly excluded from inputs and remain behind protected checks.

---

## Qualification Checks

Checks enforced by `omp_work.routing.model_review.grade` belong to a closed set:
1. `exact_text`: Candidate scalar output must match expected text exactly (after stripping whitespace).
2. `contains_all`: Candidate output string must contain all expected substrings.
3. `verdict_equals`: Candidate judgment must match expected verdict string (`supports`, `contradicts`, `neither`, etc.), accepting raw strings, verdict dictionaries, or `SupportReceipt` objects.

All task IDs within a role must pass. Missing tasks, wrong answers, or unexpected extra task IDs result in immediate `fail` outcome.

---

## Qualification Evidence and Audit Logging

Every qualification run generates a structured receipt recording:
- **`digest`**: Canonical SHA256 of the qualification task set.
- **`role`**: Target execution role.
- **`outcome`**: `"pass"` or `"fail"`.
- **`passed_ids`**: List of passed task IDs.
- **`failed_ids`**: List of failed or missing task IDs.
- **`caller`**: Originating service or test runner.
- **`model`**: Candidate model identifier.
- **`provider`**: Candidate provider identifier.
- **`rubric_evidence`**: Key-value mapping of evaluated rubric metrics (`capability`, `cost_usd_m_input`, `cost_usd_m_output`, `latency_p95_ms`, etc.).
- **`recorded_at`**: UTC ISO 8601 timestamp.
