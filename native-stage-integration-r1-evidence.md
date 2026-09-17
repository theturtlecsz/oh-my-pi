# Native stage integration r1 evidence

Date: 2026-09-15

## Checks

- `bun check`: pass; only three pre-existing optional-chain warnings in `packages/coding-agent/src/session/agent-session.ts`.
- `bun test session-system/tests/auditor-runner.test.ts session-system/tests/native-stage-profile.test.ts packages/work-client/test/client.test.ts session-system/tests/native-stage-dispatch.test.ts`: 105 pass, 0 fail.
- `PYTHONPATH=python/omp-work/src python -m pytest -q python/omp-work/tests/test_native_stage_contract.py`: 3 pass.
- `OMP_WORK_POSTGRES_INTEGRATION=1 PYTHONPATH=python/omp-work/src python -m pytest -q python/omp-work/tests/test_workflow_service.py -k native_stage_launch`: 1 pass against fresh native PostgreSQL.
- Full `test_workflow_service.py` under the same fresh PostgreSQL integration setup: 111 pass, 0 fail.

## Live transport qualification

- Gemini exact route: `/home/thetu/.local/bin/agy-omp --project oh-my-pi --model gemini-3.8-flash-high --effort high --mode plan --disable-slash-commands --output-format stream-json --print ...`; result `SUCCESS`, one turn, exact model `gemini-3.8-flash-high`, response `OK`. Read-only probe; no authoring.
- Fable exact route: `claude -p --model claude-fable-5-1 --effort high --output-format json --no-session-persistence ...`; result `success`, model usage includes `claude-fable-5-1`, response `OK`. Read-only probe.
- Kimi exact route: `kimi -m kimi-code/k3 -p ... --output-format stream-json`; result `OK`, session `session_bcbe8662-2cf4-4160-9b19-fe8d7b1de01f`.
- Pinned qualification runtime path `/home/thetu/.codex/omp-completion/runtime/bun1.4` is absent; existing pinned runtime `/home/thetu/.codex/omp-completion/runtime/bun` exists. No authoring run was attempted through the absent path.

## Actual bounded four-role journey

Fresh disposable repository: `/tmp/omp-native-journey-r1.AtusvZ`. Base commit `a0314a992b7d2406e11d6399f4586b13e50ba5a3`, base tree `31740e59c15718740c0e3eeea7f688411878a233`, one sealed path `hello.txt` containing `seed`.

- Plan: live Fable `claude-fable-5-1:high` returned a structured plan for the frozen identity and only `hello.txt`.
- Implement: live Gemini `gemini-3.8-flash-high:high` with `--new-project`, sandbox, and `accept-edits` changed only `hello.txt` to `native-implemented`; the model attempted unrelated generated docs, which were detected and removed from the disposable tree before candidate sealing.
- Candidate seal: commit `1dd75b4a83206f3b030c3a5667193c4b70e288ba`, tree `6c40e122ad4b252779367e6e5e2453ba98258631`, clean status, diff only `hello.txt`.
- Audit: live Kimi `kimi-code/k3:high` returned PASS with zero blocking findings, conditional only on host hash verification.
- Frontier: fresh live Fable `claude-fable-5-1:high` returned PASS with no blocking findings and the same non-blocking tree-hash re-verification condition.

The journey exercised real provider delivery and exact frozen identity. The model-generated docs incident confirms why sealed-path checks must remain host-enforced; no unrelated file remained in the candidate.

Follow-up Fable frontier with read access to `/tmp/omp-native-journey-r1.AtusvZ/hello.txt` independently verified candidate/base commits and trees, file bytes, clean status, and one-file diff; verdict PASS, zero blocking findings. The reviewer runtime's startup generated an untracked `docs/` fixture outside the candidate; it was removed from the disposable repository and clean status/tree hash were rechecked. Earlier Kimi direct review was read-only and returned PASS with a conditional hash-verification note.

## Behavioral changes evidenced

- Typed TS commands now mirror staged Python `reserve_stage_launch`, `handoff_stage_launch`, `settle_stage_launch`, `cancel_stage_launch`, and `reconcile_stage_launch`.
- Native stage context is inserted into the user task input as bounded data; runner system prompt receives no retrieved context. `task_sha256` covers the full delivered task, while `prepared_context_sha256` covers the context block separately.
- First provider dispatch awaits durable handoff. Handoff response loss routes to WorkService reconciliation, preserving an uncertain delivery state.
- WorkService reservation checks revision/candidate/attempt/grant identity and role phase before minting a launch. Duplicate request identity replays the existing launch.
- Migration 0025 and `associate_candidate_source` bind current finalized candidates to distinct source-version, base-commit, analyzed-commit/tree, source-manifest, snapshot-manifest, and content identities; forged association hashes and stale candidates are refused.
- Native audit uses the stage lifecycle when Kimi is qualified; escalation paths run the Fable frontier stage after Kimi verdicts when the bounded predicate matches. Frontier BLOCKED/NEEDS_FIX remains blocking.
- `/execute` invokes plan then implement natively only when both exact routes are available; it fails closed on partial native qualification.

## Not yet evidence

No live provider call, no live approval mutation, no commit, and no MASTER edit were performed. Four-role provider journey, WorkService restart integration, candidate SourceVersion association, and independent Kimi/Fable reviews remain external gates.

## Recovery delta (2026-09-15)

The candidate was recovered at base commit `1b78b801ed` (native tree), while combined-r72 is based on a different tree and must be merged path-by-path. No whole-file replacement from combined-r72 is valid.

Changed paths in this recovery delta:

- `session-system/extensions/workflow/native-stage-dispatch.ts`
- `session-system/extensions/workflow/host.ts`
- `session-system/tests/native-stage-dispatch.test.ts`
- `python/omp-work/tests/test_workflow_service.py`

The dispatcher now leaves a provider-delivered launch in WorkService `interrupted` after a lost handoff response; it does not attempt settlement after reconciliation. Reserve replay reuses settled outcomes and preserves handed-off uncertainty without provider replay or automatic reconciliation. Same-host dispatch uses the existing process-owned file lock through executor completion. Fallback reservations now carry the explicit reason `primary route unavailable or native transport preflight failed`. Session start does not reconcile native rows without proof of owner death; a two-host liveness qualification remains required. Frontier escalation counts the current Kimi launch when evaluating the repeated `NEEDS_FIX` predicate. Host derives `candidate_tree_sha` from frozen commit `^{tree}` and never aliases candidate payload hash. Existing evidence above remains historical and unchanged.

Validation after recovery with pinned Bun 1.4.0: native dispatch/profile tests 8 pass, 0 fail; knowledge bridge tests 61 pass, 0 fail; native WorkService integration 2 pass; full WorkService integration 112 pass; repository `bun check` passes with the same three pre-existing optional-chain warnings.
