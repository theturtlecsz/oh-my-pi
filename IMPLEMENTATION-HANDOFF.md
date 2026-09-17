# Provider preflight reconciliation (2026-09-17)

Accepted source-development commit `7f38a8b701` adds WorkService-owned reconciliation for uncertain dispatched native-stage preflight intents. Migration `0032`, Python service/store/API contracts, generated schemas, TypeScript work client, and real PostgreSQL tests bind original transport and logical request identity to provider observations. Indeterminate evidence preserves uncertainty; completed, failed, and confirmed-absent evidence settles through serialized authoritative transitions.

Contract digest: `a4da1fef0a7b538e0f31d64246af2997ea44210bc7ffd173fc1b46ee141947d0`. Frozen repaired diff: `bf480302ef03ddacb7f996e3e61f9cccfb8fa9ebf469f5ecdac356e996a33a58`. Deterministic suites pass, and fresh Kimi K3 256K re-review returned PASS after one timezone-awareness repair. Production approval remains unchanged.

Next bounded work: implement provider-specific lookup adapters and qualify exact operation UUID, workspace header, contract identity, provider-account correlation, and redacted diagnostics without allowing reconciliation evidence to authorize operations. Continue afterward through qualified rate cards and complete reserve/handoff/settle recovery. No installation, deployment, native acceptance, or Q36 action occurred. [Full disposition](/home/thetu/.codex/workflows/economy/artifacts/omp233-shared-job-inventory-20260916/provider-preflight-reconciliation-20260917/REVIEW-DISPOSITION.md).

# Native tier route/profile slice

Luna-authored isolated candidate. Combined and Enola trees untouched.

Astra correction after Fable amendment: no invented fallback-grant prerequisite. User authorization already permits Luna fallback; route preflight records requested/resolved/served identities and fallback reason, while WorkService remains authority for the launch and outcome.

Implemented:

- `session-system/extensions/workflow/native-stage-profile.ts`: versioned exact route tuple profiles for plan, implement, frontier, audit. Implement primary is `google-antigravity/gemini-3.8-flash:high`; only fallback is `openai-codex/gpt-5.6-luna:high`. Plan/frontier require exact `anthropic/claude-fable-5-1:high`; audit requires exact `kimi-code/k3:high`. Matching checks provider, id, API, thinking mode, supported effort and wire id. Gemini 3.7 and aliases cannot satisfy primary.
- `session-system/extensions/workflow/auditor-runner.ts`: generalized `prepareNativeStageRunner` uses the profile route and requires immutable audit route/policy binding. Production construction is owned by `native-stage-dispatch.ts`; the legacy direct audit wrapper was removed after host migration.
- `session-system/agents/{planner,implementer,frontier}.md`: planner/frontier expose read/grep/glob/lsp only; implementer exposes read/grep/glob/lsp/edit/write and no shell/task/grader access. Host owns deterministic checks.
- `session-system/tests/native-stage-profile.test.ts`: exact Gemini3.8, Luna fallback, Gemini3.7 rejection-as-primary, missing Fable, and exact Kimi routes.

Validation: 83 existing auditor tests pass, 4 profile tests pass, biome check passes, `tsgo -p session-system/tsconfig.json --noEmit` passes. No provider calls, WorkService mutations, commits, or MASTER edits.

Successor r1 corrections now present: native role defaults map plan/implement/frontier/audit to planner/implementer/frontier/auditor; preflight retries only the statically allowlisted implement Luna route after missing credentials or probe failure; cancellation aborts before route work; subprocess model fallback chains are disabled and reported served identities are checked against selected route. `nativeStageWriteRoots` is forwarded through executor/session/tool boundaries and `enforceNativeStageWrite` rejects traversal and symlink escapes before write routing. Prepared stage context is forwarded through the same runner boundary.

WorkService ownership slice now adds migration `0024_native_stage_launches.sql` plus reserve, handoff, settle, cancel, and restart-reconcile command models, service scopes, store transitions, workflow readback, and generated schemas. Stage rows retain prepared context/task digests, requested/resolved/served route identity, fallback reason, timestamps, status, and outcome digest. Contract hash after generation: `63f66b1f94f4c0002f860fad4c61072451497f24650a99d3f38ce8425faf775c` (run `python -m omp_work validate` for local no-approval validation; disposable approval still required for migration bootstrap).

Latest checks: native runner/auditor 88 pass / 0 fail / 485 expectations; native write guard 1 pass; native stage contract pytest 3 pass; selected existing pytest 29 skipped by PostgreSQL marker; repository `bun check` exits 0 with three pre-existing optional-chain warnings. No provider calls, live approvals, commits, or MASTER edits.

Next implementation slice remains required: qualify exact Gemini3.8/Fable5.1 through authenticated supported routes; add WorkService stage-launch migration/commands and durable budget/restart state; wire planner/implementer/frontier through the existing host boundary and shared stage-context hook; then fresh Kimi and blocking Fable frontier review plus one real complete journey.

## Recovery delta (2026-09-15)

Fresh recovery receipt: [native-stage-integration-r1-recovery-receipt.md](native-stage-integration-r1-recovery-receipt.md). It supplements prior handoff/evidence and does not rewrite `CURRENT-HANDOFF.md`.

Base for composition is native tree commit `1b78b801ed`. Combined-r72 and context source have different bases; merge native changes path-by-path and preserve concurrent edits. Changed-path manifest for this recovery delta:

- `session-system/extensions/workflow/native-stage-dispatch.ts`
- `session-system/extensions/workflow/host.ts`
- `session-system/extensions/workflow/git.ts`
- `session-system/extensions/workflow/knowledge-bridge.ts`
- `python/omp-work/src/omp_work/v1/store.py`
- `session-system/tests/native-stage-dispatch.test.ts`
- `python/omp-work/tests/test_workflow_service.py`
- `native-stage-integration-r1-recovery-receipt.md`

Dispatcher recovery invariant: after handoff response loss, WorkService launch is reconciled to `interrupted` and never settled from `run.started`. Reserve replay reuses settled outcomes; handed-off replay preserves uncertainty without provider replay or automatic reconciliation; request identity includes resolved route. A lost reservation response is recovered by matching durable `request_sha256` plus `tool_call_id` before retry. Same-host dispatch holds existing process-owned `withFileLock` through executor completion. Fallback launches record `primary route unavailable or native transport preflight failed`. Session start does not reconcile native rows without proof of owner death; live-host safety takes precedence, and a bounded two-host liveness probe remains required. Frontier repeated-`NEEDS_FIX` counting includes current Kimi launch. Host derives `candidate_tree_sha` from the frozen commit's `^{tree}` object and never aliases candidate payload hash; missing test-double Git identity is omitted so production WorkService can refuse unverifiable attempts. Current hashes: dispatcher `705d11fae1dc7a0a608a2f748ea03c2dc431e67c7b75f2aeb16b6b5a709daece`; host `c74e113a4f21e55e8e4532d250f826e36a4e6d53e0432e88ed9c015f9f9e46b8`; Git `0e1fed06434da4c42842057731128d574c402dbd0457302bfdbea36ec927f421`; knowledge bridge `a776bb67e0c07e4e77c47e0abcd5b34bd26eff511eee31ea8dcb08597246690a`; WorkService store `1f3b8a5c35f5d9461b82dcac5a498bc29874b9839d6a3783dcd7b130a4e88888`; dispatch tests `ae6f11660ca491cfe4021a0ec46dec842a1ac3109600c8c0804c7ed41e56aafc`; knowledge tests `5c87844b6628a8da6feac12cea140dacc8bd926554f7145032a43c3cb3266e40`; WorkService tests `ee197a79002a5a69ef3fd354ead9dbbff07547ae630caf135a15a7f905344844`.

Validation with `/home/thetu/.codex/omp-completion/runtime/bun` (Bun 1.4.0): native dispatch/profile tests = 9 pass; knowledge bridge = 61 pass; OMP-246 lifecycle = 14 pass / 3 TODO; `bun check` passes with three pre-existing optional-chain warnings. Python native contract = 3 pass; native WorkService integration = 2 pass; full WorkService integration = 112 pass.

## Shared SourceVersion handoff

The source/context owner can consume the existing `associateCandidateSource` transport without a new scheduler or ledger. Required association tuple is:

`candidate_id`, `work_id`, `revision_id`, `repository_id`, `source_version_id`, `snapshot_id`, `base_commit`, `analyzed_commit`, `tree_sha`, `source_manifest_sha256`, `snapshot_manifest_sha256`, `content_sha256`, `association_sha256`, `producer`, `producer_receipt_sha256`.

Host rules: derive `tree_sha` from the exact frozen commit with `git rev-parse <commit>^{tree}`; keep it distinct from candidate payload hash, source manifest hash, and snapshot manifest hash. `base_commit` is the grant baseline; `analyzed_commit` is the source actually analyzed; absent dirty-source tree identity stays null. Candidate binding is current finalized candidate plus exact work/revision/repository. Context compile must select the returned `snapshot_id` and manifest domain, and native task identity remains `{workspace, repository, work, revision, candidate, stage}`. Current host now supplies actual Git tree identity when available and omits the field when test doubles provide no real commit, allowing WorkService to refuse unverifiable production attempts without restoring the old hash alias.
