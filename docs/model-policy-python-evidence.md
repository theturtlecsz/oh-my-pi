# Model Policy Python Caller-Boundary Evidence

Python caller-boundary evidence for Work Ledger model policy routing (OMP-615-s08-s02-s04). This document records whether and how policy assertions loaded from `omp_work.routing.policy:load_policy` reach executable caller boundaries, orchestrator stages, and upstream transports.

## 1. Implement: Proxy Transport Proven

- **Caller**: Work execution proxy caller (`omp_work.model_proxy:create_app`, handling registration via `POST /tasks` and Gemini calls via `POST /v1beta/models/{model}:streamGenerateContent?alt=sse`).
- **Evidence**:
  - `python/omp-work/tests/test_model_policy_execution.py::test_implement_call_uses_policy_model_and_bills_it`: Proves that `load_policy()` implement model/provider reaches the upstream `transport.post` URL (e.g. `models/{model}:streamGenerateContent`), the grant descriptor id equals the policy model, billed spend is recorded (`usd > 0`), and one `omp_jobs.usage_events` row agrees with the model and billed USD amount.
  - `python/omp-work/tests/test_model_policy_execution.py::test_policy_model_change_reaches_upstream_and_usage`: Proves that mutating the routing policy dynamically alters the upstream model targeted by `transport.post` (`models/{new}:streamGenerateContent` called; `models/{old}:` never called) and the recorded `omp_jobs.usage_events` row agrees with the mutated model.
  - **Gated lane**: `test:py:work-ledger:integration` script in `package.json:133` includes `test_model_policy_execution.py` with `OMP_WORK_POSTGRES_INTEGRATION=1`.
- **Limitation**:
  - Gated lane execution result is recorded as **pending** until a gated run passes on main (requires a running PostgreSQL instance with `OMP_WORK_POSTGRES_INTEGRATION=1`; offline test execution skips the test).
  - Transport execution is verified against a recording transport (`RecordingTransport`) capturing request URLs and SSE responses, rather than live provider network calls.

## 2. Plan / Verify / Audit: Deterministic Orchestrator Operations

- **Caller**: Orchestrator service operations dispatching and executing workflow stages (`omp_work.orchestrator.service.OrchestratorHandler`).
- **Evidence**:
  - `python/omp-work/src/omp_work/orchestrator/service.py:41`: Imports `bounds_for, decide` from `omp_work.orchestrator.stages` (names at lines 47–48).
  - `python/omp-work/src/omp_work/orchestrator/service.py:54`: Imports `RoutingPolicy, load_policy` from `omp_work.routing.policy`.
  - `python/omp-work/src/omp_work/orchestrator/service.py:1232`, `1252`, `1256`: State machine step transitions are evaluated deterministically via `chosen = decide(facts, self._bounds(stage))`.
  - `python/omp-work/src/omp_work/orchestrator/service.py:1378`: `self._bounds(stage)` uses `load_policy()` solely to construct stage bounds: `bounds_for(load_policy(), kind, stage, self.config.repair_rounds)`.
  - `python/omp-work/src/omp_work/orchestrator/service.py:1941-1948`: `_alternate(stage)` uses `load_policy()` solely to look up provider alternate fallbacks (`from_provider` mapped to `to.provider:to.model`).
  - `python/omp-work/src/omp_work/orchestrator/service.py:2019`: Initial qualification check evaluates `decide(facts, bounds_for(load_policy(), request.scope.kind, STAGE_ORDER[0], config.repair_rounds))`.
  - `python/omp-work/src/omp_work/orchestrator/service.py:2181`: `dry_run` evaluates `decide(facts, bounds_for(load_policy(), request.scope.kind, facts.stage, config.repair_rounds))`.
  - Code inspection confirms deterministic orchestration: `load_policy()` feeds bounds and `decide`, plus the `_alternate` metadata lookup above. There is no direct model or LLM API call in `orchestrator/service.py`.
- **Limitation**:
  - This proves service-level decisions, not model execution for plan, verify, or audit. `python/omp-work/src/omp_work/orchestrator/service.py:1298` selects registered callbacks (`run(ctx)` at line 1323); their external runners are outside this evidence boundary.

## 3. Research: Local Campaign Generation and Null Model Attribution

- **Caller**: Research campaign ledger writer (`omp_work.research_ledger.run_campaign_with_ledger`).
- **Evidence**:
  - `python/omp-work/src/omp_work/research_campaign.py:48`: `_default_generate` builds candidate strings locally; `run_campaign` selects it when no generator is supplied at line 78. `python/omp-work/src/omp_work/research_ledger.py:39` calls `run_campaign` without a generator and records `"model": None` at line 51.
  - `python/omp-work/tests/test_research_model_attribution.py::test_research_ledger_model_is_null_and_no_tier_model`: Proves `run_campaign_with_ledger` generates campaign results locally via deterministic generation (`_default_generate` in `omp_work.research_campaign`), and the persisted ledger event has `"model": null` (`None`). No event string contains `"gemini"` or any model from `load_policy().tiers`.
  - `python/omp-work/tests/test_research_model_attribution.py::test_research_ledger_tokens_and_campaign_parity`: Proves persisted tokens sum to `campaign.spent_tokens` (`input_tokens + output_tokens == ledged.campaign.spent_tokens`), and campaign output attributes (`finding`, `stopped`, `eval_winner_n`, `spent_tokens`) match direct `research_campaign.run_campaign`.
  - `python/omp-work/tests/test_research_model_attribution.py::test_passing_model_raises_type_error`: Proves the `model` parameter was removed from `run_campaign_with_ledger`; passing `model=` raises `TypeError`.
- **Limitation**:
  - These tests cover the ledger wrapper and default local generator, not every research caller. `run_campaign` accepts a custom generator; no provider execution or actual token receipt is proven here. Ledger token totals are local budget accounting.

## 4. Judge / Intake: No Execution Consumer for resolve_admission

- **Caller**: None (unconsumed / dead boundary).
- **Evidence**:
  - State the grep used:
    ```bash
    grep -rn "resolve_admission" python/omp-work/src
    ```
  - Grep result:
    ```
    python/omp-work/src/omp_work/intake_support_admission.py:3:``resolve_admission`` answers, from the caller's own open transaction, whether
    python/omp-work/src/omp_work/intake_support_admission.py:38:    "resolve_admission",
    python/omp-work/src/omp_work/intake_support_admission.py:66:def resolve_admission(
    python/omp-work/src/omp_work/routing/routing-policy.v1.json:247:            "python/omp-work/src/omp_work/intake_support_admission.py::resolve_admission"
    python/omp-work/src/omp_work/routing/routing-policy.v1.json:260:            "python/omp-work/src/omp_work/intake_support_admission.py::resolve_admission",
    ```
  - The grep confirms that `resolve_admission` appears only in its own docstring, export list, and definition in `python/omp-work/src/omp_work/intake_support_admission.py`, and in declarative routing metadata in `python/omp-work/src/omp_work/routing/routing-policy.v1.json`. There are no execution callers or consumers of `resolve_admission` in `python/omp-work/src`.
- **Limitation**:
  - Search covers source references in `python/omp-work/src`, not tests or external/dynamic callers. `python/omp-work/src/omp_work/intake_support_admission.py:97` resolves the research route for admission, but this search finds no execution consumer. It does not establish whether other judge/intake paths call models.

## 5. Not Proven: Unverified Research/Judge Execution and Receipt Boundaries

- **Caller**: Research and judge role callers across the Python workspace.
- **Evidence**:
  - Research and judge model execution and actual receipts are assigned to owner slice `OMP-615-s05`.
  - Grant counts, ledger labels, and role strings (e.g. `intake_support_judge`, `research_campaign`) are metadata labels only and are not execution proof.
- **Limitation**:
  - The research attribution tests in section 3 and admission search in section 4 do not prove research/judge provider execution or actual receipts. Full execution proof remains assigned to `OMP-615-s05`; collection of tests only confirms citation names, not passing execution.
