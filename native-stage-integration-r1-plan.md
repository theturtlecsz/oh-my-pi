# Native stage integration r1

## Scope

Own the native host/runtime bridge in this isolated implementation tree. WorkService remains the only launch authority; Knowledge remains the context compiler authority. The host dispatches one role worker through the existing `runSubprocess` boundary.

## Actual inputs read

- `CURRENT-HANDOFF.md` in this tree (native profile and 0024 staged; host still audit-only).
- `ROOT-R4-NATIVE-INTEGRATION-DISPOSITION.md` and `ROOT-R3-DISPOSITION.md` under `/home/thetu/.local/state/omp-fleet-knowledge/stage-context-planning-20260914`.
- Fable amendment and plan under `/home/thetu/.local/state/omp-stabilization/coordinator/native-tier-workflow-planning-20260914`.
- Enola source read-only under `/home/thetu/.local/state/omp-fleet-knowledge/stage-context-delivery-20260914/source`.

## Contract paths and hashes

- WorkService migration: `python/omp-work/src/omp_work/operations/migrations/0024_native_stage_launches.sql`.
- Python command/models/store: `python/omp-work/src/omp_work/v1/models.py`, `api_models.py`, `service.py`, `store.py`.
- TS wire contract: `packages/work-client/src/index.ts`; loaded contract digest remains `63f66b1f94f4c0002f860fad4c61072451497f24650a99d3f38ce8425faf775c` because 0024 was already included in staged contract.
- Native runner/profile: `session-system/extensions/workflow/auditor-runner.ts`, `native-stage-profile.ts`.
- Context compiler bridge: `session-system/extensions/workflow/knowledge-bridge.ts`.
- Executor dispatch hook: `packages/coding-agent/src/task/executor.ts`, `packages/coding-agent/src/sdk.ts`.

Current file SHA-256 evidence: profile `88d0f852e81cc2176e7c5308d2f2194859b786f884ecd07f5231c901a81380b3`; runner `2edb00af61713cd11a65ac99bae650d1a16671d9dd52bf1bd64e38fdd93b0d44`; dispatcher `4ab585d77c1c8a0437571bb3bfea7b9f63ff7897847870825495dce4432ecfad`; host `ee4989e722a0aea878cb0a054a3d8b0b6c59ea822f166f351e9a4becc547d4a7`; migration `edc703bbc38a363056d5488b9a434717869725cd3e2859841009c70e1962a431`; store `c156aab1c843fa83207c67190a5d9552c84deb5120da39a8076f5c2348751b8f`; work-client `379121f249f270df0af1cadf57aa9d60e3e1cb1f7a15be0d4e6147929d9a4e75`; executor `0173685f72b24b2ca5c5f440cd29cfa5001c9919867721107df105f449d6f0d0`; SDK `8c76680596a3a352100278d690d698b23bbafef33cfab60d2cfc92e4d5ac7b0f`.
After SourceVersion/Frontier integration: host `4642704c2086fc373cf225161f8a399f432ef923cceb3582e87ede465c2b51b4`; dispatcher `4ab585d77c1c8a0437571bb3bfea7b9f63ff7897847870825495dce4432ecfad`; WorkService store `99fa96700627366125d9bbde0314e8337050aba85cbac46f6204d2d277c46de1`; models `14afe85c73ff6826c1deab9de4eb4d5380603a567e119ee7a0c0a32e123d8fae`; API models `172c3eec85f7f8ea33928a9212afa11f0999afb67aefdb42af9d042467f4ffd7`; migration 0025 `89c21a112e26f95f46baf61fe4d12e396e5c3a2d92c1676b4ba2e90cf9304790`; TS wire `7829717d55b23958cb8b1db37c02522104795aedab10fe4867383eaaef894cd4`; contract `09f48931f1dad732939fdd89ab08a9ec7cb6148179d6f60e3a4edf4ee27fbce3`.
Correct current WorkService store hash: `99fa96700627366125d9bbde0314e8337050aba85cbac46c6204d2d277c46de1`.
Final current host hash after audit/frontier dispatch: `4a1ae8cef0f0b5a09192475782a27c306508a3cae1982f0574313ebfde99ca58`.

## Implementation boundary

`dispatchNativeStage` compiles stage context, hashes the full delivered task plus the separate context block, preflights the exact allowlisted route, reserves in WorkService, awaits WorkService handoff from `onFirstChatDispatch` immediately before the first provider request, and settles the full native outcome. A failed handoff response is reconciled as interrupted so response loss cannot become proof of no delivery. Context is task data inside `<stage_context_data>`; it is never passed through the system-prompt context field.

The host uses this dispatcher for qualified plan + implement routes at `/execute`. If neither route is installed, existing execution admission remains available for compatibility. A partially qualified pair blocks rather than mixing native and owner-driven authorities.

## Remaining shared work

Context owner must correct Enola `SnapshotRef.tree_sha` commit/tree provenance and add SourceVersion/candidate association migration 0025. Root must obtain independent Kimi and required Fable reviews on this same candidate before claiming readiness. Runtime qualification still needs a disposable four-role journey and restart/response-loss DB evidence.
