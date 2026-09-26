# OMP completion audit and continuation decision — 2026-09-18

Review scope: independent inspection, safe isolated verification and this document only. No product edits, backlog changes, native-goal changes, deployment, paid model jobs or production mutations. One accountable reviewer; no review fan-out. This is a review artifact, not a new status ledger.

## 1. VERDICT

**Completed goal: substantiated as a reviewed, packaged source delivery, with material limits on “useful controls” and lifecycle proof.** The actual completed goal is Codex goal `885c4adb-80ef-4c5f-b8f3-4077d716e869`, thread `01a0b245-b374-7841-923c-7650ecc99a46`. Read-only SQLite inspection found its objective matches the corrected kickoff and status `complete`. Current review thread's supported `get_goal` returned null. Neither was changed.

The old goal did not require production cutover, whole-programme completion, or completion of the separate 20-task/72-hour gates. Owner's 2026-09-18 13:28 correction explicitly removed native OMP slash commands as prerequisites for Codex development. Thus absence of native OMP-280 closure is **not** grounds to reject the delivered source package. Preserve that success. However, “every requirement proven” in the completion audit is too broad: control tests use a mocked WorkService or mock control executable; queue cancellation tests establish payload/state handling, not cancellation of a running worker; the ECC record was assembled after candidate review. These qualify the evidence, not erase the implementation.

**Candidate readiness: ready for source review/reconstruction; not ready to enable as a dependable integrated user workflow.** Three clean, independently reviewed commits and matching archive exist. Fresh bounded checks pass. One concrete UI targeting defect remains: Pause/Stop appear below coordinator activity without displaying the active native work target before action. They operate on WorkService's current grant, not the external Codex/Gemini job shown elsewhere. OMP-280 had no execution grant. This is a discoverability/control-scope defect, not a demonstrated authorization bypass. Correct it and qualify the actual integrated process path before enabling controls.

**Wider programme: substantially incomplete, not failed by this delivery.** Research contracts, budgets, knowledge/correction and recovery foundations are real. Full adaptive campaigns, protected empirical evaluation, hypothesis/method revision, recursive platform research, fresh-task learning benefit, broad ECC activation, integrated WebUI and sustained qualifications remain. No supported 10× claim.

Confidence is strongest in exact candidate identity, inspected code and fresh boundary checks; narrower for historical full suites; absent for production Plex behavior, candidate-matched installed controls, full reconnect/restart qualification of the external queue, owner-time advantage and whole-programme completion.

### Evidence snapshot and source precedence

Snapshot collected 2026-09-18 around 13:59–14:10 UTC; later writing does not turn observations into continuous monitoring.

| Surface | Observed identity/state | Meaning |
|---|---|---|
| Main OMP workspace | `/home/thetu/oh-my-pi`, `codex/full-programme-20260917`, `1cec1889ea2b8bfa63d52015f13243d3e728d473` | Older than isolated programme source; not the delivered controls candidate. |
| Main dirty OMP work | Modified work-client index, two Python egg-info files, observation log/date; untracked historical review/kickoff/generator/principles, screenshots, archive and observation artifacts | Preserved. No claim clean or accepted. |
| Current isolated programme source | `admission-checkout`, `codex/omp-programme-20260917`, `57ab92fbd48ead684886965e1a8e1b38e6a6bf98` + 23 R04-S1 changed/untracked files | All 23 still match frozen tree `b0146ea2c543443f4f354b0a343a5326079454b5`; patch hash matches. R04 review ends “Session closed,” no final verdict. |
| OMP controls | `/home/thetu/oh-my-pi-worktrees/omp-280-controls`, `96f511b4b67d08bb5d8b5bcb3662515691679af2`, clean | Base `55984580e52d59ceec75e06d7776a1c2b29699f6`; tree `f22eaa013f37da730db13acd6bfd2b2b210c15be`. Separate candidate, not automatically integrated with R04. |
| WebUI main/candidate | Main `master` `d0834bcd0f53732690844deafacdc45ddc5c5c90`, existing daemon/UI/test dirt preserved. Controls worktree clean at `0a2e0cd0a6592698dd5fcc5712d8d120186918c7` | Candidate tree `a811cdb4f9e93b6487db026c1cd4a501c49b1f95`. Includes earlier progress work plus controls. |
| Media main/candidate | Main `923fbc0ef37be68f3f605e311c7b07bcc4039961`, existing TASKS/untracked dirt; isolated candidate `85be7980c8d7394bf4c5e12f6abadac1a43f19c5`, clean | Candidate tree `e28bbe7f3ec3c6a6a31f1788e8313b8933a03f9d`. No production edits. |
| Remote candidates | Three `git ls-remote origin refs/heads/delivery/...` calls exit 0; heads match the three full commits above | Pushed branch evidence, not merge/deployment evidence. |
| Package | `OMP-280-first-delivery.tar.gz`, SHA256 `faa666b40652cc68fc15ea255d9b3818ccf34279d9f85472e90a5306f4274f42` | `sha256sum -c PACKAGE.sha256` passes. All archived members match existing siblings; exact patch hashes match manifests. |
| Installed CLI/service | PATH omp resolves to main workspace's source launcher, package 18.0.6. `omp-work-service.service` active, PID 859505, main-workspace Python interpreter, port 54322 | Source-linked running system; no immutable installed identity inferred. Reviewed controls return typed `contract_mismatch`; `~/.local/bin/omp-execution-control` absent. |
| Runtime services/jobs | Fleet PostgreSQL/Neo4j and three local inference listeners present; restore-drill service failed. No listeners on checked WebUI ports 7490/7491. No Kimi/Claude worker observed; unrelated agy PID 1370817 remains | Availability is not qualification. Unrelated agy left untouched; remote jobs/quiescence not established. Failed restore flag requires diagnosis, not a claim of data loss. |
| Native records | OMP-280 r1 REVIEWED, six criteria, final candidate `af47aa51-375e-4342-99d4-9a5db0108e43`, no close attempts; OMP-233 r2, 246 r2, 249 r4, 267 r2, 278 r1, 279 r1 BACKLOG; 277 DONE | Fresh supported WorkClient reads. Historical “OMP-280 IN PROGRESS” is superseded. Owner focus v305 unchanged. |

Sources: [delivery package][package], [frozen media manifest][media-manifest], [controls manifest][control-manifest], [execution checkpoint][checkpoint], [isolated programme MASTER][candidate-master]. Dirty-state observations are from `git status --short`, not inferred from branch names.

Required guidance read: home/repository AGENTS, Antidote audit mode, Observer session-start and current observations, CONTEXT, current economy POLICY/MODELS; candidate media AGENTS and WebUI CLAUDE guidance inspected for applicable verification boundaries. No observation/backlog writes performed.

Precedence/conflicts resolved:

- [Corrected kickoff][kickoff] plus actual edited goal and owner correction govern old delivery. The nonexistent `OMP_Astra_Implementation_Handoff.md` dependency was explicitly withdrawn; it is not a missing prerequisite.
- Main MASTER's opening checkpoint is older than isolated MASTER and current delivery evidence. The existing checkpoint contains explicit superseding completion paragraphs **and** a stale final “goal remains active” paragraph. Native goal is complete; report/checkpoint do not override authority state.
- [Q1–35][decisions] preserve full ambitions; Q36 stays withheld. Historical Astra/Luna routing and older cost proposals do not override current POLICY/MODELS. Historical broad UI deferral was narrowed for this thin control delivery, not waived for all WebUI work.
- Older Fleet-first sequencing yields to the September 18 engineering → adaptive-research → two-repository correction order. This audit inserts a bounded control qualification step, not a new programme.
- Native WorkService status remains separate from Codex source delivery. R/WP labels are programme labels, not fabricated native IDs.
- Historical review's main/WebUI commits and CI describe older source. Current WebUI candidate CI is web-only; prior green CI does not prove current daemon/control integration.
- Complete local v9.1 archive exists and its dependency, NSI, 03B, Fleet and Web workbench contracts were inspected. Sandbox links embedded in older Fleet documents are not assumed accessible; current local contracts/native records supply consequential obligations.
- Current review session is configured Astra/high in local client metadata, not Sol/Medium. Prior delivery rollout records two early Astra/medium contexts then seven Sol/medium contexts after owner switch. No claim this review used economy lead routing, no additional model spend commissioned, no settings changed. Next execution must verify required binding.

Actual goal/owner steering is retained in [delivery session transcript][transcript]; native goal read used `thread_goals` in `/home/thetu/.codex/goals_1.sqlite` with `mode=ro`. The latest goal read reports 1,070,496 tokens and 5,892 seconds; this is not a cash figure or complete programme cost.

## 2. ROADMAP AND COMPLETION MATRIX

Levels: **S** specified; **I** inspected implementation; **T** tested at named boundary; **D** installed/deployed identity; **U** demonstrated useful outcome. All programme rows remain desired. T never implies D/U. “Historical T” means inspected retained evidence, not a fresh run here.

| Accepted outcome/source | Existing work ID | Evidence level | Current evidence | Remaining work/uncertainty | Dependency/next milestone |
|---|---|---|---|---|---|
| Bounded reviewed media change; kickoff / OMP-280 criteria | OMP-280 r1 | I/T; U at reviewable-patch boundary | Membership reconciliation, six fresh regression cases; exact reviewed/pushed commit | Real Plex roundtrip/pagination unverified | Separate authorized media deployment |
| Independent exact-candidate review/package | OMP-280; WP4 | I/T | Kimi source reviews; manifests/patches/archive verified | Some review test claims rely on worker logs; no native close audit | Preserve source acceptance; isolated integrated qualification |
| Useful inspect/pause/stop | OMP-280 controls; OMP-249/246 | I/T, no D/U | CLI fixture 11 cases; installer 5; real daemon/mock executable controls | Target visibility defect; no same-workflow installed worker proof | F1/F2 next delivery |
| Ordinary failure, cancellation, uncertain effects | OMP-233/246; completed 277 | I/T partial | Worker interruption/repair, fixture CAS, durable pending-operation code; historical installed recovery suite | New control path not bound to old installed evidence; mismatch stop usability unqualified | Exact integrated process tests |
| Queue/event/wait continuity; kickoff gate | Delivery queue job; OMP-246 boundary | T busy path; fixture lifecycle | Exact event sent/consumed once in actual transcript; result predated event | No predispatch record for this already-finished pilot; idle/disconnect/restart unverified | Retain process fallback; qualify affected boundaries only |
| Checkpoint/fresh context recovery | Existing R04 PACKET checkpoint | I; manual recovery demonstrated | This review recovered candidate, goal and evidence from records | No compaction hook proof; contradictory trailing status | Reconcile existing checkpoint during next execution |
| Installed baseline/ownership/full mirror | WP1/R00 | I/T partial | Source/live split verified; pinned ECC checkout exists | Complete classified mirror/discovery/effective activation and qualified release identity | Needed assets for each bounded delivery |
| Ownership-aware ECC adapter/install/update/remove/rollback | WP2/R01 | S, pilot method reuse | ECC assets/commit named; no adapter activation claimed | Full dependency closure, transformed hashes, ownership and lifecycle behavior | Preserve full programme; do not claim from method reuse |
| Engineering/domain/maintenance/research ECC packs | WP3/R01 | S + inspected pilot methods | Search-first, contract-first, verification, Python/TS and review assets identified | Runtime language/script-dependent/advisor discovery not demonstrated | Selected pack qualification; then broader coverage |
| Typed independent audit output | WP4; OMP-249 | I/T historical | Versioned effective audit policy and frozen-route source; preserved native authority | Final installed consumer and adversarial oracle qualification | Release-specific evidence |
| Complete economics/cheap routing | WP5; cost PR1–PR3; R18 | I/T components | Native budget hierarchy, stage binding, quotes and account identities in newer source; raw pilot usage | Cash/allowance/owner/reviewer/maintenance attribution incomplete | Measure next bounded delivery; matched cohort later |
| Adapted advisor specialization | WP6; Advisor owner | S; earlier source present | Existing roles and required review remain | Qualified adapted specialization/fresh-task benefit not established | Applicable pack; no mandatory extra model pass |
| Learning + lifecycle reuse | WP7A/WP7B | I/T components | Native proposal/use/outcome/withdrawal and pending-operation mechanisms | Complete learned-behavior/correction journey absent | Delivery 3 / FK-5–7 |
| Security diagnostics, upstream maintenance, release promotion | WP8; OMP-249 | I/T partial | Source negative checks and installer symlink safety | Full security pack, monthly qualified upstream candidate, compatible rollback/cutover | Isolated release first; live authority later |
| Intelligent interpreted claims, explicit constraints, ratification | NSI-1–6; 03B OMP-267–270 | S; partial contracts | v9.1 distinguishes semantic ratification from formal consistency; native E1–E4 descriptions | No full interpreted-intent-to-ratified-runtime journey demonstrated | Preserve H0/NSI admission gates; reuse bounded existing contracts |
| Divergence without premature ranking; valid-pool retention | OMP-267 r2 / 268 r2 | S, partial development | E1 owns shared job reuse; E2 runtime depends on qualified E1/NSI-4/H0 | Runtime proof/paid exploration authority remains; no generator/synthesis conflation | R03 shared substrate; no parallel scheduler |
| Autonomous engineering and parallel integration | OMP-233/246; native-tier owners | I/T historical + pilot | Native launch/recovery code; external scoped workers yielded reviewed commits | Pilot needed lead repairs/integration; no autonomous parallel useful-work cohort | Qualify one control/recovery path before breadth |
| Fleet capture/contracts/exact reads | OMP-278/279; FK-1–3 | I/T historical | Real knowledge package/native reads, retained concurrent/store evidence | Native records still BACKLOG; no full installed Fleet release | Shared authority/custody; retain accepted component results |
| Context, immutable Enola snapshots, inference | FLEET-5; FK-4/5 | I/T historical; services D only | Compiler and scoped snapshots; prior real Neo4j/reranker evidence | Integrated source/version/denied-scope/outage/rebuild and fresh worker benefit | Delivery 3; optional enrichment never authority |
| Correct later behavior across repositories | FK-6/7; WP7A; R12 | I/T components, no U loop | Withdrawal tests exclude fact-1 and preserve fact-2 in later compiled context | Controlled NullEngine case is not independently observed later-task improvement | Two real tasks across OMP/media-discovery + counterexample |
| Campaign/trial/lifecycle/compatibility | R02 | I/T source | d49004617e compatibility; ResearchStoreMixin; policy/reference/refusal tests | Observations intentionally untrusted; no experiment scheduler/evaluator implied | R03–R07 |
| Shared model/compute execution | R03; OMP-267/268; cost PR3 | I/T partial | Existing native stage/budget reservation, recovery and fencing | Complete common compute job, cancellation, runner settlement and process proof | Delivery 2 after selected qualification |
| Byte custody/source/dataset artifacts | R04 | I + recorded T; review incomplete | 23 unchanged dirty files, register/read 4 MiB verified bytes, migration0039 | No final Kimi verdict; collection/archive/source datasets/retention remain | Finish existing R04-S1 before depending on it |
| Isolated experiment runners | R05 | S + reusable runtime I/T | Existing isolation/staging; CPU/compiled-worker contracts specified | Actual research runner and independent environment reproduction absent | R03/R04; no broad GPU prerequisite for CPU slice |
| Protected evaluation/trusted empirical receipts | R06 | S + reusable audit | Explicit generation/selection/acceptance separation | Managed scorer, spoof/NaN/tamper/label isolation not demonstrated | Runner + immutable evidence |
| Managed engineering autoresearch bridge | R07 | Legacy I/T, managed S | createLogExperimentTool records agent metric/keep, warns on mismatch | Legacy loop not protected acceptance; complete managed campaign absent | R01/R03–R06 |
| Literature/source access/contradictions | R08 | S | Full access-depth/source-family/governance contracts | No qualified cited campaign found | Shared artifacts/jobs; later domain |
| ML/simulation/connected instruments | R09 | S; hardware/services available | Available inference listeners, retained GPU route work | Dataset split/leak/reproduction/backend evidence incomplete | One qualified domain; no invented connected instruments |
| Adaptive allocation and hypothesis revision | R10/R11 | S + typed vocabulary I | challenge/refine/replicate actions exist as proposals | No executed evidence-driven search/hypothesis/ablation loop found | First adaptive campaign; simple-loop comparator |
| Executable mechanism and platform improvement | R13/R14 | S | Bilevel mechanism protocol, frozen evaluators, qualified promotion specified | No fresh-task gain for generated executable mechanism/runtime candidate | Managed research then protected confirmation |
| Continuous/distributed and recursive methods | R15/R19 | S + foundations | Standing-policy/fairness/recursive cap requirements retained | No continuous campaign/failover or measured outer-method improvement | Later; maintain independent objectives/resources |
| Coherent WebUI/CLI/research reports | R16/R17; OWEB/WFM | I/T session/progress; research S | Thin control candidate, existing daemon/replay/files/terminal | Research evidence graph/report/reproduction, authoring, Monaco, anchored review, desktop/mobile/reconnect obligations remain | Monitoring/control first; backend-matched expansion |
| Deterministic workflow/plugin/supervision | CPK-0–6; OMP-202/206–208 | S + existing mechanisms | v9.1 and Fleet amendment preserve events/lifecycle/projections and supervision migration | No general framework completion inferred; removal needs replacement evidence | Reuse ordinary modules; defer scale-only framework |
| Evaluation portability and paid comparison | OMP-250/252; Harbor; R18 | S / older harness evidence | Existing native installed-process fixtures available | Harbor adapter and paid comparisons not established or required for this pilot | External evaluation adapter, never work authority |
| 20 consecutive accepted ordinary tasks | WP8/stabilization P8/P11 | S, no qualifying cohort found | Exact gate retained in stabilization guide | No success selection around failures; frozen recipe; no unplanned workflow repair | Operational adoption gate, separate from pilot |
| 72-hour mixed qualification | ADR0001; R18; earlier Q1–7 | S, no qualifying run found | 30 distinct ordinary tasks across two repos + 100 actual experiment attempts | Frozen release/config, coverage/fault strata, zero loss/duplicate/unauthorized/false acceptance/unplanned repair; measured responsiveness | Later sustained qualification, not launched here |

The complete [ECC specification][ecc], [research plan][research], [scope preservation][scope] and [qualification ADR][qualification] retain detailed criteria behind the matrix. Accepted Q1–35 also preserve collaborators from single-owner foundations, conversation/queue/dashboard consistency, optional-service degradation, separate recurring allowances/campaign reservations, capped recursive allocation, research data/provider permissions, pause versus immediate stop, retention, evidence-backed reports and compatible rollback. None is silently converted into a prerequisite for this next slice.

### Representative usefulness trace

1. **Intent/intake:** real September 11/12/17 Plex collection-sync HTTP400 reports led to six explicit OMP-280 criteria. This was competent source/ledger investigation, not evidence the full NSI compiler ran.
2. **Execution:** external Gemini edited only `brain/placeholder.py` and focused tests in the isolated media worktree. `collection_children_keys` paginates; `ensure_collection` computes absent unique keys, skips all-present PUT, retains create/readback/promotion and propagates failures. This removes duplicate-add behavior rather than swallowing HTTP400.
3. **Interventions:** lead interrupted an optional-memory scope excursion, resumed the same worker once, provisioned missing worktree venv, classified a full-suite failure against unchanged base, assembled source candidates and controls, repaired review findings. Owner corrected an invented handoff dependency and the mistaken native slash-command blocker. Do not call this unattended/no-rescue delivery.
4. **ECC:** pinned `8321021c54d670126ce3b2969d5deb880b4b0c2a` assets and adaptations are documented. Record explicitly says source fetched after candidate review. Credit inspected/adopted methods; causal attribution of earlier choices to this pack and actual installed pack use remain unproven.
5. **Review/integration:** separate Kimi media and controls reviews, latter with delta follow-up. Three exact branches/package exist. Package reconstructs components; it is not a tested running composition with the later dirty R04 candidate.
6. **Usable result:** owner can inspect/reconstruct a meaningful media repair now. Household recovery is not demonstrated without Plex validation/deployment. Control UI cannot presently manage that external Gemini job; native grant controls are a separate source capability.

A capable agent + ECC + basic experiment loop could also produce this patch. OMP adds durable work identity, candidate-bound evidence, recovery mechanisms and a potential inspection/control surface. This pilot measures neither incremental advantage nor a 10× multiplier.

### Verification actually performed

Each acceptance command ran as its own process.

| Command / root | Result | What it establishes |
|---|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .../brain/.venv/bin/python -m pytest -q -p no:cacheprovider brain/test_placeholder.py -k ensure_collection`, isolated media candidate | 6 pass, 3 deselected, exit 0 | Mixed/all-present/create/error/pagination transformations with controlled Plex calls. No DB/live Plex access. |
| `bun test session-system/tests/execution-control.test.ts session-system/tests/install.test.ts`, OMP controls | 16 pass, 298 assertions, exit 0 | CLI/CAS/privacy fixture and disposable installer behavior. |
| `bun test packages/daemon/test/progress-activity.test.ts packages/daemon/test/progress-project.test.ts packages/daemon/test/progress-control.test.ts`, WebUI controls | 40 pass, 223 assertions, exit 0 | Projection/liveness, real HTTP daemon, mock control executable, security/concurrency/timeout behavior. |
| `bun test packages/daemon/test/phase6-security-review.test.ts`, WebUI controls | 7 pass, 72 assertions, exit 0 | Separate existing adversarial daemon boundary suite; not part of historical “44” denominator. |
| `bun test queue-notify.test.ts`, economy directory | 5 pass, 19 assertions, exit 0 | In-memory notifier transitions, not process restart/cancellation proof. |
| `bun run test -- progress-panel.test.tsx`, candidate Web package | exit 127: vitest missing | Environment limitation. No new DOM verdict; no dependency install attempted. |
| `bun session-system/tools/execution-control.ts inspect --json`, OMP controls | exit 0, `{"ok":false,"code":"contract_mismatch"}` | Actual read-only fail-closed compatibility refusal, not success. |
| Package hashes, candidate tree/status, archive member comparison and three remote branch reads | pass/exit 0 | Exact saved/pushed source identities, not runtime efficacy. |
| R04 byte comparison against frozen tree/patch | 23 match, zero drift | Preservation only; no independent acceptance. |
| Supported WorkClient workflow/focus reads | exit 0 | Current native records only; no mutations. |

One attempted daemon filter `auth.test.ts` matched no file (exit 1); no test ran. Correct existing security suite was discovered and executed above. An ancestry query from main object database could not resolve the separate programme commit (exit 128); no ancestry conclusion drawn. Neither is a product failure.

Historical counts retained, not added to fresh counts: media 9 focused and 93 + 50 subtests full gate; OMP bun check; WebUI 44 daemon/security and 19 DOM/typecheck. Raw [review streams][control-review] show independent source inspection, partial reruns and explicit reliance on worker transcripts. Their PASS cannot be promoted to end-to-end semantic independence. Existing installed tests use scripted model/audit responses with real processes/PostgreSQL; useful for lifecycle, not proof a live model judges correctly. See [installed recovery tests][installed-tests], especially `SCRIPTED_PASS_REPORT` and tests around lines 5923–6130.

Checks not run: full media suite (touches real DB), production Plex write, whole OMP suite/build, PostgreSQL/installed staging, real browser against synchronized service, compaction/disconnected queue, live GPU/knowledge qualification, restore drill, 20-task or 72-hour run, new provider comparison. Their smallest useful follow-ups are scoped in findings/next delivery; no production probe is required merely to fill this report.

## 3. PRIORITIZED FINDINGS

### F1 — Control target is not visible before acting
**Priority P1; defect in newly delivered control UX. IDs: OMP-280 controls, OMP-249 integration.**

User can mistake a button beneath Codex coordinator activity for control of that work. [ProgressPanel.tsx:456][panel] renders Pause/Stop whenever native control is live; `activeWorkKey` is shown only in paused-resume text. Stop dialog receives callbacks/in-flight state but no target. [runControl][control-cli] resolves `client.execution("")`, fences the current native grant and calls `setExecutionState`. It has no external-job cancellation integration. OMP-280 has no corresponding grant.

Smallest remedy: explicitly name native work/grant target before Pause and in Stop confirmation; distinguish unrelated external activity. Preserve version/freshness fences; disable/refuse stale or unresolvable targeting. Do not create a second authority or pretend native grants stop external Codex processes. Acceptance: browser shows two distinct activities and a control action affects only the named disposable native workload; stale selection cannot silently retarget. Existing single-flight/CAS/security tests stay green.

### F2 — Control and recovery evidence stops before the integrated installed path
**Priority P1; missing validation plus operational/authority boundary, not evidence production failed. IDs: OMP-249 r4, OMP-246 r2, preserve OMP-233/277.**

Current wrapper absent; source CLI/service contract mismatch reproduced. OMP tests use `createMockWorkService`; daemon tests spawn a mock executable. Historical real-process recovery does not include this UI→CLI composition. Completion report acknowledges live qualification remains, but calls source/fixture cancellation “Proven.”

Smallest verification: reuse [runtime staging][stage] and [installed harness][installed-support] with disposable PostgreSQL, identities, ports and home. Install matched components in isolation; drive actual browser/daemon/CLI/controller/worker. Test pause preventing new starts, bounded cancellation evidence, committed-but-unobserved response, restart/reconnect and late results. Do not patch live approval to match source, use Q36 grant, or require owner slash commands for Codex work. Acceptance: exact manifest, raw process/service events, one logical effect after retries, terminal cancellation remains authoritative, compatible recovery/rollback; required tests cannot silently skip.

Keep accepted source work. This is the next release qualification increment; it does not claim all OMP-249/246 criteria complete.

### F3 — Continuity coverage and checkpoint wording overstate what was proved
**Priority P2; missing validation/record conflict. IDs: OMP-246; existing queue job/checkpoint.**

Busy-turn queue event is real and correctly source-bound. Five notifier tests are useful but the cancellation test checks a terminal payload; it never dispatches/cancels/restarts a worker. `deliver` tests do not exercise CLI durable writes. `deadlineAt` is recorded, not enforced by notifier; actual supervision remains launcher/process responsibility. Predispatch record was absent because queue requirement arrived after the real job finished; this is disclosed. Idle/disconnect/restart are expressly unverified. Safe fallback is allowed, so queue adoption is not a release blocker.

Checkpoint's last paragraph says active/incomplete after earlier completion correction. Smallest remedy: update the same checkpoint with explicit current identity/status and fallback limits; qualify actual launcher/harness recovery for next workload, persist results before notifications, consume exact current events once. Acceptance: fresh context reconstructs active/finished/uncertain attempts and does not rerun uncertain work; cancellation/late event does not launch again. No scheduler/daemon/status LLM loop needed.

### F4 — Costs are candidly incomplete; benefit remains unmeasured
**Priority P2; missing validation/accounting, not failure of the pilot's explicit-unknown criterion. IDs: WP5/R18; existing cost-control owners.**

Raw final Gemini metadata totals 753,361 media + 3,613,473 controls = 4,366,834 reported tokens. Do not add earlier cumulative results again. Final metadata separately reports cache reads 3,764,380 + 40,917,367; vendor “total” semantics must not be assumed to include or price them. Native goal records 1,070,496 Codex tokens/5,892 seconds, but per-item/model attribution and allowance unavailable. Fable, Kimi, cash, owner time, integration labor and amortized maintenance remain unknown. One media interruption, two worker repair turns across components, three Kimi reviews and one Fable plan are recorded.

Smallest remedy: capture available per-attempt usage, lead turns, elapsed/provider wait, active owner minutes and interventions during next delivery using existing records; preserve unknown prices. Keep operating and investment costs separate. Later compare matched useful tasks against competent agent + ECC + basic loop, including failures and maintenance. Acceptance: denominators and failures visible; no inference of savings from fewer queue messages or “cheap” model names. No new subscription or automatic routing change.

### F5 — ECC pilot use and R04 progress must not be promoted into programme completion
**Priority P2; missing implementation/validation in planned later scope. IDs: WP1–WP3/R01; R04.**

[ECC reuse record][ecc-reuse] is method/provenance evidence; post-review fetch cannot prove earlier pack activation. R04 dirty candidate genuinely stores/verifies bytes, and retained worker checks are substantial, but reviewer has no final verdict. Neither requires reimplementation.

Smallest remedy: qualify needed ECC assets through existing installer/discovery seams when next pack is used; preserve full mirror/update/remove/rollback requirements. Finish exact frozen R04 review before research consumes it; retain source/dataset/archive/reproduction obligations. Acceptance: one language rule, script-dependent research asset and advisor adaptation work through native discovery; R04 acceptance binds actual retained bytes and authorization. Do not broaden next control slice to full ECC/research.

### F6 — Research and learning closed loops are still future delivery
**Priority P2; planned later scope with missing implementation/validation. IDs: R03–R19, OMP-267/268, OMP-278/279/FK-5–7.**

`ResearchStoreMixin` implements campaign/trial/component/artifact records. ADR0010 explicitly defers scientific validity/trusted evaluation to R06. Legacy `createLogExperimentTool` accepts agent metric/status and records disagreement as a warning; it is not the managed protected oracle. Knowledge withdrawal test `test_context_compile_excludes_withdrawn_engine_facts` proves later compiled context excludes withdrawn fact-1 while retaining fact-2, using NullEngine and real disposable stores. This is meaningful correction behavior, not a fresh engineering task becoming better.

Smallest next research delivery after controls: existing R03/R04→R05–R07 plus initial R10/R17, one real adaptive engineering campaign: baseline, alternative, failed candidate, evidence-driven revision, protected evaluation, restart/cancel and clean reproduction. Keep basic-loop comparison. Then actual two-repository lesson/use/outcome/counterexample journey. Recursive method/platform improvement remains separate capped R13–R19 work; no paper-derived speedup or automatic promotion.

### F7 — Green summaries and old qualification do not cover current candidate
**Priority P2; missing validation. IDs: OMP-249, WP8/R18, OWEB.**

Current candidate [WebUI CI][web-ci] only runs web typecheck/tests; daemon and browser lifecycle not required there. `model-commands.test.ts` early returns when stub unavailable. Installed OMP fixture explicitly skips when release/manifest variables are missing. These may be legitimate optional developer checks, but cannot count as qualification. Required source approval test excluded in some candidate research runs because live approval was intentionally unchanged; that is not whole-suite green.

Smallest remedy: exact integrated qualification command makes missing required dependencies/coverage fail visibly; preserve optional skips in ordinary suites without reporting them as success. Acceptance: required suite results, no mandatory skips, exact source/runtime/manifest identity, negative oracle cases. Do not create duplicate harnesses.

### F8 — Simplification should reduce duplicate orchestration and stale views, not erase foundations
**Priority P3; optional improvement, bounded by existing work.**

Keep WorkService authority, native pending-operation journal, content custody, independent review, privacy projections and candidate identities. Existing control bridge already reuses WorkClient/backend and daemon collector; that is sound reuse. Preserve scoped knowledge/compiler and accepted lifecycle code.

Combine only overlapping implementation when proven: control-specific and native recovery tests through installed harness; research/intake compute dispatch through existing shared job/budget owner; knowledge validity/context through one native contract. Treat main MASTER, isolated MASTER and appended checkpoint as dated views with one current pointer, not three independent authorities. Retire stale “active” assertions and any duplicated retry/launch path only after coverage transfers.

Do not replace legitimate trust-boundary validation with a cast, remove provenance/cancellation to reduce line count, add a plugin framework/broker, force all models into every pass, or collapse adaptive reasoning into a fixed user flow. The ~5,459-line WebUI candidate includes previously developed progress UI and tests; it is not all marginal cost of the small media fix. No justified broad code deletion established in this review. Tradeoff: retaining local evidence/disposable harness costs some maintenance but avoids authority migration and lost auditability. Upstream ECC adaptations require pinned provenance and reviewed updates; no upstream fork rewrite justified.

## 4. NEXT DELIVERY DECISION

**Choose one bounded release increment: a clearly targeted, installed-in-isolation OMP/WebUI control workflow with actual worker recovery proof.** Reuse OMP-280 source, OMP-249 release ownership and OMP-246 recovery ownership; preserve OMP-233 and completed OMP-277. No reopening or whole-item closure proposed.

Why this outranks immediate adaptive research: a reproducible target-visibility defect exists; compatibility refusal is observed; actual stop/recovery behavior is not yet shown through the new control surface. Solving these gives owner a useful, intelligible control path and provides the same reliable execution boundary research will need. It advances beyond the valid source-delivery completion. A full rewrite, full Fleet release, model tournament, generic queue upgrade or 72-hour run would add scope before resolving this concrete boundary.

Prerequisites: reconcile supported work records and source candidates; retain current dirty R04 separately; provision only disposable test resources using existing authority/fixture setup; verify exact model routes and resource allowance. Production Q36 is not a prerequisite. Existing OMP-249 plan/evidence binding must be rechecked if modifying its referenced guide; preserve historical stale-by-design binding rather than silently reusing it.

Observable acceptance:

1. UI explicitly identifies control target before Pause/Stop and confirmation; external coordinator/worker activity cannot be mistaken for target. Stale target/version rejected.
2. Matching staged OMP, WorkService, wrapper and WebUI run from one immutable candidate manifest in disposable state; both clients inspect the same grant/work state.
3. Actual worker/process machinery demonstrates pause admission, deliberate stop, bounded termination observation, no new effects from late/canceled work, committed response-loss reconciliation and restart/reconnect without duplicate effects.
4. A fresh context reconstructs state and saved results through the existing checkpoint/journal; queue fallback remains honest and no routine LLM polling occurs.
5. Existing media regressions retained; relevant TypeScript/UI/daemon, PostgreSQL and installed suites pass on identified integrated candidate. Protected negative cases and required skips are explicit; one independent frozen-candidate review.
6. Reconstructible package, raw command/exits/events, compatibility/rollback evidence, known limits and complete available usage/owner-effort record delivered. No live cutover implied.

Non-goals: production Plex repair activation, live native grant migration, Q36, arbitrary control over Codex external jobs, all OMP-249/246 criteria, full ECC installation, R04 completion, Fleet deployment, adaptive/recursive research implementation, 20-task/72-hour qualification, broad UI redesign, subscriptions or purchases.

No new product-preference decision blocks isolated implementation. Required execution model is Sol/Medium; bind that before activating continuation. Any missing authority for a concrete live action stays separate: prepare exact reviewable proposal and continue independent work. Owner cutover and Q36 remain withheld. Do not ask owner to run native OMP slash commands to make Codex development possible.

Next existing programme work after this increment: complete/review preserved R04-S1 and continue R03 shared execution toward Delivery 2's first adaptive experiment-to-reproduction slice, preserving OMP-267/E1 and OMP-268/E2 dependencies. Delivery 3 remains two-repository learning/correction; later full research and qualifications remain mandatory programme outcomes.

### Source index

All filesystem links below were located; they are evidence, not newly invented handoffs.

[package]: /home/thetu/.codex/workflows/economy/artifacts/delivery1-omp280-20260918/DELIVERY-PACKAGE.md
[media-manifest]: /home/thetu/.codex/workflows/economy/artifacts/delivery1-omp280-20260918/FROZEN-MANIFEST.json
[control-manifest]: /home/thetu/.codex/workflows/economy/artifacts/delivery1-omp280-20260918/CONTROL-FROZEN-MANIFEST.json
[checkpoint]: /home/thetu/.codex/workflows/economy/artifacts/omp233-shared-job-inventory-20260916/research-artifacts-r04-s1-20260918/PACKET.md
[candidate-master]: /home/thetu/.codex/workflows/economy/artifacts/omp233-shared-job-inventory-20260916/admission-checkout/MASTER.md
[kickoff]: /home/thetu/oh-my-pi/OMP_Codex_Implementation_Kickoff.md
[decisions]: /home/thetu/oh-my-pi/docs/programme/ACCEPTED-DECISIONS.md
[transcript]: /home/thetu/.codex/sessions/2026/09/18/rollout-2026-09-18T02-08-27-01a0b245-b374-7841-923c-7650ecc99a46.jsonl
[ecc]: /home/thetu/oh-my-pi/docs/programme/ECC-IMPLEMENTATION-SPEC.md
[research]: /home/thetu/oh-my-pi/docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md
[scope]: /home/thetu/oh-my-pi/docs/programme/SCOPE-PRESERVATION.md
[qualification]: /home/thetu/oh-my-pi/docs/adr/0001-autonomous-execution-and-qualification.md
[control-review]: /home/thetu/.codex/workflows/economy/artifacts/delivery1-omp280-20260918/control-reviewer2-stream.jsonl
[installed-tests]: /home/thetu/oh-my-pi-worktrees/omp-280-controls/python/omp-work/tests/test_installed_execution_recovery.py
[installed-support]: /home/thetu/oh-my-pi-worktrees/omp-280-controls/python/omp-work/tests/installed_runtime_support.py
[stage]: /home/thetu/oh-my-pi-worktrees/omp-280-controls/session-system/runtime/stage.ts
[panel]: /home/thetu/omp-webui-worktrees/omp-280-controls/packages/web/src/components/ProgressPanel.tsx:456
[control-cli]: /home/thetu/oh-my-pi-worktrees/omp-280-controls/session-system/tools/execution-control.ts:193
[ecc-reuse]: /home/thetu/.codex/workflows/economy/artifacts/delivery1-omp280-20260918/ECC-REUSE.md
[web-ci]: /home/thetu/omp-webui-worktrees/omp-280-controls/.github/workflows/ci.yml

Additional essential sources inspected: main MASTER and CONTEXT; docs/programme/ORIGINAL-OBLIGATIONS.md; local v9.1 ZIP (03B SHA256 c5e39ca78d03e011733fa8cb5ab1a0dd4e6da1066d5c97b67130b797e4c01e75); candidate docs/omp-intake-exploration-implementation-plan.md and docs/omp-stabilization-plan.md; Fleet OMP_Immediate_Roadmap_Amendment.md under /home/thetu/.local/state/omp-fleet-knowledge/worktree/docs/plans/fleet-knowledge; /home/thetu/.local/state/omp-cost-control/20260915/USER-HANDOFF.md; current economy POLICY.md/MODELS.md, queue-notify.ts/tests; delivery COMPLETION-AUDIT.md, QUEUE-VALIDATION.md, queue-job.json, usage-record.json and raw worker/reviewer streams; programme research ADR0010/0012, ResearchStoreMixin, research tests and knowledge correction tests. Historical reference recommendations are not assumed to be accepted policy.

## 5. READY-TO-PASTE CONTINUATION GOAL

Goal body: 3891 characters, excluding fence/newline delimiters. Not activated or executed.

```text
Deliver an isolated, usable OMP/WebUI inspect-pause-stop workflow for the reviewed OMP-280 candidate, addressing F1-F3 in docs/report/omp-completion-continuation-review-2026-09-18.md. Use existing OMP-249 release and OMP-246 recovery ownership; preserve OMP-233 criteria and completed OMP-277 behavior. Scope: isolated-install qualification; no production activation or whole-item closure.

Read AGENTS.md, MASTER.md, OMP_Codex_Implementation_Kickoff.md, OMP_Product_Platform_Review_2026-09-18.md and docs/programme/ACCEPTED-DECISIONS.md. Continue candidates in /home/thetu/oh-my-pi-worktrees/omp-280-controls (96f511b4) and /home/thetu/omp-webui-worktrees/omp-280-controls (0a2e0cd). Preserve media candidate 85be7980, newer changes, IDs and owners. Reconcile work records/dependencies; keep unfinished R04 separate.

Implement, test, integrate, package:
1. Identify actual WorkService work/grant target before action and confirmation. Separate that target from Codex coordinator/external-worker activity. Refuse stale/mismatched requests; show unavailable controls honestly. Do not invent authority over external jobs or require owner OMP slash commands for Codex development.
2. Reuse session-system/tools/execution-control.ts, session-system/runtime/stage.ts and python/omp-work/tests/installed_runtime_support.py in the OMP candidate; reuse packages/daemon/src/progress/control.ts and packages/web/src/components/ProgressPanel.tsx in the WebUI candidate. Stage compatible immutable OMP, WorkService, wrapper and WebUI in disposable state through supported test authority; never edit live approval or fabricate production grants.
3. Prove real browser -> daemon -> installed CLI -> disposable WorkService -> worker behavior: inspect, pause admission, deliberate stop with bounded termination evidence, repeated/stale requests, committed response loss, restart/reconnect, late completion after cancellation and fresh-context recovery. Reconcile uncertain effects before retries; no duplicate transition, launch or effect. Scripted model inputs may bound cost; mocks cannot replace the lifecycle machinery. Retain media assertions and failures.
4. Run applicable package checks and exact-candidate PostgreSQL/installed suites without counting required skips. Independently review frozen integrated changes, retain raw commands/exits/identities, then package a reconstructible release and compatibility/rollback evidence. Report user results, owner effort, lead/external/reviewer/retry/integration/recovery/maintenance usage and unknowns; no unmeasured savings.

Reuse the execution checkpoint at /home/thetu/.codex/workflows/economy/artifacts/omp233-shared-job-inventory-20260916/research-artifacts-r04-s1-20260918/PACKET.md and native goal support. Save results before notifications; use validated queue/events/waits, deduplication, deadlines, cancellation and safe fallback without routine LLM polling. Update superseded checkpoint status without a new ledger.

Verify GPT-5.6 Sol Medium lead; Gemini 3.8 Flash implementation, Fable 5.1 only difficult planning, Kimi independent review, Astra only authorized exceptional escalation. Follow current economy POLICY/MODELS and cumulative allowances; reassess after two failed repairs. Simplify only with preserved behavior/evidence; no change is valid.

Preserve ECC WP1-WP8, intelligent NSI/exploration, autonomous engineering, Fleet/context/knowledge/correction, adaptive/bilevel/recursive autoresearch including platform improvement, coherent WebUI, and separate 20-task/72-hour qualifications. Later scope stays open. No production cutover, destructive actions, purchases or withheld Q36 without applicable authority. Stop affected work on integrity/authority failure or exhausted limits after independent work; completion requires exact integrated evidence. Next: existing R03/R04 -> first adaptive experiment-to-reproduction delivery.
```

