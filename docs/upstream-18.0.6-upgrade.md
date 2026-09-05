# Upstream 18.0.6 upgrade handoff (OMP-156)

## Pinned refs

- Merge base: `ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472`
- Fork (frozen main): `79b037e420943010e03727d7cdb22f05e64507b7`
- Upstream target (18.0.6): `b4e8e856ad40294167679a3f88417c07429fe59b`
- Rollback branch: `rollback/omp-156-pre-18.0.6` at the frozen fork commit; worktree `/home/thetu/oh-my-pi-omp-156-rollback` (natives 17.3.2, wrapper smoke `omp/17.3.2`).
- Integration branch: `upgrade/omp-156-18.0.6`; worktree `/home/thetu/oh-my-pi-omp-156`.

## Live-link manifests

- Before cutover: `~/.omp/agent/state/omp-156-live-links.before.tsv` — recorded 2026-08-26 from the committed `install.sh --print-manifest` plus the `command -v omp` target: 27 lines, extension root a real readable directory, every managed link resolving into `/home/thetu/oh-my-pi`, only `rules/linear-plan.md` absent (expected).
- Rollback set: `~/.omp/agent/state/omp-156-live-links.rollback.tsv` — recorded at cutover (§6).
- Primary set: `~/.omp/agent/state/omp-156-live-links.primary.tsv` — recorded at cutover (§6).

## Inventory

- Source manifest: `docs/upstream-18.0.6-fork-sources.tsv` (869 records: 815 hunks + 52 binary + 2 meta; frozen from `git diff --unified=0 --no-renames ae2d3d6ea16a..79b037e42094`).
- Fork matrix: `docs/upstream-18.0.6-fork-matrix.tsv` (one surface per changed path; 378 rows, 50 shared).
- Changelog ledger: `docs/upstream-18.0.6-changelog.tsv` (129 entries, versions 17.3.3–18.0.6 from pinned-target package changelogs).
- Verifier: `bun scripts/verify-upstream-handoff.ts --record docs/upstream/baseline.json` (`--allow-pending` pre-merge only).

## Predicted conflict resolutions (17 paths)

| Path | Exact merged resolution | Focused proof |
|---|---|---|
| `.config/nextest.toml` | Union upstream default/CI failure output, fail-fast, retry, and slow-timeout settings with fork OMP-127 jobspec process-group kill override; keep the override as an additional profile.default rule. | `cargo nextest run --workspace --exclude brush-core --status-level=fail --final-status-level=fail` |
| `packages/ai/src/providers/google-gemini-cli.ts` | Keep fork fetchWithRetry, planning-leak stripping, and visible-content accounting; adopt upstream hasThinkingOutput/thoughtOnly semantics so a thought event commits the endpoint, skips duplicate empty retries/failover, becomes valid silence only for acceptEmptyResponse, and otherwise raises empty-output for session-level recovery. | `bun test packages/ai/test/google-empty-response-retry.test.ts` |
| `packages/ai/test/google-empty-response-retry.test.ts` | Keep Bun imports and fork retry/planning-leak cases; adopt upstream one-call thought-only recovery, advisor thought-only no-failover, emitted thinking events, and AIError.Flag.EmptyResponse assertions. | `bun test packages/ai/test/google-empty-response-retry.test.ts` |
| `packages/catalog/src/models.json` | Never hand-resolve; merge provider/catalog sources, run generator, and accept generated output only. | `bun run gen:models && git diff --exit-code -- packages/catalog/src/models.json` |
| `packages/catalog/test/variant-collapse.test.ts` | Keep fork Antigravity Gemini 3.7, Devin GLM-5.2, alias, discovery, and effort-routing cases; add upstream CURSOR_VARIANT_COLLAPSE_TABLE, dynamic Cursor Grok tier families/aliases, defaultSupportedEffort, and revised Antigravity-before-CCA provisioning cases. | `bun test packages/catalog/test/variant-collapse.test.ts` |
| `packages/coding-agent/src/advisor/advise-tool.ts` | Adopt upstream deferred-note queue/flush, duplicate escalation, and blocker steering during interrupt-immune turns; retain @oh-my-pi/omptype, required OMP-55 category, severity-rank dedupe, delivery-channel routing, and kCursorExecResolved quarantine. | `bun test packages/coding-agent/test/advisor/advisor.test.ts` |
| `packages/coding-agent/src/extensibility/plugins/legacy-pi-compat.ts` | Adopt upstream SQLite parse cache (getLegacyPiExtensionCacheDbPath), cached source analysis, package-import exclusion sentinel/path validation, and export-condition fixes; retain astHasCommonJsModuleScope, omp-legacy-pi-bundled:, Bun createRequire rewriting, compiled self-package roots, and fork compatibility exports. | `bun test packages/coding-agent/test/extensibility/legacy-pi-*.test.ts` |
| `packages/coding-agent/src/modes/components/status-line/component.ts` | Keep fork StatusLineComponent — never import upstream FooterDataProvider; integrate upstream composer styles, usage-window filtering, compaction boundaries/context line, standalone layouts, autocomplete/speculation indicators, quota/token metrics, and render revisions while preserving inline extension slots, VCS cache, loader lifecycle, context cache, and active-time meter. | `bun test packages/coding-agent/test/context-consolidation.test.ts packages/coding-agent/test/collab/guest-idle-reconciler.test.ts packages/coding-agent/test/status-line-overflow.test.ts` |
| `packages/coding-agent/src/session/agent-session.ts` | Re-fit all upstream session-loop additions — provider response metadata, preferred dialect, capability reset, code/extended-context listeners, checkpoint notice, image entries, and resume command — into fork AgentSession; retain taskDepth, cancellable plan_approved, setSessionName precedence, SQLite multi-credential auth, ToolSession/BUILTIN_TOOLS, Bun runtime, and persistence API. Use LSP references for every moved signature and migrate all callers. | `bun test packages/coding-agent/test/agent-session-*.test.ts packages/coding-agent/test/acp-agent.test.ts` |
| `packages/coding-agent/src/session/session-advisors.ts` | Integrate upstream Tokenizer/estimateTranscriptTokens, AdvisorLoopGuard, transactional recorder turns, chained retry fallbacks, text-ambiguous overflow handling, compaction method order, terminal-yield advice preservation, subscription detection, and resume-cost barrier; retain fork SessionAdvisorsHost, YieldQueue, SecretObfuscator, channel routing, and auto-resume suppression. | `bun test packages/coding-agent/test/agent-session-advisor-suppression.test.ts packages/coding-agent/test/agent-session-message-pipeline.test.ts packages/coding-agent/test/agent-session-retry-fallback.test.ts` |
| `packages/coding-agent/test/advisor/advisor.test.ts` | Keep Bun/omptype fixtures and fork category/quarantine cases; adopt upstream deferred-note flush/dedupe/escalation, blocker-immune-turn routing, Tokenizer-based incoming-message maintenance, and onTurnAbandoned assertions. | `bun test packages/coding-agent/test/advisor/advisor.test.ts` |
| `python/robomp/tests/test_persona.py` | Retain fork thread/login/PR-review cases; adopt upstream ReleaseTaskContext, release system/kickoff/follow-up prompts, retag contract, release todo phases, and relaxed directive/push-refusal wording assertions. | `python3 -m pytest -v python/robomp/tests/test_persona.py` |
| `python/robomp/tests/test_queue_cancel.py` | Retain every fork cancellation, operator-error, and reap-order case; add upstream _StubSandbox.reclaim_workspace_caches and reclaim_all_caches methods exactly, with no production-semantic replacement. | `python3 -m pytest -v python/robomp/tests/test_queue_cancel.py` |
| `python/robomp/tests/test_queue_dispatch.py` | Retain fork issue/PR routes and synchronize no-op; add upstream cache-reclaim stub methods and workflow_run/completed dispatch to tasks.handle_release_ci(payload, attempts). | `python3 -m pytest -v python/robomp/tests/test_queue_dispatch.py` |
| `python/robomp/tests/test_queue_shutdown.py` | Retain fork drain-then-kill, cancel-hook/hookless, _shutdown_cancelled, and non-root semaphore cases; add upstream cache-reclaim methods to _StubSandbox. | `python3 -m pytest -v python/robomp/tests/test_queue_shutdown.py` |
| `python/robomp/tests/test_retry.py` | Retain fork backoff, jitter, claim gate, manual requeue, retry budget/state, and reap cases; add upstream cache-reclaim methods to _StubSandbox. | `python3 -m pytest -v python/robomp/tests/test_retry.py` |
| `python/robomp/tests/test_worker.py` | Retain fork _FakeRpcClient, resume, XDG/permissions, slot groups, timeout, completion/dirty-state, and NativesCache cases; adopt upstream exact terminal-tool reminder set plus release fresh/resumed prompt routing and repeat-until-retag-or-abort cases. | `python3 -m pytest -v python/robomp/tests/test_worker.py` |

Re-run the read-only `git merge-tree` preview immediately before merging; any change to the conflict set is recorded here.

### Merge-preview re-run (pre-merge)

Re-run 2026-08-26 from integration commit `089daf02e4` (pre-merge tooling committed):
`git merge-tree --write-tree --no-messages HEAD b4e8e856ad40294167679a3f88417c07429fe59b`
predicts textual conflicts in **19** paths — the 17 planned paths plus two caused by the
pre-merge tooling commit itself:

- `package.json` — fork `test:scripts` gained the verifier test on a line upstream also
  changed (upstream adds `scripts/ci-test-ts.test.ts` to the same script). Resolution:
  union both — `test:scripts` runs ci-test-ts, release, musl, publish, build-binaries,
  and verify-upstream-handoff tests.
- `.github/workflows/ci.yml` — fork replaced the `bun test scripts/release.test.ts`
  workaround with `bun run test:scripts` on lines upstream also touched. Resolution:
  keep the fork's full `test:scripts` invocation inside upstream's revised workflow text.

Both additions are unions of the planned §3 tooling changes with upstream's 18.0.6 text;
no new behavioral surface is involved. The other 17 paths and their resolutions are
unchanged from the table above.

## Generated and binary handling

- `packages/catalog/src/models.json`: never hand-resolved; provider/catalog sources merged first, then `bun run gen:models`; the generated output is committed verbatim (merge commit `6c9bd5c9b5`, regeneration `a864f00dae`). The generator merges **live provider discovery**, so regenerations pick up newly published upstream models/prices; the committed file is the generation adjacent to the merge, and `bun run gen:models && git diff --exit-code -- packages/catalog/src/models.json` was a fixpoint immediately after `a864f00dae`. Two upstream data-pinning tests were re-pinned to the accepted generation (`6e11745d0c`): OpenAI cut `gpt-5.6-sol` base rates to $4/$20/0.4, and MiniMax now publishes a 512K output ceiling for MiniMax-M3.
- `bun.lock`: regenerated with plain `bun install` after package-manifest conflicts were settled; the regenerated lockfile is committed in the merge.
- 52 binary source records (fork-only assets — fonts, images, fixtures) ride the merge unchanged; retention is proven by the frozen-manifest recomputation (source set equality) in every verifier run.
- `packages/coding-agent/src/export/html/tool-views.generated.js` is untracked output of `gen:tool-views` (runs via `prepare`); the fork's collab-web tool views legitimately change it, so the upstream HTML-export template test's hardcoded byte/sha pin was re-fitted to a derived expectation with an asset-floor omission guard (`c62bd75784` + follow-up).
- `python/omp-work/src/omp_work.egg-info/{PKG-INFO,SOURCES.txt}` are tracked setuptools artifacts that a host-repair `pip install -e` regenerated; frozen-main bytes were restored (`7ab3089a5f`) — fork-only paths ride the merge unchanged.

## Host drift and repairs (2026-08-26)

- The workstation's system python moved to 3.14 and its site-packages were wiped (pytest, robomp/omp-rpc editable installs, omp-work deps all gone; `bun run test:py` failed identically on frozen main — pre-existing host drift, not candidate damage). Repairs: user-site editable installs rebound to the **frozen primary** checkout (`pip install --user --break-system-packages -e /home/thetu/oh-my-pi/python/omp-rpc -e '/home/thetu/oh-my-pi/python/robomp[dev]'`), and `test:py` was made checkout-hermetic via `uv run --project … --extra dev` per package so gates always test the checkout under test, never live host bindings. Post-cutover, primary == candidate, so the live binding stays correct without further action.
- Upstream's new plugin-precedence discovery test and the startup-changelog PTY smoke both fail on the pristine `b4e8e856ad` tree on this workstation (real-`HOME` plugin leak; `CURRENT_SETUP_VERSION` bump launching the animated setup splash inside the smoke window). Both were made hermetic on the candidate (`0caec851de`, `eb795f45e8`) — environment sensitivity, not candidate behavior drift.

## Known pre-existing failures (not candidate-caused)

- `agent-session-retry-recovery.test.ts › collapses exhausted retries…` fails intermittently when the full `agent-session-*.test.ts + acp-agent.test.ts` glob runs in one bun process — reproduced byte-identically on frozen main (`79b037e4`, run log `/tmp/omp156-as-baseline.log`) and passes standalone and in gate 7's chunked partitions on both trees. Cross-file state leak predating OMP-156; not waived silently, recorded here.

## Gate results (driver ancestor pass at `eb795f45e8`, exit 0)

| Gate | Command | Result |
|---|---|---|
| 1 | fixed verifier `--allow-pending` | PASS: sources=869 forkPaths=378 shared=50 changelogEntries=129 |
| 2 | `bun test session-system/tests packages/work-client/test scripts/verify-upstream-handoff.test.ts` | 237 pass, 0 fail |
| 3 | `./node_modules/.bin/tsc --noEmit -p session-system` | clean |
| 4 | `bun run check:ts` | clean (biome + workspace checks) |
| 5 | `cargo fmt --all -- --check` | clean |
| 6 | `cargo clippy --workspace --exclude brush-core --no-deps -- -D warnings` | clean |
| 7 | `bun run test:ts` | all buckets pass, 0 fail |
| 8 | `bun run test:scripts` | 62 pass, 0 fail |
| 9 | `bun run test:py` | omp-work 31 passed (85 postgres-gated skips), omp-rpc 69 passed, robomp 664 passed |
| 10 | `cargo nextest run --workspace --exclude brush-core …` | 2359 passed, 5 skipped |
| 11 | `OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke` | `work-service-candidate-smoke: PASS` (88 loopback requests) |

The driver's tracked-tree dirty check passed (`update.sh: all gates passed at eb795f45e8`). The final
pre-cutover driver rerun executes on the sealed candidate; its outputs land in the OMP-156 Work Ledger
handoff receipt.

Whitespace proof: `git diff --check 79b037e420943010e03727d7cdb22f05e64507b7..HEAD` exits 0.
Upstream ships intentional whitespace in six files (markdown hard-breaks, verbatim vendored
license texts, a spaces-only blank line in a python tool, blank-at-EOF in two proto tables);
those are declared via scoped `.gitattributes` `whitespace` attributes instead of mutating
upstream content, and every other path keeps full check coverage. The fork-side diff against
the target (`git diff --check b4e8e856ad..HEAD`) contributed zero new whitespace errors beyond
fork-generated protobuf files byte-identical to frozen main.

## Cutover and rollback results

Recorded during §6: rollback activation, primary activation transaction, live smokes, and the
compare-and-swap push, with the rollback/primary manifests at
`~/.omp/agent/state/omp-156-live-links.{rollback,primary}.tsv`.

## Appendix A — source records by surface

Each matrix surface (path) with the frozen source records it accounts for:

- .clinerules/caveman.md ← s24299b299011
- .config/nextest.toml ← sb3a3271b562a
- .cursor/rules/caveman.mdc ← s3efabbcbf058
- .github/copilot-instructions.md ← sb520ed578ff7
- .github/workflows/ci.yml ← s55b507004094, s470f96b9da22, sdf388aaed453
- .gitignore ← sa5fc7daa0357, sce7c41948cac
- .windsurf/rules/caveman.md ← sffe6a133fad2
- .work-project ← se75f97efce9b
- AGENTS.md ← s1103f76c4c1b
- WATCHDOG.yml ← s5dbec56f8d40
- bun.lock ← sff7bcc352748, sa4dda95f56e4
- crates/pi-builtins/src/pgrep.rs ← s69da6629a541
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/anomaly-report-4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945.json.gpg ← sa45617ea45f3
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-attachments-0-82baa8dba69aa94f85deaa9054533490b11847669f7bff70c03f99c23d896650.json.gpg ← s216281811cce
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-comments-0-0460a977034fc5d60f68c5a44623632914747f968c7abbf7df43d4426679b032.json.gpg ← s278a3485f7cd
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-initiativeToProjects-0-cf0ab94399adc54a0b51acd63002b6bb81deaa39b81bc4d96635ca72314c070e.json.gpg ← sbbb91e406d58
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-initiatives-0-aba446376378c7eacc8705d693d4303adb71228a6cb2a152de250c19c33f3328.json.gpg ← sbfad82c62bda
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-issueLabels-0-c25af5e31334ba2776101d23a6de12438e8f0ea7d5951317a96a87cb95751bc0.json.gpg ← scefe90d40d68
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-issueRelations-0-1f75db96d0cdc6d731e40b031ae58fc258368296c7c3d3af79e357f2808ded45.json.gpg ← s30d2243ff892
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-issues-0-5dcf113f82ce4d3921cfa6f25b87194d734a443bef166614ac216731f4c47ac6.json.gpg ← s27677fce59a5
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-projectMilestones-0-25841d5bd28951b7569d8c1ddd7c9c343773513e8b671bb13abe98a38d4270ee.json.gpg ← s8697b3d1fa4c
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-projectUpdates-0-e31c03ad4eb7d2ce629ff4b595ef68b88605fbdf1606dc26df5ffe80183f029c.json.gpg ← s4dd0f71467f0
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-projects-0-9e5802fd0f751063f3ed5adfc79cfafaaffb33d8832d31248686e5be4574e5aa.json.gpg ← s40fd9db01c63
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-teams-0-1820149de12392ed153b92f598db40120d575e8fffb8cbf11da96e603a9967e7.json.gpg ← sc6db8209e04c
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/delta-workflowStates-0-fcadda8c16735fdca0bd3c04a853fb01ca7bb882fc8d8c6d096e414260a9a649.json.gpg ← sb0728f45d51d
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/manifest-4ea70db806de4fb0a5cf62da25e7465399d2eb2160e72f08d3ae05ce6ff3d9d1.json.gpg ← s5d1397e1375b
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/privacy-report-b38b66b418fb129209f1c8a1a226eb2a15c5a021f5227644fb5e6597a02333e4.json.gpg ← s82f87a7c1f9c
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/scope-report-24da03b4131572907f1cec73b9141ac214b8ac1cb337edae2d16098cd2cb7aa4.json.gpg ← sd1e99a2a6d88
- data/linear-export/825e3ef4-4094-4b20-97da-64a7c477a306/source-hashes-08826c0c8248bb5048b78068a1f6d5823a68c9dcda3d1350d624632c8a1dace1.json.gpg ← saa4e1dc7e919
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/anomaly-report-4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945.json.gpg ← s9c4d385ae93d
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-attachments-0-cf94ef6b31dcd73d2e6157772d91df43a6a34226e526eba69c5f7b06a0b7b129.json.gpg ← se18a73ff5801
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-comments-0-ac7f801fc8d461a39b5f830496f65c9d6edd144da21b0164649e04d7ef69a7e2.json.gpg ← sa829e3d3ecf4
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-initiativeToProjects-0-8cc08a312d899cdc05f56829ffb96353beb09f8ea673d363253b2aaac218906d.json.gpg ← s46ae8a35c595
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-initiatives-0-5202a87396ffe34612fb8d4bf76663bf684ba8641a64e9f65675fb33ff2bafee.json.gpg ← sd582479a4605
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-issueLabels-0-3f3fd6a09f85340be09acb053863f26ad4a26740eacf2e0cc2ac844576d8478e.json.gpg ← sbd7d8aa8237a
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-issueRelations-0-f46b6419c933f96e75ae15ebe33140749ec3fe4ca149a2f63678fb2878dfcc8e.json.gpg ← s33b2b91455a8
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-issues-0-a0edcb0151046905570fa2ee26f9577173cc7d2334594f672b5d214a6e45a3f2.json.gpg ← s978a2478cf9d
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-projectMilestones-0-a9121db79c36d1c201d72d1566ff613fefb4f152f52694df136f424bf3b058cd.json.gpg ← s490c42f2e8d1
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-projectUpdates-0-30116fcc4e369faade7b3efaff36e94946e5087b8026b52af0127d3493909e1a.json.gpg ← s67caadfbced0
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-projects-0-055a9c1a845c34c43f0d0e7c031e70701aafec8d369fe4122687d0453b9c9012.json.gpg ← s477cd4665602
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-teams-0-2729caf2460169de901f54a08cae538064535040984541c76ddfd5af359e33dd.json.gpg ← se6d6bab9b508
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/delta-workflowStates-0-6e16344a4dc03c2a1c5ce774df0e620cff004661216008c36d0b72b5fde9cfa7.json.gpg ← sace452921b3c
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/manifest-f3f84e1088d336e67d8b57f110b33c1293fc4658fab962d1df8b32a0439cab5a.json.gpg ← sa5f0791a7625
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/privacy-report-b38b66b418fb129209f1c8a1a226eb2a15c5a021f5227644fb5e6597a02333e4.json.gpg ← s86a76d8819ae
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/scope-report-24da03b4131572907f1cec73b9141ac214b8ac1cb337edae2d16098cd2cb7aa4.json.gpg ← s4346fb4bcf48
- data/linear-export/c4e848b6-2e4f-423a-a502-c66e0f37e746/source-hashes-08826c0c8248bb5048b78068a1f6d5823a68c9dcda3d1350d624632c8a1dace1.json.gpg ← sacbec8ddb47a
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/anomaly-report-4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945.json.gpg ← s00efe0310ab9
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-attachments-0-149a51b759ebaffd7b234054b2b8253ebf72dd900669b2d6dc5855f1f1af4fc7.json.gpg ← s752e254fea53
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-comments-0-65b5ac635fdecc3d0f9bc43cd7cf815afc12034fbe47f5358062020c144489bf.json.gpg ← sf53a95424473
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-comments-1-b68428a4c49def7dff3609752d44b729f677bebe73dc89ea764d81707d90362d.json.gpg ← sebd3b07a544a
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-comments-2-f3be8e9dba966b9f3f8d512d2b10ef9d56577d91d0333611507bfabec936843d.json.gpg ← s8223067d8ad0
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-initiativeToProjects-0-66c0b4e2cf3b7ba75b9571884930ee881caf71c45a1a077d9277cdfd68398d98.json.gpg ← s3e52179e3fd3
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-initiatives-0-71154621c2b35f6a342aca67a8878147d15e9e7f0454b069d9cd3b0950cacde0.json.gpg ← s95c6cbfa5c6e
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-issueLabels-0-f3d943e3d853541a15da1544b2422032943c5e6b47001167d572ad3f36652d7d.json.gpg ← s58bceaf61af2
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-issueRelations-0-915702a848cd4fc0bb5a3f652f3379617689b8b90454e872eae3a8e0562ff6ef.json.gpg ← s1957b09e5d8e
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-issues-0-075198295770cb2f280de7ce7bdc54950f10b689973bf0a42bbd1e6cfb40844f.json.gpg ← sb5073fe19808
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-issues-1-d9414e41a400b52a69ab6e6e86c9d37b603e12db6259cee3a8cb8000af9dafa7.json.gpg ← sdf007939084d
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-issues-2-e86a9c8329f17061359df530c0bef8274dd1a8e7a474bfae1577635c134a6e72.json.gpg ← s7d350c55999c
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-projectMilestones-0-e9ad680017408850c3cfe0458ff2ffec741b0e7e1e69acd88267db12c63f7d06.json.gpg ← s37d04604bcc6
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-projectUpdates-0-378f885cc25a6635a37f4d945a606cddf327b8a69f071fe8c11b5ce1ea2ff865.json.gpg ← s17d04562deaa
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-projects-0-36a3a4255bae560f07b6099fcfd41a394d093fa7853a1112a63b07eb089ba394.json.gpg ← s6c0d6be30040
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-teams-0-95c49ffe58f0bd8e2226eb16d6697b661a977207e73b974eaf3cabc39cbb2f45.json.gpg ← sa825fd1c9396
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/baseline-workflowStates-0-7afc03c68a86faa1e3b10631983a5096b2032ef8f4ed146d01c1574e176ba361.json.gpg ← s9ee271ca7425
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/manifest-6d6c61bde82a7af5f6554aef8d342a321947809f62f2128b5de849c2fab25dff.json.gpg ← sb6e05d77c5df
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-attachments-0-a448ebd726705799eb463de552ae3288c953b0f3025965bafde288408e80db05.json.gpg ← sde46778a76e8
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-comments-0-f94b4f28ca4a7db6733eae3e03a4b8e11c2860ba68816ee4f21c63e1f60c2c5d.json.gpg ← sfa6a57065143
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-initiativeToProjects-0-4164313e4f2d517bbe72d631da3d470b24b4b53a368b7db0ee74c74665992d7e.json.gpg ← s348b9709c766
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-initiatives-0-14c9204758771cdba21b563ff007a5cc0c6a2c775feed80de59233124652f9b8.json.gpg ← sfe61a1569326
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-issueLabels-0-62ab374610ba9a6c5404b23f449f5a59830bd36ad2a86f17cc5bd74a2e5dca64.json.gpg ← s0ba00e030bd4
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-issueRelations-0-8dbd3c56bff8e5ff977acf9b4343c498c36cfefa020bb0ee8e4455b9599dc71a.json.gpg ← s876789f82fc6
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-issues-0-a09f606588c06c285f6edc95b7eefa21293959ee24c509c0dd2c9ec582b0f773.json.gpg ← s5d3f45852dfc
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-projectMilestones-0-e5cef9cd4c634b1a0e2734ec8716eca87ca689fbb1395a32d245e9c0ab1b63da.json.gpg ← s29bf93dc1757
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-projectUpdates-0-5dcd73a84f7458635ff79c093a339993d3967d5767213cc441cf3e2c4890eda6.json.gpg ← s52cd3bb5d39b
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-projects-0-9c47e13e5e4464aa6f3ac1deec947c2365af29a58ea5a0618ff31e33ab962e7b.json.gpg ← s826e1b93b925
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-teams-0-6751fb58f34b33448cbc24e56bd3dee821110a8fc2f230ea8f0bc2a2b196ece1.json.gpg ← sab2ccd737be2
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/overlap-workflowStates-0-a4d526d5a0d9d0f3eeb60877dcc23c363c9a3db4293525d4d4a5f71229b22a9a.json.gpg ← s0e71bfab2c03
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/privacy-report-b38b66b418fb129209f1c8a1a226eb2a15c5a021f5227644fb5e6597a02333e4.json.gpg ← sb3652d863470
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/scope-report-1c517d9a6496b70fec59735c949396b26ed50138425bd110574f477bb155e414.json.gpg ← s1fc3cd4dfcb7
- data/linear-export/f6cba355-c75a-4235-8888-13cb6b2ecd32/source-hashes-08826c0c8248bb5048b78068a1f6d5823a68c9dcda3d1350d624632c8a1dace1.json.gpg ← s4dcb18a78595
- data/linear-export/finalize/report-124891316e3bbaef0358ec60242a12e8352b42aad964d5748e39833db6c8f4f8.json.gpg ← sa8bd2211a593
- docs/extensions.md ← s1395529f30bc
- docs/work-ledger-operations.md ← s883af3405d4e
- infra/work-ledger/install.sh ← s8ff3c7c70087
- infra/work-ledger/linear-import-map.json ← s5159e5ac3019
- package.json ← s2f1b44d02c8a
- packages/ai/CHANGELOG.md ← sd7580da4f7ef
- packages/ai/src/providers/google-gemini-cli.ts ← sbde6d41e9f38, sfaf19f4d04e4
- packages/ai/test/context-overflow.test.ts ← s359842878ff1
- packages/ai/test/google-empty-response-retry.test.ts ← sdf276a239558, sebc3e5755f6c
- packages/ai/test/google-gemini-cli-variant-routing.test.ts ← sa1d070ef56fb, s72e49554235d, sb3eef0ebe8b7, s28b134cce1bc, s8e569086dc7d
- packages/catalog/CHANGELOG.md ← sc0bf66f90063
- packages/catalog/src/model-cache.ts ← s755aa9c63c59, s3e7ccf804896
- packages/catalog/src/models.json ← s19ac338ea222, s033bdb164758, sa0a24d52fad2, sd7856f23fdee, s64963bcfba0f, sf7d739cd5b8e, s52411349547d, saeb2f0e5f196, sc691deaf8835, s91da56e23fce, s3e33bf08364d, s7356bce6d61f, sd78a1c7dcbc7, sf54ee3f7bf30, sd1d0c4d0f5be, sfda0d92caad7, s6145fc44e8c0, s627263a2bb44, s2f8b4df27339, s42aeaa017caa, s7f6a94d82f01, sc69cd57a6b6b, s1a7ccde98779, sa2f500abe551, sc26d8aaab8e3, scb211b618d12, s39276c6e44ac, s9791328254a2, s7dd3048e70e8, sa672125cdcb1, s7b75613522fc, s06b8c09ead77, sc4086c4ce54d, sfe9df373fa5d, sd09a4ac2632b, s62de7dd5e7f5, s75071b4230b1, s2a5d115360d7, sdecbeaedf8fd, sa434c1fcd68c, sa72b0a204a00, s329f1f431e43, sab7db6e18bbe, s795bb1a59095, s57a7a2d55152, s836c1c132cde, s2d1d51016d7b, s433eedf71762, sf67c4362a3cb, sa1e2b23db9e2, s07fcc95512f3, s7b86878ec788, s11416c12c273, s675f3e25a8ea, s7578cc4868bf, s38dd7acf88ee, sb71f514e6280, sf7a5f0c1a898, s97d8bf759ad7, saa0a6f2cc50f, s0473dc572198, sf9e135b915c1, sc80d04000d8b, s8f67d93cb84e, sfd66acd57d4c, s20778dd3b175, s8ec2c54b843c, sec461897c889, s95d4f5cf5509, sc59b1fe7c906, s476c6d6cf162, s03f68c050f4a, sa85194bb1831, sac90a632b6b6, s38d5ec3980e5, s64ff3798a5da, s921dfb414a87, s42f176561db6, s8cdf52597caa, scba1d1f5f1d3, s5a8573e6cd9e, s8fbe811fa594, s46055b49de13, s31f620081154, s503f91d70f7e, sb6c40f2e3d13, s3b22a29adf36, s9fcd5279ef27, sb41cebfacb6c, sda00dbc8e9e4, se0d4fede9d29, s74709ef5aa5f, sa6325cee5ea8, se2fb761537cf, s97c289b54071, s7294263809f5, sf1e34e157d43, s3b214f9dbf12, sacf7825a50e0, s539486d4e860, s4187484ea1e2, s701c1a408ba2, se659442ef55f, s29f4620a2339, sbe546e7a88e6, s79c5aeef9086, s6aa17d324bfe, sfea7b50abbb2, s3454bf22ec5d, se19aaa4dfe9b, s4e7cfb42e093, s3b5021f76ce7, scf33adf667a5, s149937b706f8, s92c29b209251, sb622732ad742, sd41a5b4ea825, s3f37357a6c34, s718e88c0dffa, sb31252f3eb48, s0ce8e31e8e4b, see3092f18d95, sa8217794f73a, s110d8c154ebf, s002d8e332456, s1452b4948a84, s73d825db8ff4, s2b5b0f3577d5, s7c052de51095, sa2617ed814e8, s15be1a5769ff, s452463388a8b, s3f45f45f8ba8, sa21f1840f869, sb49787d1b500, sc3413a550166, sdae893153bb3, sf417a381db37, s1d3a79da069d, sbb42af5b097a, sdcad964e32a6, s583a599a14df, s35c0a2121412, s0168b3807439, sec880dbc2f5b, s68752f23a44d, s82221d351926, sf4571a51f6e1, s233d1595a8d9, saf71b92ec321, s13fb2b3218e0, s48fae2992edc, sc38de8a01a6b, s5c6370411cfa, s2e9e90550aa6, s85819cf5ed2c, sdc370322ee4b, sa7481471ed26, s70529d94f78f, s66473538f616, s24ace739e21d, s66e4ffb9c693, s5ede20ec6c90, s3905cc4ce392, sef42590f5160, s00f957a7adef, s34124cf1f178, sc0fb70169c3f, s7cee0b8c0691, s9c0cd3b5dead, s452e0f59ca23, s9b7515c8d9ff, sa0206dc81774, s4701db25d7be, s56dac37dc75b, sfa02d4ecc78e, s2fde161e94a1, sfccd245765f0, s42947c688980, sfb6f57349c0e, sa7e6f97da794, sb1b16cdc7f37, s9afd0a896121, scb3f9223cae5, s90828590734f, s4c2e2f2da620, s78623e9dbc2f, s29f2c5919208, scc44a1068994, s0bf0e37fffdb, s7ed94c154152, sdb8872a54135, s4b7cb82070b6, s053ff662eefc, s26797175335d, s31fde42df33f, s39c38f56bf87, sf6d480990a89, s0bdc5bd737c4, s561ab34859b7, s8390a28bfdfe, s199351011eba, sd13b33f5a5d9, sa34e532299a2, sa160988ef5c9, s251a02347cb2, s821f84e3153b, s8259ad7f4a05, s3e3dd261d97f, s8fbfa6e1c329, s88f72af35cc6, s19f88a1d7c66, sc47ab48a6075, s6ea7b02ce9ed, s4d2dfc41e00c, s9828c978b47b, s81dcb39c2517, s337dbf4da199, s587915f3fd51, s70134e85fae8, sf95d2326a0fa, s6b899100f243, sb142f647b3c8, sc0bfc5140d0a, s373c16e487d2, s91e43f0e21b1, s3922f7427fbb, s32763f34e736, s5d9839aa0fbd, scb3893d8470b, s6467516d035a, s62dbc1b74b05, s164408ed52ed, s76ac47791d32, s67ec247b0c59, sa9f6ed2fff0a, sfadaca891b9f, se63d4b7165b0, s22d070d2efc9, s17db956acb79, s063c1fcceb1f, s62700ce5f91a, s2a5ea79cf27c, s4a9fb9dbd7ef, sa6f21c65c9ed, s13ca866714aa, s1aee81c25389, s257711811673, s05688a600616, s952b3bf7d90c, s1041d15a9e6d, sb2d5d6c0c2cb, sa4d3dc086a5e, sec9bac50d073, s1ab2a999b81c, s75dc7445b24d, s111688bd424d, s5c518128e5dd, sfb0c5f13a5fa, sd750e221d580, s0d67f2e4478c, s61296af696ef, s42aef98ef121, s3ee607223e39, scb259d3e9648, sf692463ad2fd, sca4ff760b670, sa9cda5673130, sfd8e308d0d89, sd0608d3ab4bb, s57ebf448ab59, sad1946b281d6, sff1980b9a1f8, s0efd47698553, s37cf8d66ef9e, s113e9575b5d7, scf2caddb030c, s3a3807b311f7, s6bb6dfc7fca9, sf2567ede65f7, s8dd940ce03b6, s503edfca2e16, s0add950c6a06, sd677475c842c, sbde9d67c7f95, s169f3a03b542, s48518c53ea67, sd48f9336646f, sc2295131dc76, sd4c0209fbade, sadfc77c49550, s34e3a306b294, sfed215dfcb74, s0378f8e3d77a, s948b6a231350, se3cf1a0b8b90, sbae983271122, sbf73e3459ef8, sff76322ca03d, s912e31a14998, s15606b9aedd9, s53526404296b, s5f1cffa31c6d, s275696549601, se7a1448529fd, saaa9c964d62f, se3307dab92e7, sd4e82b2a5a10, s1cd6332134a3, sb6a94bf07ba8, s88b2c48829cf, s998c733b1be9, sc24cd654db43, s458fc1f7f44e, sb986c77216c4, sa1ffa1f36308, s6f8c286eff37, s5e9e0de1e196, sf933f64128b9, s281324ed9a72, s1d89687f1f84, s2efc0d428ac0, s33694a7cd717, sa2a2875d5984, s07eb2da9f8a2, sead0d6332662, sa0740a12ec07, s3cbe21716f52, sb29a3017ac54, scb1ffb53dabf, s3f8de716ba1e, sa13422f6c8d3, s1be6ae8e7b3e, s18b73c67b261, sd090833ab5f7, s789c3b1dc862, s8f0d71eda2cc, s755b70f0f68d, s6b971deb6eee, sfe68a4760527, s0dee66285de1, sac5575c60617, s706b9c2ec77a, s1af2f5b01ae7, sc8cbaed8c532, sce3820d7d69b, s3248061e36a3, sfa5a0adc245b, sb3d6584c7c7c, sc9230b513d1a, sa6225b9f1470, se5f18ff17236, s4b42fa98a4a9, sea4990eb031b, s05202090b89a, s86432d3225e9, saf57b9e2c26d, s656bc89d04bd
- packages/catalog/src/variant-collapse.ts ← s87ace149ee96, s020af7de1d94, sadfec7866eff, s54d30b175b5d
- packages/catalog/test/build.test.ts ← s540edbb6afaa, sf992ba40553a, s802d47501a76, s236b3833daa3, s6c9e6c4ff7df, s55dfe33d06e3, sc1e6a0239445, s2ca9dda7ac55
- packages/catalog/test/variant-collapse.test.ts ← s6aed117ea033, sc92f45e9a109, s8aeaff183f88, s3741a361db9d
- packages/coding-agent/CHANGELOG.md ← s43108a1d45c9
- packages/coding-agent/src/advisor/advise-tool.ts ← s67c03e2e13cb, s439c8a5d4874, s08be7000ee7f, sdce212ac9095, s8f5cfcbfcd89, s7dbd89d6641e, s941f538a8a2c, s806d3960cbd1, se2b7d26888cf, s9000f5477ae3, s8040cafc49f6, sfea63872491d, sba5c69e54d8d
- packages/coding-agent/src/advisor/runtime.ts ← s29ffb58e1b9d, s99322529d30d, sb6b8fb2cf8c4, s8a9fdadf4fae
- packages/coding-agent/src/extensibility/extensions/loader.ts ← sd18e7c9de3aa, s417f0f713c87, s3b44d4350014
- packages/coding-agent/src/extensibility/extensions/runner.ts ← sf08b0ed44d04, s1ce39db0f164, s5afde45faabb, sf2b7e512436a, s5136863e8e0d, s4b3c3dd81b34, sfa61e024a82d, sac156414c765, s53661e8fc06e, s7856fe05ffbd
- packages/coding-agent/src/extensibility/extensions/types.ts ← sbc2e2166bcef, sb0db56f5a51e, s764769e78007, s7c6f2c79d267, s97086b569999, se7766def9dc5, s1e0988835d9f, s0448c408acbb, s0cbcda36b673, sd0ecdb353321, s37916b609772, s4139e8fcd8e0, sae56b737e3ea
- packages/coding-agent/src/extensibility/plugins/legacy-pi-compat.ts ← sffe3ae6c1f59, s60ce7c326385, s06c5e46ed427
- packages/coding-agent/src/index.ts ← sa6b53a2af1d4, s7e59981b7817
- packages/coding-agent/src/modes/acp/acp-agent.ts ← s8a59e80f14ec, se5eefcabc65f
- packages/coding-agent/src/modes/components/status-line/component.ts ← s7ce904ff41d1, s6c77f22d5881, s27fd6daca597, saaf7cbe3af4f, sf06654a44f7f, s2caf3c59ec37, sad4de187cbb1
- packages/coding-agent/src/modes/controllers/extension-ui-controller.ts ← s8e6dc35535bb, seaf206db9bca, s0bab83f3cbdd, s8e2d90e388f7, s0d35e5d796d9, s79eca500d2bf
- packages/coding-agent/src/modes/interactive-mode.ts ← sf9bb09de6b41
- packages/coding-agent/src/modes/runtime-init.ts ← s05f22416d996
- packages/coding-agent/src/prompts/advisor/system.md ← s5dbdc77f4aa0, s8b8d036b4f5c
- packages/coding-agent/src/sdk.ts ← s3781f6424021
- packages/coding-agent/src/session/agent-session.ts ← sf3e4b2eba057, s09fc39fd6615, sc18bb0b2b6a6, sf661a65e284f, s005893e7e180, s2e7213263900, s2ceaf26c3bf4, sc4313fadcb6d, s3fed5f1a8bc7, sbbfdee0a568e, s3c360de0a3f6, s2ca73eaeb4c9
- packages/coding-agent/src/session/extension-delivery.ts ← s868e1e810438
- packages/coding-agent/src/session/session-advisors.ts ← s7e17c14d2a4b, sc4ec47c2f48c, s1c40c9a0f912, s28907831a2aa, s8ef8275b90d9, s674e11ec8200, s26584a825b26, s65d0dbfc873a, sc045dbebbe28, s2bfbf3b6182d, sd00ab95e59bd, sbecde85afb81, s197a77c114d0, s1170705a996a, scea31122bb54, sfd0326fee3d1
- packages/coding-agent/src/task/executor.ts ← s040b2f1f4da6
- packages/coding-agent/src/utils/git.ts ← s52b86003763c, s93bc57febe92
- packages/coding-agent/test/acp-agent.test.ts ← sc23e219fbc37, s8b6d405d66d9, s3a874e70a4df
- packages/coding-agent/test/advisor/advisor.test.ts ← sa1021be2c90b, s368cec94060d, s1a9e7fe6fd52, s81e28b0fd8cc, s7d4ac8596264, sa2b4deb68345, sd8de361714c6, s03b25f5474c6, sb7cc702b27e9, s7aab011ae080, sb31d91592fca, s7126880b89e0, s6146c93937b6, s32cf3ef3a175, s028018963b17
- packages/coding-agent/test/agent-session-advisor-suppression.test.ts ← sc91b53ee38fb
- packages/coding-agent/test/agent-session-async-delivery.test.ts ← sa6b1a212008a
- packages/coding-agent/test/agent-session-before-agent-start-attribution.test.ts ← sf87360c775e6
- packages/coding-agent/test/agent-session-plan-mode-convergence.test.ts ← s89ddf149e711
- packages/coding-agent/test/extensibility/legacy-pi-inplace-load.test.ts ← scc31d5f99a5d
- packages/coding-agent/test/extensions-runner.test.ts ← s32e9fe00bd18, s0d3f51a5ba07, s461c0e428646
- packages/coding-agent/test/git-active-context.test.ts ← s4c64fc11e477
- packages/coding-agent/test/interactive-mode-plan-review.test.ts ← sa8c8de226e9d, sbe52174581dd
- packages/coding-agent/test/marketplace/project-scope.test.ts ← sd4dec30915be
- packages/coding-agent/test/modes/warp-events.test.ts ← s1dc92cb9ade1
- packages/coding-agent/test/rpc-client.restart.test.ts ← s61475e197501, s0d63695496e9, sab1e4d324b80, s9fcfeec9e4c6
- packages/coding-agent/test/rpc-stdin-lock.test.ts ← s87222be32f32
- packages/coding-agent/test/rpc.test.ts ← s82c8223b401b
- packages/coding-agent/test/sdk-mcp-instructions.test.ts ← s297310debc51, sb0064149dcb6
- packages/coding-agent/test/status-line-overflow.test.ts ← sa3ce8ec7934f
- packages/coding-agent/test/system-prompt-dedup.test.ts ← sac35e6ffb383, s9e5ab8f24d59
- packages/coding-agent/test/tools/lsp-regressions.test.ts ← s2c2f4a4f5fa0, sc3eecf6a36f0
- packages/coding-agent/test/utils/changelog.test.ts ← s318f09df50f9
- packages/tui/test/kitty-keyboard-da1-ordering.test.ts ← s1f7aacf90e21, s139e7d99a4c5
- packages/work-client/package.json ← s0637fa859e68
- packages/work-client/src/contract.ts ← s4b8530984a56
- packages/work-client/src/index.ts ← sc4f4fec9372c
- packages/work-client/test/client.test.ts ← s2343db6f372c
- packages/work-client/tsconfig.json ← sfb71e4bf9ee8
- python/omp-work/.coverage ← s44984e93ae1f
- python/omp-work/CHANGELOG.md ← sce33eb56f37b
- python/omp-work/README.md ← s0fe203253163
- python/omp-work/pyproject.toml ← s1cca73f446b7
- python/omp-work/src/omp_work.egg-info/PKG-INFO ← s94657264a7a3
- python/omp-work/src/omp_work.egg-info/SOURCES.txt ← sa07f0911583b
- python/omp-work/src/omp_work.egg-info/dependency_links.txt ← sf30ed6384319
- python/omp-work/src/omp_work.egg-info/entry_points.txt ← s3c1bcbb7c9da
- python/omp-work/src/omp_work.egg-info/requires.txt ← s8d2514f0e901
- python/omp-work/src/omp_work.egg-info/top_level.txt ← s2c882c6b5845
- python/omp-work/src/omp_work/__init__.py ← s2fd0ea53c5ec
- python/omp-work/src/omp_work/__main__.py ← se75308e6d991
- python/omp-work/src/omp_work/contracts/v1/api-schema.json ← s8eb7dd547d5b
- python/omp-work/src/omp_work/contracts/v1/approval.json ← s5384bfb87978
- python/omp-work/src/omp_work/contracts/v1/candidate-hash.json ← sdbcb1b26d300
- python/omp-work/src/omp_work/contracts/v1/contract.json ← s77b1f00dba2c
- python/omp-work/src/omp_work/contracts/v1/decisions/0001-domain-source-and-security.md ← s9374b2875bd6
- python/omp-work/src/omp_work/contracts/v1/decisions/0002-evidence-completion-and-recovery.md ← s9858164c36e7
- python/omp-work/src/omp_work/contracts/v1/decisions/0003-one-ledger-cutover.md ← sa86d5df5fecc
- python/omp-work/src/omp_work/contracts/v1/decisions/0004-home-147-pre-cutover-amendment.md ← s5d219295b62b
- python/omp-work/src/omp_work/contracts/v1/decisions/0005-close-attempt-authority.md ← sa3d7b0578c73
- python/omp-work/src/omp_work/contracts/v1/decisions/0006-batch-completion-rider-authority.md ← sd512d13593e3
- python/omp-work/src/omp_work/contracts/v1/decisions/0007-omp-147-bookends-closure.md ← s64217ff54727
- python/omp-work/src/omp_work/contracts/v1/examples.json ← s9621e1333a4d
- python/omp-work/src/omp_work/contracts/v1/manifest.json ← s42e5d6d72116
- python/omp-work/src/omp_work/contracts/v1/schema.json ← s6574547e0f4e
- python/omp-work/src/omp_work/integration/__init__.py ← sa5f9b5641ecb
- python/omp-work/src/omp_work/integration/importer.py ← s4380cb89618f
- python/omp-work/src/omp_work/integration/legacy_artifacts.py ← s1ef2d4aebe9b
- python/omp-work/src/omp_work/operations/__init__.py ← sda8b5c7ff9cf
- python/omp-work/src/omp_work/operations/artifacts.py ← s0752ef76797c
- python/omp-work/src/omp_work/operations/backup.py ← s5ca2897ee862
- python/omp-work/src/omp_work/operations/capabilities.py ← sdb98ac04e358
- python/omp-work/src/omp_work/operations/cli.py ← se693fdf5fd9d
- python/omp-work/src/omp_work/operations/config.py ← s61b08a458e4f
- python/omp-work/src/omp_work/operations/database.py ← scc94b9ae08b8
- python/omp-work/src/omp_work/operations/fingerprints.py ← sb8552c96500e
- python/omp-work/src/omp_work/operations/migrations/0001_schema_and_security.sql ← s0a86edded858
- python/omp-work/src/omp_work/operations/migrations/0002_operations_health.sql ← s99928495c0b4
- python/omp-work/src/omp_work/operations/migrations/0003_work_service_domain.sql ← s51a313143ac2
- python/omp-work/src/omp_work/operations/migrations/0004_backup_reader_privileges.sql ← s76a486b970c3
- python/omp-work/src/omp_work/operations/migrations/0005_work_service_hardening.sql ← s149f6961d540
- python/omp-work/src/omp_work/operations/migrations/0006_linear_export_records.sql ← s37c99b8ffcb5
- python/omp-work/src/omp_work/operations/migrations/0007_linear_import.sql ← s80c3bdac4a63
- python/omp-work/src/omp_work/operations/migrations/0008_workflow_candidate_contract.sql ← se164b8afa8bf
- python/omp-work/src/omp_work/operations/migrations/0009_service_readiness_grants.sql ← s9424e6097339
- python/omp-work/src/omp_work/operations/migrations/0010_cutover_authority.sql ← sc28dd4b8a219
- python/omp-work/src/omp_work/operations/migrations/0011_cutover_attestation.sql ← s139face7158d
- python/omp-work/src/omp_work/operations/migrations/0012_source_watermark.sql ← se6e53e7905de
- python/omp-work/src/omp_work/operations/migrations/0013_first_mutation_pairing.sql ← s572bf57a0a34
- python/omp-work/src/omp_work/operations/migrations/0014_close_attempts_and_audit_manifests.sql ← s65a0c870406a
- python/omp-work/src/omp_work/operations/migrations/0015_auditor_launch_cancellation.sql ← s2156c7f06061
- python/omp-work/src/omp_work/operations/migrations/0016_close_attempt_riders.sql ← s80d21b4a8bd2
- python/omp-work/src/omp_work/operations/migrations/0017_close_authorization_uses.sql ← s54efebea5b37
- python/omp-work/src/omp_work/operations/sql/roles.sql ← se07b7945ce13
- python/omp-work/src/omp_work/py.typed ← se0720d76510b
- python/omp-work/src/omp_work/v1/__init__.py ← s7b0fe647407b
- python/omp-work/src/omp_work/v1/api_models.py ← s271e48410bbf
- python/omp-work/src/omp_work/v1/canonical.py ← sb58891650f8a
- python/omp-work/src/omp_work/v1/client.py ← s9344b6fe23c1
- python/omp-work/src/omp_work/v1/models.py ← sfe77f45f6fa5
- python/omp-work/src/omp_work/v1/semantics.py ← s1e662618ffc3
- python/omp-work/src/omp_work/v1/server.py ← sa5f2d5a5ee3a
- python/omp-work/src/omp_work/v1/service.py ← s7599af6e95c3
- python/omp-work/src/omp_work/v1/store.py ← s7f4e2079ebfc
- python/omp-work/tests/pg_native.py ← s511eebb2c826
- python/omp-work/tests/test_contract.py ← s6a79e184c38a
- python/omp-work/tests/test_cutover.py ← sf2c4861aaaa1
- python/omp-work/tests/test_install_script.py ← s2a79ca7fbe23
- python/omp-work/tests/test_linear_import.py ← s4493fd7c1d22
- python/omp-work/tests/test_postgres_operations.py ← sd83d7eb41295
- python/omp-work/tests/test_work_service.py ← s439979d7cb35
- python/omp-work/tests/test_workflow_service.py ← s24fb2e78e3b9
- python/omp-work/uv.lock ← s30d44f59c5c7
- python/robomp/tests/test_persona.py ← s2363bab03de3, s995196c113c8, s5d0a123b637e, sedc4c53b95b1
- python/robomp/tests/test_queue_cancel.py ← sa74c399156b9
- python/robomp/tests/test_queue_dispatch.py ← s90e099f6c976
- python/robomp/tests/test_queue_shutdown.py ← s4a18a7821fd0
- python/robomp/tests/test_retry.py ← sd11a56e4158f
- python/robomp/tests/test_worker.py ← s4dbd47d7323a
- python/robomp/uv.lock ← sc561434b1ad8
- session-system/.gitignore ← sd00749176bcd
- session-system/README.md ← se84d636fc918
- session-system/agents/AGENTS.md ← s36f046fbc080
- session-system/agents/auditor.md ← scbe94fba21e7
- session-system/agents/omp-AGENTS.md ← sb64fae216b63
- session-system/extensions/model-bookends-audit.md ← sabe958cb47eb
- session-system/extensions/model-bookends.ts ← s5b2e1e241f2f
- session-system/extensions/work-now.ts ← s5f5884e8afd0
- session-system/extensions/workflow/backend.ts ← s46beb8c7b340
- session-system/extensions/workflow/checkpoint-delivery.ts ← s6f6b08faadcf
- session-system/extensions/workflow/config.ts ← s5bf0e36957e8
- session-system/extensions/workflow/confirm.ts ← s796e7646ec00
- session-system/extensions/workflow/digest-prompt.md ← s65993da7b774
- session-system/extensions/workflow/git.ts ← s425d19f2cb91
- session-system/extensions/workflow/host.ts ← sabe7ce968a75
- session-system/extensions/workflow/kind-description.md ← s558372d5fcbc
- session-system/extensions/workflow/lock-refusal.md ← s6518ea605c42
- session-system/extensions/workflow/pending-ops.ts ← sfe451b17d473
- session-system/extensions/workflow/rider-batch.ts ← sb28ce0dff0ea
- session-system/extensions/workflow/sequence.md ← s8507fe84bde6
- session-system/extensions/workflow/session-ledger-prompt.md ← sd1695efdb8df
- session-system/extensions/workflow/session-ledger.ts ← sa990beff8ea4
- session-system/extensions/workflow/status.ts ← s72ff842f2a01
- session-system/extensions/workflow/tool-description.md ← sa092eb3dfafd
- session-system/extensions/workflow/transcript.ts ← s0540caff99b3
- session-system/extensions/workflow/work.ts ← sf47ce157e5c1
- session-system/hooks/task-observer-first-tool.mjs ← sd782ecdd73bc
- session-system/install.sh ← s28a618582c94
- session-system/prompts/archive/PROMPT-adhd-session-review.md ← s5a05ded47f99
- session-system/prompts/archive/PROMPT-omp-extension-casualties.md ← s7b68dfb31a6b
- session-system/prompts/archive/PROMPT-omp-linear-integration.md ← sd85bee20534c
- session-system/prompts/archive/PROMPT-omp-system-walkthrough.md ← sd85518b678e9
- session-system/prompts/archive/PROMPT-session-product-drain.md ← s2bee561474c9
- session-system/prompts/archive/PROMPT-session-system-hardening.md ← sdfc438d99812
- session-system/prompts/archive/PROMPT-session-system-v2.md ← s57f48791ad65
- session-system/prompts/archive/PROMPT-session-verdict-drain.md ← s7bd4d5dc2b41
- session-system/refresh-natives.sh ← sc260e87c315d
- session-system/rules/work-plan.md ← sf7f79e063f11
- session-system/skills/caveman-commit/README.md ← s56e909583d5c
- session-system/skills/caveman-commit/SKILL.md ← s4a6500f0bceb
- session-system/skills/caveman-compress/README.md ← s9cafe0bcc731
- session-system/skills/caveman-compress/SECURITY.md ← sb1783aa6a180
- session-system/skills/caveman-compress/SKILL.md ← s87fd539a815a
- session-system/skills/caveman-compress/scripts/__init__.py ← s8473aa6097d4
- session-system/skills/caveman-compress/scripts/__main__.py ← s06e36b18e830
- session-system/skills/caveman-compress/scripts/benchmark.py ← sff8574f88063
- session-system/skills/caveman-compress/scripts/cli.py ← sf2d0af0669c6
- session-system/skills/caveman-compress/scripts/compress.py ← sea07ef1aa068
- session-system/skills/caveman-compress/scripts/detect.py ← sd0dd0fb987b5
- session-system/skills/caveman-compress/scripts/validate.py ← sd936791933a1
- session-system/skills/caveman-help/README.md ← sf4853a26025c
- session-system/skills/caveman-help/SKILL.md ← s5b3476e996aa
- session-system/skills/caveman-review/README.md ← s5d4752b93fc4
- session-system/skills/caveman-review/SKILL.md ← sa5d14f893aa1
- session-system/skills/caveman/README.md ← s0b5140a1ab3a
- session-system/skills/caveman/SKILL.md ← s6322c75b1833
- session-system/skills/intake/SKILL.md ← s4d01faadf462
- session-system/skills/notebooklm/API.md ← se0af49a93105
- session-system/skills/notebooklm/SKILL.md ← sf444abae6b41
- session-system/skills/notebooklm/playbooks.md ← s3cb5466fda77
- session-system/skills/notebooklm/reference.md ← s75b71d4b991b
- session-system/skills/prompt-master/LICENSE ← s66f99c8d763d
- session-system/skills/prompt-master/README.md ← s9829096ec582
- session-system/skills/prompt-master/SKILL.md ← s5d04af0a39bd
- session-system/skills/prompt-master/references/patterns.md ← s7212c6ee2aeb
- session-system/skills/prompt-master/references/templates.md ← s26dd22601bcf
- session-system/skills/questionyourself/SKILL.md ← s3248c67897cd
- session-system/skills/summary/SKILL.md ← s95cb6e029a28
- session-system/skills/summary/references/ledger-close.md ← sced50c04cdf5
- session-system/skills/summary/references/loop-charter.md ← s59557508f9e6
- session-system/skills/summary/references/policy.md ← scdb35147b8cc
- session-system/skills/summary/references/rule-map.md ← sfb628a0f53dd
- session-system/skills/summary/references/session-review.md ← s0037031ab612
- session-system/skills/task-observer/SKILL.md ← s5474ee9c2abd
- session-system/skills/task-observer/references/environments.md ← s567faa1db162
- session-system/skills/task-observer/references/skill-authoring.md ← sc7a2cccbec64
- session-system/skills/task-observer/references/weekly-review.md ← sb95ef4a2c5ae
- session-system/skills/vibe-check/.nojekyll ← s53016fc30562
- session-system/skills/vibe-check/CHANGELOG.md ← s650754f1643b
- session-system/skills/vibe-check/LICENSE ← s7d65c4876889
- session-system/skills/vibe-check/README.md ← s78592175141e
- session-system/skills/vibe-check/RELEASING.md ← s72f18ee1f545
- session-system/skills/vibe-check/SKILL.md ← s798c2a8fefd0
- session-system/skills/vibe-check/VERSION ← s7f07b268aec9
- session-system/skills/vibe-check/assets/diagrams/blueprint.html ← s6ab3aecc3d2e
- session-system/skills/vibe-check/assets/diagrams/crazy8-combine.html ← sd03c9cfc2de5
- session-system/skills/vibe-check/assets/diagrams/crazy8-web.html ← s28977f0d9c51
- session-system/skills/vibe-check/assets/diagrams/crazy8.html ← sf311b90b6aaf
- session-system/skills/vibe-check/assets/diagrams/data-clearlist.json ← s45449bfd900b
- session-system/skills/vibe-check/assets/diagrams/data-matrix-example.json ← s255e372f0216
- session-system/skills/vibe-check/assets/diagrams/data-opportunity-example.json ← s223ea1688f20
- session-system/skills/vibe-check/assets/diagrams/data-skillconcierge.json ← s4407a03a034c
- session-system/skills/vibe-check/assets/diagrams/data-storymap-example.json ← sb3aa40fa74c2
- session-system/skills/vibe-check/assets/diagrams/data-vinti.json ← s06cdd7fe0be7
- session-system/skills/vibe-check/assets/diagrams/engine.css ← se95c002834b1
- session-system/skills/vibe-check/assets/diagrams/matrix.html ← seb7e2ef32f28
- session-system/skills/vibe-check/assets/diagrams/opportunity.html ← s7a3ab4996081
- session-system/skills/vibe-check/assets/diagrams/storymap.html ← s09ab95f3bf1c
- session-system/skills/vibe-check/assets/readme-banner.png ← sf1ac54d58f18
- session-system/skills/vibe-check/bump.sh ← s064d6339de88
- session-system/skills/vibe-check/examples/README.md ← s33ca8b45cf70
- session-system/skills/vibe-check/examples/audhd-validation-report.html ← s98af12cef764
- session-system/skills/vibe-check/examples/clearlist-blueprint.html ← s771ed620fa05
- session-system/skills/vibe-check/examples/clearlist-prd.html ← s9711d6137dcc
- session-system/skills/vibe-check/examples/clearlist-session.md ← sb55f2de5b13e
- session-system/skills/vibe-check/examples/plant-blueprint.html ← s43be0cc9f59d
- session-system/skills/vibe-check/examples/plant-watering.md ← s487831efd8cc
- session-system/skills/vibe-check/references/CODE-CHECKUP.md ← s5fe3d2f2171a
- session-system/skills/vibe-check/references/COLD-START.md ← s964755a22c0d
- session-system/skills/vibe-check/references/DIAGRAM-SYSTEM.md ← s539a67f8577e
- session-system/skills/vibe-check/references/DISCOVERY-DEEP-DIVE.md ← s73685b65f9bc
- session-system/skills/vibe-check/references/EXPERIENCE-BLUEPRINT.md ← sb60bb3ab5924
- session-system/skills/vibe-check/references/GITHUB-AND-DEPLOYMENT.md ← sfc0bcab0445d
- session-system/skills/vibe-check/references/GROWTH-LOOPS.md ← sb1fefb74e680
- session-system/skills/vibe-check/references/HTML-BLUEPRINT.md ← s339cdebc4ae1
- session-system/skills/vibe-check/references/KEEPING-CODE-NAVIGABLE.md ← s075f0fde446c
- session-system/skills/vibe-check/references/MANAGING-YOUR-AI.md ← sf5d79304a6d9
- session-system/skills/vibe-check/references/MULTI-SIDED.md ← s3e7af221027b
- session-system/skills/vibe-check/references/PLAN-TEMPLATE.md ← s2c23ca622156
- session-system/skills/vibe-check/references/PRD.md ← se28ebe62e6e0
- session-system/skills/vibe-check/references/WHAT-A-SKILL-ACTUALLY-IS.md ← s156a752d79cc
- session-system/skills/whatsmissing/SKILL.md ← sb184bbb2beb2
- session-system/skills/wiz-ccr-creator/SKILL.md ← s7b43dd9ebc2a
- session-system/skills/wiz-ccr-creator/references/ccr_reference.md ← sef53457697a9
- session-system/skills/wiz-mcp/SKILL.md ← s70aee90f8744
- session-system/tests/checkpoint-delivery.test.ts ← s71129aa2bb22
- session-system/tests/closeout-boundary.test.ts ← s991b33833955
- session-system/tests/closeout-lock.test.ts ← sab3e2aa6fc6f
- session-system/tests/commit-step.test.ts ← s7811ba3b4518
- session-system/tests/extension-load.test.ts ← s4059cce41f1c
- session-system/tests/fixtures/closeout-lock-harness.ts ← sca23d69a82e1
- session-system/tests/fixtures/obligation-loop-harness.ts ← s023756137c74
- session-system/tests/fixtures/scope-enforcement-harness.ts ← sec08b7453919
- session-system/tests/fixtures/two-phase.ts ← sa96a6f3a78c1
- session-system/tests/fixtures/work-service-smoke-harness.ts ← sd1ad6282680b
- session-system/tests/fixtures/workflow-sequence-harness.ts ← s431a11d3b104
- session-system/tests/install.test.ts ← s708d28134fcb
- session-system/tests/model-bookends.test.ts ← s73205aa20b38
- session-system/tests/now-terminal-guard.test.ts ← s69d912e5dff4
- session-system/tests/obligation-loop.test.ts ← se398c4b869e3
- session-system/tests/pending-ops.test.ts ← s83b01fd82d1c
- session-system/tests/rider-batch.test.ts ← s30d09ac7fdc0
- session-system/tests/same-session-sections.test.ts ← scc98d120acfb
- session-system/tests/scope-enforcement.test.ts ← sda9c6721b392
- session-system/tests/session-ledger.test.ts ← s935939e0914e
- session-system/tests/task-observer-first-tool.test.ts ← sd83d9d198ea1
- session-system/tests/work-config.test.ts ← s51f5aa159a8d
- session-system/tests/work-service-candidate-smoke.ts ← scde34bdb42ec
- session-system/tests/workflow-sequence.test.ts ← sb7f6c5e6e379
- session-system/tsconfig.json ← sd9fd46c9fb20
- session-system/update.sh ← s909b70d773bc
- skill-observations/cross-cutting-principles.md ← s55e21097e07c
- skill-observations/last-review-date.txt ← s8563841e1cd3
- skill-observations/log.md ← saed49ec5f400
- skill-observations/log.md.bak ← sa92299786293

## Appendix B — changelog entries

- agent@17.4.0:breaking:1 → breaking-change disposition in changelog ledger
- agent@17.4.0:breaking:2 → breaking-change disposition in changelog ledger
- agent@17.4.0:added:1 → adopted upstream behavior
- agent@17.4.0:added:2 → adopted upstream behavior
- agent@17.4.0:added:3 → adopted upstream behavior
- agent@17.3.5:added:1 → adopted upstream behavior
- ai@18.0.6:added:1 → adopted upstream behavior
- ai@18.0.5:breaking:1 → breaking-change disposition in changelog ledger
- ai@18.0.5:added:1 → adopted upstream behavior
- ai@18.0.5:added:2 → adopted upstream behavior
- ai@18.0.1:added:1 → adopted upstream behavior
- ai@18.0.0:added:1 → adopted upstream behavior
- ai@17.4.2:added:1 → adopted upstream behavior
- ai@17.4.1:added:1 → adopted upstream behavior
- ai@17.4.0:added:1 → adopted upstream behavior
- ai@17.3.5:added:1 → adopted upstream behavior
- catalog@18.0.5:added:1 → adopted upstream behavior
- catalog@18.0.5:added:2 → adopted upstream behavior
- catalog@18.0.1:added:1 → adopted upstream behavior
- catalog@18.0.1:added:2 → adopted upstream behavior
- catalog@18.0.0:added:1 → adopted upstream behavior
- catalog@17.4.2:added:1 → adopted upstream behavior
- catalog@17.4.1:added:1 → adopted upstream behavior
- catalog@17.4.0:added:1 → adopted upstream behavior
- catalog@17.4.0:added:2 → adopted upstream behavior
- catalog@17.3.8:added:1 → adopted upstream behavior
- catalog@17.3.8:added:2 → adopted upstream behavior
- catalog@17.3.5:added:1 → adopted upstream behavior
- catalog@17.3.4:added:1 → adopted upstream behavior
- coding-agent@18.0.6:added:1 → adopted upstream behavior
- coding-agent@18.0.6:added:2 → adopted upstream behavior
- coding-agent@18.0.6:added:3 → adopted upstream behavior
- coding-agent@18.0.5:added:1 → adopted upstream behavior
- coding-agent@18.0.5:added:2 → adopted upstream behavior
- coding-agent@18.0.5:added:3 → adopted upstream behavior
- coding-agent@18.0.5:added:4 → adopted upstream behavior
- coding-agent@18.0.5:added:5 → adopted upstream behavior
- coding-agent@18.0.5:added:6 → adopted upstream behavior
- coding-agent@18.0.5:added:7 → adopted upstream behavior
- coding-agent@18.0.5:added:8 → adopted upstream behavior
- coding-agent@18.0.5:added:9 → adopted upstream behavior
- coding-agent@18.0.5:added:10 → adopted upstream behavior
- coding-agent@18.0.4:added:1 → adopted upstream behavior
- coding-agent@18.0.4:added:2 → adopted upstream behavior
- coding-agent@18.0.4:added:3 → adopted upstream behavior
- coding-agent@18.0.4:added:4 → adopted upstream behavior
- coding-agent@18.0.3:added:1 → adopted upstream behavior
- coding-agent@18.0.2:added:1 → adopted upstream behavior
- coding-agent@18.0.1:added:1 → adopted upstream behavior
- coding-agent@18.0.1:added:2 → adopted upstream behavior
- coding-agent@18.0.1:added:3 → adopted upstream behavior
- coding-agent@18.0.1:added:4 → adopted upstream behavior
- coding-agent@18.0.1:added:5 → adopted upstream behavior
- coding-agent@18.0.0:added:1 → adopted upstream behavior
- coding-agent@18.0.0:added:2 → adopted upstream behavior
- coding-agent@18.0.0:added:3 → adopted upstream behavior
- coding-agent@18.0.0:added:4 → adopted upstream behavior
- coding-agent@18.0.0:added:5 → adopted upstream behavior
- coding-agent@18.0.0:added:6 → adopted upstream behavior
- coding-agent@18.0.0:added:7 → adopted upstream behavior
- coding-agent@17.4.4:added:1 → adopted upstream behavior
- coding-agent@17.4.2:added:1 → adopted upstream behavior
- coding-agent@17.4.2:added:2 → adopted upstream behavior
- coding-agent@17.4.1:added:1 → adopted upstream behavior
- coding-agent@17.4.1:added:2 → adopted upstream behavior
- coding-agent@17.4.1:added:3 → adopted upstream behavior
- coding-agent@17.4.1:added:4 → adopted upstream behavior
- coding-agent@17.4.1:added:5 → adopted upstream behavior
- coding-agent@17.4.1:added:6 → adopted upstream behavior
- coding-agent@17.4.1:added:7 → adopted upstream behavior
- coding-agent@17.4.1:added:8 → adopted upstream behavior
- coding-agent@17.4.1:added:9 → adopted upstream behavior
- coding-agent@17.4.1:added:10 → adopted upstream behavior
- coding-agent@17.4.1:added:11 → adopted upstream behavior
- coding-agent@17.4.0:added:1 → adopted upstream behavior
- coding-agent@17.4.0:added:2 → adopted upstream behavior
- coding-agent@17.4.0:added:3 → adopted upstream behavior
- coding-agent@17.4.0:added:4 → adopted upstream behavior
- coding-agent@17.4.0:added:5 → adopted upstream behavior
- coding-agent@17.4.0:added:6 → adopted upstream behavior
- coding-agent@17.4.0:added:7 → adopted upstream behavior
- coding-agent@17.4.0:added:8 → adopted upstream behavior
- coding-agent@17.4.0:added:9 → adopted upstream behavior
- coding-agent@17.4.0:added:10 → adopted upstream behavior
- coding-agent@17.4.0:added:11 → adopted upstream behavior
- coding-agent@17.4.0:added:12 → adopted upstream behavior
- coding-agent@17.4.0:added:13 → adopted upstream behavior
- coding-agent@17.3.8:added:1 → adopted upstream behavior
- coding-agent@17.3.6:added:1 → adopted upstream behavior
- coding-agent@17.3.5:added:1 → adopted upstream behavior
- hashline@17.4.0:added:1 → adopted upstream behavior
- mnemopi@17.3.8:added:1 → adopted upstream behavior
- natives@18.0.5:added:1 → adopted upstream behavior
- natives@18.0.5:added:2 → adopted upstream behavior
- natives@18.0.0:added:1 → adopted upstream behavior
- natives@18.0.0:added:2 → adopted upstream behavior
- natives@18.0.0:added:3 → adopted upstream behavior
- natives@17.4.0:added:1 → adopted upstream behavior
- natives@17.4.0:added:2 → adopted upstream behavior
- natives@17.4.0:added:3 → adopted upstream behavior
- natives@17.3.4:added:1 → adopted upstream behavior
- snapcompact@17.4.1:added:1 → adopted upstream behavior
- tui@18.0.6:added:1 → adopted upstream behavior
- tui@18.0.5:breaking:1 → breaking-change disposition in changelog ledger
- tui@18.0.5:added:1 → adopted upstream behavior
- tui@18.0.1:added:1 → adopted upstream behavior
- tui@18.0.1:added:2 → adopted upstream behavior
- tui@18.0.0:breaking:1 → breaking-change disposition in changelog ledger
- tui@18.0.0:breaking:2 → breaking-change disposition in changelog ledger
- tui@18.0.0:breaking:3 → breaking-change disposition in changelog ledger
- tui@18.0.0:added:1 → adopted upstream behavior
- tui@18.0.0:added:2 → adopted upstream behavior
- tui@18.0.0:added:3 → adopted upstream behavior
- tui@18.0.0:added:4 → adopted upstream behavior
- tui@18.0.0:added:5 → adopted upstream behavior
- tui@18.0.0:added:6 → adopted upstream behavior
- tui@18.0.0:added:7 → adopted upstream behavior
- tui@18.0.0:added:8 → adopted upstream behavior
- tui@18.0.0:added:9 → adopted upstream behavior
- tui@17.4.4:added:1 → adopted upstream behavior
- tui@17.4.2:added:1 → adopted upstream behavior
- tui@17.4.1:added:1 → adopted upstream behavior
- tui@17.4.0:added:1 → adopted upstream behavior
- tui@17.4.0:added:2 → adopted upstream behavior
- utils@18.0.6:added:1 → adopted upstream behavior
- utils@18.0.5:added:1 → adopted upstream behavior
- utils@18.0.4:added:1 → adopted upstream behavior
- utils@17.4.1:added:1 → adopted upstream behavior
- utils@17.3.8:added:1 → adopted upstream behavior
