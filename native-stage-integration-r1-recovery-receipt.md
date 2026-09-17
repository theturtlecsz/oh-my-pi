# Native stage integration r1 recovery receipt

Date: 2026-09-15 UTC

## Composition identity

- Native base: `1b78b801ed`.
- Combined-r72 and context source use different bases; composition must be path-by-path.
- No commit, push, approval mutation, deployment, or shared-service restart performed.

## Applied source changes

- `session-system/extensions/workflow/native-stage-dispatch.ts`: WorkService reserve response-loss lookup by `(request_sha256, tool_call_id)`; settled replay; handed-off replay preserves uncertainty without provider replay or automatic reconciliation; resolved route included in request identity; same-host dispatch is serialized by existing OS-backed file lock.
- `session-system/extensions/workflow/host.ts`: candidate close attempt now carries Git `^{tree}` identity when the frozen commit is available; candidate payload hash is never used as tree identity; repeated frontier predicate includes current Kimi launch.
- `session-system/extensions/workflow/git.ts`: exact commit tree identity helper.
- `session-system/extensions/workflow/knowledge-bridge.ts`: bound source association snapshot ID is carried into context compile and source observation identity.
- `python/omp-work/src/omp_work/v1/store.py`: candidate tree identity is retained as an independent binding and is no longer falsely compared to candidate payload hash.
- `python/omp-work/src/omp_work/v1/store.py`: competing active reservations for one `(grant, work, role, attempt)` identity are refused before insert.
- `session-system/tests/native-stage-dispatch.test.ts`: response loss, settled replay, handed-off replay, and durable workflow lookup regressions.
- `session-system/tests/knowledge-bridge.test.ts`: bound snapshot compile request regression.
- `python/omp-work/tests/test_workflow_service.py`: independent tree identity and exact settlement replay integration.
- `python/omp-work/tests/test_workflow_service.py`: competing distinct request/tool ID race is refused; exact settlement replay remains idempotent.

## Safety boundary

Session start does not reconcile all reserved/handed-off rows. WorkService currently has no process/lease owner for native stage rows; `owner_id` is shared across hosts. Reconciliation from session start could interrupt a live provider in a second host. Restart recovery is therefore limited to deterministic request replay: reserved rows may resume only under the existing same-host process lock, settled rows replay their stored outcome, and handed-off rows remain uncertain without provider replay or automatic reconciliation. WorkService refuses distinct active stage races. A two-host liveness qualification still needs persisted owner/process epoch proof before implementation can safely add automatic owner-loss reconciliation.

## Verification

- Pinned runtime `/home/thetu/.codex/omp-completion/runtime/bun`, Bun 1.4.0.
- Native dispatch/profile tests: 9 pass.
- Knowledge bridge tests: 61 pass.
- OMP-246 lifecycle: 14 pass, 3 TODO.
- Python native contract: 3 pass.
- WorkService native integration: 2 pass.
- Full WorkService integration: 112 pass.
- `bun check`: pass with three pre-existing optional-chain warnings.

Current source hashes are recorded in `IMPLEMENTATION-HANDOFF.md`; this receipt does not replace prior evidence or `CURRENT-HANDOFF.md`.
