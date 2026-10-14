# Native audit caller evidence (OMP-615-s08-s06)

Run from repository root:

```bash
bun test session-system/tests/model-policy-native-review.test.ts
```

This offline fixture covers `session-system/extensions/workflow/auditor-runner.ts::prepareNativeAuditRunner` through real `packages/coding-agent/src/task/executor.ts::runSubprocess`. Only `completeSimple` transport preflight and SDK `createAgentSession` execution boundary are spied on. Agent discovery and alias resolution are real; SDK session fixture prompts and yields valid typed `AuditResult` output using executor-pass-through fixture pattern.

The command starts a fresh Bun test process running the same file, with isolated HOME, agent directory, XDG roots, profiles, credentials and config overlays. Each child case pins discovery to its own workspace, installs a copy of `session-system/agents/auditor.md` and writes `.omp/config.yml`. Thus the runner's independent `Settings.loadReadOnly` and context's `Settings.isolated` see matching audit bindings. Fresh process avoids cached host home/configuration without adding discovery or settings spies. Child runs three cases; parent requires successful exit, includes child diagnostics on failure and removes temporary tree. Child restores execution spies and discovery override after each case.

## Authority and observed identity

As recorded in `docs/model-policy-governance.md`, packaged `python/omp-work/src/omp_work/routing/routing-policy.v1.json` governs Work routing/reviewer admission. Session role assignments remain independent configuration. This test assigns admitted reviewer to isolated session `@audit`; native runner does not itself read Work policy or enforce reviewer admission. Evidence proves configured caller consumption, not an owner assignment or settled policy scope.

| Surface | Fixture or policy value | Evidence |
| --- | --- | --- |
| Discovered auditor selector | `@audit` | Repository auditor copied into isolated `.omp/agents` |
| Isolated role assignment | `modelRoles.audit = openai-codex/gpt-5.6-sol` | Real `resolveModelRoleValue`, real executor selection |
| Admitted reviewer | `openai-codex/gpt-5.6-sol:medium` | `stages.audit.reviewers[0]` |
| Observed Model.provider / Model.id | `openai-codex` / `gpt-5.6-sol` | Preflight, preflight credential lookup, SDK session creation, attempt-scoped credential resolver |
| Policy concurrency lane | `openai_codex`, cap 1 | `concurrency.providers`; **not an observed execution lane** |
| Lane/wire association | `wire: null`, `status: pending_owner`, owner Chris | `governance.lanes.openai_codex`; unresolved, no lane-agreement claim |

Alias resolves to observed provider/model; mapping onward to a concurrency lane remains unqualified. Governance explicitly rejects reviewer prefixes, matching names and API availability as lane/wire evidence. Test preserves that unresolved record rather than deriving a lane by changing hyphens to underscores. Native caller path does not exercise Work proxy lane admission.

Changing only isolated `@audit` to `openai/gpt-5.6-sol` keeps model ID identical while preflight and SDK session boundary switch provider to `openai`. Runner reports `openai/gpt-5.6-sol`, exposing reviewer-provider drift. No lane is inferred from either provider spelling.

## Reviewer versus stage

Work audit stage declares provider/lane `kimi`, model `kimi-code/k3-256k`, effort `E3`. Its admitted reviewer is distinct: `openai-codex/gpt-5.6-sol:medium`. Fixture checks native caller receives configured reviewer identity rather than stage route. It does not establish that owner's session `@audit` must equal that reviewer; reconciliation belongs to s05. Both `kimi` and `openai_codex` lane/wire records remain pending owner.

Fixture uses provider/model identity without reviewer effort suffix. This is provider/model execution evidence, not effort qualification.

## Fallback evidence

SDK fixture reports `servingModel.isFallback: true`, retaining same model identity to prove identity agreement alone is insufficient. Real executor and runner expose `resolvedModelIsFallback: true`; primary agreement requires matching reviewer identity **and** no fallback. This checks propagation of SDK fallback metadata, not live fallback selection, quota saturation, alternate approval or scheduling.

## Required owner evidence

Fixture does not attest Chris's current installation. Still required:

- Owner-only effective-config check on target installation: discovered auditor, effective `modelRoles.audit`, overlays, profiles and provider credentials. Fixture preflight is intercepted; no live connectivity is established.
- Real native audit receipt against actual Work Ledger candidate, including launch reservation/settlement, selected provider/model and fallback/substitution evidence.
- Qualification of exact lane/wire association and disputed session-reviewer versus stage scope, reserved for OMP-615-s05. No models or routing policy changed mechanically.
