# OMP routing substitutions

Record of the packaged routing (`python/omp-work/src/omp_work/routing/routing-policy.v1.json`) against the historical O2 roles (`docs/programme/ORIGINAL-OBLIGATIONS.md`). Machine-readable form: `python/omp-work/src/omp_work/routing/substitutions.v1.json` (`routing-substitutions.v1`). This reconciliation records; it changes no route. `docs/models.md` describes configuration, not stage assignments.

Three things stay distinct: a **route** is a stage's declared provider/model; a **call** is an actual model invocation by a named caller; a **subscription** is access to a provider. Subscriptions are unverified; current ids are retained. The premium set remains `claude-opus-5-5` / `gpt-6-sol`; reserve allocation defaults to USD 0.

| Stage | Provider / model | Historical O2 role | Caller | Disposition |
| --- | --- | --- | --- | --- |
| plan | openai_codex / gpt-5.6-sol | planning: Fable 5.1 | none (orchestrator plan is deterministic) | pending_owner |
| implement | gemini_flash / gemini-3.8-flash-high | implementation: Gemini 3.8 Flash high | `model_proxy.py` | kept |
| verify | gemini_flash / gemini-3.8-flash-high | none recorded | none | kept |
| research | ollama_cloud / deepseek-v4.1-flash:cloud | none recorded | `intake_support_admission.py` | kept |
| audit | kimi / kimi-code/k3-256k; reviewer openai-codex/gpt-5.6-sol:medium | auditor: Sol medium | none | kept |
| acceptance_review | kimi / kimi-code/k3-256k; reviewer kimi-code/k3-256k | independent reviewer: Kimi K3 256K | none | kept |

## Pending

- **plan**: policy route gpt-5.6-sol differs from historical Fable 5.1. Pending owner decision; no approval recorded.
- **gemini-flash-overflow**: shipped behavior queue substitutes gemini_flash with ollama_cloud/deepseek-v4.1-flash:cloud. Substitution pending Chris approval; not owner-approved.

Every entry carries `approval: null`.
