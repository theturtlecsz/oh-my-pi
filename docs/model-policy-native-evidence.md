# Native Audit Model Policy Caller-Boundary Evidence

Evidence record for native auditor execution boundary and model policy governance review (OMP-615-s08-s06). This document records how policy assertions loaded from `python/omp-work/src/omp_work/routing/routing-policy.v1.json` and agent role bindings in `session-system/agents/auditor.md` reach executable caller boundaries, transport preflight, and subagent session creation.

## 1. Exact Run Command

```bash
bun test session-system/tests/model-policy-native-review.test.ts
```

The test runs fully offline without requiring live provider credentials or external network access. It executes real `prepareNativeAuditRunner` and real `task/executor.runSubprocess`, spying exclusively at the transport preflight (`ai.completeSimple`) and subagent session creation (`sdkModule.createAgentSession`) boundaries.

## 2. Covered Caller and Preconditions

- **Covered Caller**: `session-system/extensions/workflow/auditor-runner.ts::prepareNativeAuditRunner` executing real `packages/coding-agent/src/task/executor.ts::runSubprocess`.
- **Preconditions Exercised**:
  - **Agent Discovery**: Discovers installed `auditor` agent definition (`session-system/agents/auditor.md`) from `.omp/agents/auditor.md`, verifying `model: ["@audit"]` and the contract-governed typed output schema (`$defs/AuditResult`).
  - **Role Resolution**: `resolveAuditModel(ctx)` evaluates `ctx.models.resolve("@audit")` using the real `resolveModelRoleValue` implementation against isolated settings.
  - **Transport Preflight**: Calls `ctx.modelRegistry.getApiKey(auditModel)` and runs a single-turn preflight probe via `completeSimple` to prove credentials and transport connectivity before reserving any audit launch.
  - **Subprocess Dispatch**: Executes `runSubprocess` with `modelOverride: ["@audit"]`, `modelRole: "audit"`, `outputSchemaMode: "strict"`, and `outputSchema: agent.output`.
  - **Credential Resolution**: Passes the attempt-scoped key resolver `getApiKey: requestModel => ctx.modelRegistry.resolver(requestModel, attemptId)` to `createAgentSession`.
  - **Pass-Through Fixture Pattern**: Uses the `createMockSession` and `createSessionResult` pattern from `packages/coding-agent/test/task/executor-pass-through.test.ts` unchanged; restores all spies via `vi.restoreAllMocks()` in `afterEach`.

## 3. Alias -> Wire Provider/Model -> Lane Mapping

| Surface | Entity | Identifier | Details |
| --- | --- | --- | --- |
| Agent Definition | Selector Alias | `@audit` | Defined in `session-system/agents/auditor.md#model` |
| Settings Role | Configured Role | `modelRoles.audit` | Assigned to `openai-codex/gpt-5.6-sol` |
| Policy Reviewer | Governed Identity | `openai-codex/gpt-5.6-sol:medium` | Admitted in `routing-policy.v1.json#stages.audit.reviewers` |
| Catalog Model | Wire Provider / ID | `openai-codex` / `gpt-5.6-sol` | Wire API: `openai-codex-responses`, Context: 1M tokens |
| Policy Concurrency | Concurrency Lane | `openai_codex` | Concurrency cap: 1, unresolved owner: `Chris` |

### Boundary Observation

1. **Preflight Probe**: `ai.completeSimple` receives `Model { provider: "openai-codex", id: "gpt-5.6-sol" }`.
2. **Preflight Key Lookup**: `ctx.modelRegistry.getApiKey` receives `Model { provider: "openai-codex", id: "gpt-5.6-sol" }`.
3. **Session Creation**: `sdkModule.createAgentSession` receives `opts.model` with `{ provider: "openai-codex", id: "gpt-5.6-sol" }`.
4. **Subprocess Credential Resolver**: `ctx.modelRegistry.resolver` is invoked with `(Model { provider: "openai-codex", id: "gpt-5.6-sol" }, attemptId)`.
5. **Lane Agreement**: The wire provider `openai-codex` corresponds directly to concurrency lane `openai_codex` (cap: 1), matching policy declaration and owner governance.

### Provider Drift Exposure

When isolated `@audit` is pointed to `openai/gpt-5.6-sol`:
- The model ID (`gpt-5.6-sol`) is identical.
- The wire provider switches to `openai` (wire API: `openai-responses`).
- Preflight and session creation boundaries capture provider `"openai"`, exposing drift away from the admitted reviewer provider (`openai-codex`) and the governed concurrency lane (`openai_codex`).

## 4. Reviewer-vs-Stage Distinction

| Dimension | Stage Route (`stages.audit`) | Admitted Reviewer (`stages.audit.reviewers[0]`) |
| --- | --- | --- |
| **Provider** | `kimi` | `openai-codex` |
| **Model** | `kimi-code/k3-256k` | `gpt-5.6-sol` |
| **Effort** | `E3` | `medium` |
| **Lane** | `kimi` (cap: 2) | `openai_codex` (cap: 1) |
| **Caller** | Orchestrator stage dispatch | Native audit runner (`prepareNativeAuditRunner`) |

As recorded in `routing-policy.v1.json#governance.authority.reconciliation` and `docs/model-policy-governance.md#authority-and-reconciliation`:
> "Audit route Kimi and admitted Sol medium reviewer are distinct selectors, not interchangeable execution evidence."

The native audit runner executes the admitted reviewer (`@audit` -> `openai-codex/gpt-5.6-sol`), NOT the stage route (`kimi/kimi-code/k3-256k`). The test asserts that the boundary receives the reviewer identity (`openai-codex`), proving caller consumption of the reviewer specification rather than the stage route.

## 5. Fallback Exposure Contract

- `task/executor.ts` extracts `resolvedModelIsFallback` directly from `session.servingModel.isFallback`.
- When fallback routing is active (e.g., primary model saturated or unavailable):
  - `result.resolvedModelIsFallback` is set to `true`.
  - Fallback use is explicitly exposed in `NativeAuditRunResult.resolvedModelIsFallback`.
- **Primary Agreement Contract**: Fallback execution must NEVER be counted as primary agreement.
  - Primary agreement strictly requires:
    ```ts
    const isPrimaryAgreement =
      result.resolvedModel === "openai-codex/gpt-5.6-sol" && !result.resolvedModelIsFallback;
    ```
  - When `resolvedModelIsFallback` is `true`, `isPrimaryAgreement` evaluates to `false`.

## 6. Owner-Only Checks and Real Native Audit Receipts Still Required

The test fixture proves caller consumption and boundary observation under controlled, isolated settings. It does NOT attest to the owner's (Chris's) current local installation, user-level configuration, or live production qualification.

The following prerequisites remain strictly owner-only and cannot be inferred from test execution:
1. **Effective User Configuration Check**: Verifying that the target deployment host actually configures `modelRoles.audit: "openai-codex/gpt-5.6-sol"` in user-level settings (`~/.omp/config.json` or `~/.omp/settings.json`, outside the repository).
2. **Host Provider Credentials**: Verifying that valid, unexpired provider credentials for `openai-codex` exist on the target deployment host to satisfy the `completeSimple` transport preflight.
3. **Real Native Audit Receipt**: An end-to-end audit receipt from a live execution against an actual Work Ledger candidate, reserved and settled on the ledger via `WorkClient.settleAuditorLaunch`.

## 7. Scope Boundary and S05 Separation

- This evidence covers the TypeScript native audit runner caller boundary (`session-system/extensions/workflow/auditor-runner.ts`) and its consumption of governed reviewer policy.
- Any disputed policy scope, model reassignments, live token receipts, or production qualifications are reserved for `OMP-615-s05` rather than changing models or policy mechanically.
