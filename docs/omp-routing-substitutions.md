# OMP routing substitutions

Record of the packaged routing (`python/omp-work/src/omp_work/routing/routing-policy.v1.json`) against the historical O2 roles (`docs/programme/ORIGINAL-OBLIGATIONS.md`), using the current models as the baseline. Machine-readable form: `python/omp-work/src/omp_work/routing/substitutions.v1.json` (`routing-substitutions.v1`). This reconciliation records; it changes no route. We do not presume `docs/models.md` is the governing policy: OMP-615-s08 verifies which file governs.

Three things stay distinct: a **route** is a stage's declared provider/model; a **call** is an actual model invocation by a named caller; a **subscription** is access to a provider. Subscriptions are unverified; current ids are retained. The premium set remains `claude-opus-5-5` / `gpt-6-sol`; reserve allocation defaults to USD 0.

| Stage | Provider / model | Historical O2 role | Caller | Disposition | Authority |
| --- | --- | --- | --- | --- | --- |
| plan | openai_codex / gpt-5.6-sol | planning: Fable 5.1 | none (orchestrator plan is deterministic) | pending_owner | pending governing-file verification (s08) |
| implement | gemini_flash / gemini-3.8-flash-high | implementation: Gemini 3.8 Flash high | `model_proxy.py` | kept | pending governing-file verification (s08) |
| verify | gemini_flash / gemini-3.8-flash-high | none recorded | none | kept | pending governing-file verification (s08) |
| research | ollama_cloud / deepseek-v4.1-flash:cloud | none recorded | none; admission by `intake_support_admission.py` (selects route, records usage; no production caller found, execution unverified) | kept | pending governing-file verification (s08) |
| audit | kimi / kimi-code/k3-256k; reviewer openai-codex/gpt-5.6-sol:medium | auditor: Sol medium | none | kept | pending governing-file verification (s08) |
| acceptance_review | kimi / kimi-code/k3-256k; reviewer kimi-code/k3-256k | independent reviewer: Kimi K3 256K | none | kept | pending governing-file verification (s08) |

## Pending substitutions and overflow

- **plan**: policy route `gpt-5.6-sol` differs from historical Fable 5.1. Substitution pending owner decision; no approval recorded (`approval: null`). Marked `pending governing-file verification (s08)` where the authority is not yet settled.
- **gemini-flash-overflow**: shipped behavior queue substitutes `gemini_flash` with `ollama_cloud/deepseek-v4.1-flash:cloud` when `gemini_flash` is saturated. Substitution pending Chris approval; not owner-approved (`approval: null`). Marked `pending governing-file verification (s08)` where the authority is not yet settled.

Every entry carries `approval: null`.
