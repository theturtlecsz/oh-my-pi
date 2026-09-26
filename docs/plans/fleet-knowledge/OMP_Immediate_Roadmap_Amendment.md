# OMP immediate roadmap amendment: Fleet Knowledge v1

Prepared 11 September 2026. Revised after the seven-candidate implementation review.

**Recommendation:** make Fleet Knowledge v1 the next major capability in the OMP fork. Start its isolated implementation alongside the remaining, evidence-backed stabilization work. Bring the shared evidence contracts and context compiler forward. Deliver the complete capture → retrieval → reuse → outcome → correction loop in the first knowledge release.

Use the [OMP Memory Candidate Decision Report](sandbox:/workspace/scratch/c5f3674f4c15/OMP_Memory_Candidate_Decision_Report.md) as the current architecture decision: native OMP knowledge/evidence semantics, Cognee as the lead knowledge engine, independently usable Enola code facts, and the RTX 5090 for suitable inference. Hindsight + Enola and OpenViking + Enola are primary alternatives. This supersedes the blueprint's premature vendor, free-database and local-only inference commitments. Public benchmark campaigns and model comparisons remain deferred.

This is a prepared roadmap amendment and implementation handoff. It does not claim that native issues, repository files, services, or the deployed installation have been changed.

## 1. Baseline and what changed

The relevant planning baseline is **v9.1 plus its subsequent implementation and stabilization decisions**, not the original ZIP alone.

| Evidence inspected | What it establishes | Consequence for this amendment |
|---|---|---|
| v9.1 dependency map and Fleet/NSI/CPK contracts | NSI-1/2 already describe claims, evidence and decisions; FLEET-5 already owns stage context and a symbol map; CPK describes committed events and disposable projections. | Reuse and extend those contracts. Avoid a second evidence vocabulary or independent context compiler. |
| September 6 implementation/Harbor handoff | Reliability, routing, native structured amendments, recovery, trusted evidence and a separate evaluation adapter already have an implementation plan. | Reconcile its work with current source before scheduling anything again. |
| September 8 Bookends review and stabilization guide, read at current main | A dated native backlog map identifies specific stabilization, CPK, routing and Harbor items. The guide explicitly defers broad Fleet work until live promotion. | Amend that development-order restriction for this bounded program. Preserve actual acceptance and deployment requirements. |
| OMP main at `aacabebf7c9d894ff8dca4b9fff188303b2491f4` | Source is 86 commits beyond the September 6 handoff pin. Changes include installed-runtime isolation, continuation and child-task recovery, criteria-seal reconciliation and audit-launch attribution. | Do not recreate delivered fixes from old descriptions. Source delivery does not establish live deployment or native issue completion. |
| Current recursive repository tree | No `evals/harbor/` or proposed `python/omp-knowledge/` package was present. | Treat those paths as planned; check local branches before creating them. |
| Live WorkService and 5090 host | Not available in this review. | Native issue revisions, current grants, installed release and actual local-model fit require local readback. No benchmarks were run. |

Sources: [current fork](https://github.com/theturtlecsz/oh-my-pi/tree/aacabebf7c9d894ff8dca4b9fff188303b2491f4), [changes since the September 6 pin](https://github.com/theturtlecsz/oh-my-pi/compare/9b4a0c2b7146fb739d8be2d04cd5736212bfd867...aacabebf7c9d894ff8dca4b9fff188303b2491f4), [Bookends review](https://github.com/theturtlecsz/oh-my-pi/blob/aacabebf7c9d894ff8dca4b9fff188303b2491f4/docs/bookends-open-work-review.md), [stabilization guide](https://github.com/theturtlecsz/oh-my-pi/blob/aacabebf7c9d894ff8dca4b9fff188303b2491f4/docs/omp-stabilization-plan.md).

The September 8 review identifies OMP-202 as the CPK umbrella and OMP-208 as supervision migration. Its issue mappings are retrieval leads, not current status. Do not carry forward the older handoff's shorthand “OMP-202/H5” as an exact ownership mapping.

## 2. The decisions to encode now

1. **One program, with shared ownership.** Create or amend a native Fleet Knowledge umbrella after checking for an existing equivalent. Place actual implementation under the matching native work items; the umbrella links the complete release outcome.

2. **Native knowledge can be durable structured data.** WorkService owns admitted decisions, evidence acceptance, procedure versions and their policy-controlled lifecycle. Git remains the source authority; retained artifacts preserve exact observations. The selected knowledge engine manages derived retrieval and processing; Cognee is the source-fit lead implementation. Earlier “knowledge only in files” guidance is superseded for this design.

3. **Knowledge development starts before broad Fleet promotion.** Permit its isolated implementation after applicable source/schema prerequisites. Its current-workflow integration can deliver value before the multi-item scheduler exists. Do not make completion of the entire CPK framework, NSI reasoning system or autonomous Fleet a prerequisite for the reusable knowledge components.

4. **Share contracts early; retain Fleet activation gates.** Factor reusable source, provenance and evidence contracts from NSI-1/2. Factor reusable context assembly from FLEET-5. Autonomous Fleet still consumes the required ratified intake, risk policy, grants and stage authority when activated. Building a library earlier does not mark an NSI or Fleet gate passed.

5. **Use the 5090 where it provides qualified value.** Prefer local candidates for embeddings, reranking and routine extraction, with explicit provider and fallback routes. Select difficult synthesis/reflection models by quality; paid inference and database editions remain eligible. Code extraction and authority checks run on CPU. Preserve independently qualified OMP worker/reviewer routes until a separate routing change is qualified.

6. **Make learning and correction part of v1 acceptance.** A deployment that only searches transcripts does not complete this program. Evidence lineage, candidate isolation, later reuse, counterexamples, supersession and withdrawal belong in the initial release.

7. **Prepare evaluation hooks; run no benchmarks now.** Preserve the Harbor direction and collect reproducible operational traces. Defer LongMemEval, LongMemEval-V2, cross-engine comparisons, paid model trials and broad performance experiments. During implementation, ordinary correctness checks remain necessary and distinct from benchmarking.

## 3. Roadmap crosswalk

| Existing owner or work lead | Proposed change | Boundary |
|---|---|---|
| SQL-0 / OMP-249 installation qualification | Read actual PostgreSQL qualification and installed-runtime evidence; reuse what passed. | Do not restart an implemented migration. Native schema/runtime changes require the applicable contract and migration process. |
| OMP-233 / OMP-246 and subsequent recovery fixes | Reuse stable operation identities, session recovery and replay semantics for knowledge capture. | An ingestion queue cannot become another workflow scheduler. Reopen only a demonstrated remaining gap. |
| Stabilization phase 3: trusted runner evidence | Link the actual existing owner, or admit one narrowly scoped missing item after reconciliation. | Observations may be ingested immediately; promotion to verified procedures depends on qualified evidence. |
| NSI-1 / NSI-2 | Bring shared source, claim, evidence, decision and provenance contracts forward into FK-1. | Keep intake-specific semantic ratification and readiness within NSI. Avoid duplicate Decision/Evidence models. |
| FLEET-5 | Split reusable compilation/retrieval work from later Fleet launch binding. Add immutable knowledge versions and code snapshot sets. | Current workflow uses the same compiler; Fleet later supplies its required manifests and risk policy. |
| OMP-206 / CPK typed seams | Consume the specific event/API seam needed for native committed facts. | No prerequisite to finish the general plugin kernel or rewrite all events. |
| OMP-207 / projection work | Reuse transport and projection contracts for the knowledge inspection surface where they fit. | Do not wait for broad Web/Fleet UI completion. |
| OMP-208 / Advisor-Observer migration | Link findings to attributed proposals and common learning records. | Adding learning capture does not retire existing supervision; retirement retains its own evidence gate. |
| OMP-219 queue qualification / later FLEET-13 | Preserve independent scheduler work. | First knowledge release works with bounded single-item execution and already isolates concurrent candidate snapshots. |
| OMP-241 routing | Add explicit knowledge-service routes with local GPU candidates and declared alternatives, without changing the worker comparison profile. | Memory inference and coding/reviewer model selection are separate decisions. |
| OMP-250 / OMP-252 | Reuse Harbor integration and later evaluation ownership; add knowledge trace/export requirements where relevant. | No second benchmark framework and no benchmark runs in this tranche. |

The OMP aliases above come from the dated source review. The local implementing agent must retrieve full current records, structured criteria and revisions before applying this mapping. A title match, old snapshot or merged PR is insufficient.

## 4. Immediate work packages and dependency order

**FK-0 through FK-7 are proposed planning labels, not allocated Work Ledger aliases.** Each row is a reviewable work package; split a large package into coherent PRs without weakening its acceptance criteria.

| Order / label | Deliverable | Dependencies | Observable acceptance |
|---|---|---|---|
| **FK-0 — Reconcile and amend** | Current source/deployment/ledger map; exact existing-item amendments; one architecture decision and release definition. Record Cognee as lead, Hindsight/OpenViking as primary alternatives and conditions for changing the selection. | Read current native records and applicable instructions. | Every requirement has an existing owner or a justified new item; no duplicate backlog; explicit development-order change and provisional vendor choice recorded. |
| **FK-1 — Native knowledge contracts and complete reads** | Shared source/evidence/decision contracts, lesson/procedure lifecycle, immutable context/publication identities; complete paginated or cursor-based reads and receipt/artifact resolution. | FK-0; applicable SQL/contract prerequisites. | Exact IDs and revisions resolve independently of the capped tree view; changed evidence cannot retain prior acceptance; stable repository identity spans worktrees. |
| **FK-2 — Capture and replay** | Transactional native outbox, content-addressed artifacts, attributed tool/stage outcomes, resumable ingestion jobs and status. | FK-1; relevant recovery contracts. | Crash/replay cannot lose a committed event or duplicate an admitted record; conflicting payload hashes refuse; errors and partial attempts remain visible. |
| **FK-3 — Knowledge engine and inference services** | Implement the pinned Cognee path behind a small native adapter contract; retain Hindsight/OpenViking adapter seams. Define qualified data services, local embedding/reranking/extraction candidates, stronger-model alternatives, bounded queues and health information. | FK-0 and agreed FK-1 API boundary. | Isolated processing preserves source identity and scope; declared local routes operate; provider/fallback routes are recorded; service failure does not rewrite workflow state. Record remaining qualification limits. |
| **FK-4 — Versioned code graph** | Independently usable Enola extraction, repository/snapshot namespace adapter, coverage manifest, staged publication and retained active snapshots. Bind to Cognee through its adapter without making extraction vendor-dependent. | FK-1; agreed structural storage/query seam, with FK-3 needed for Cognee publication. | Two candidates can coexist; ingesting one cannot change the other's structural query results; partial snapshots remain unpublished; alternatives can consume equivalent structural inputs. |
| **FK-5 — Native context compiler** | Shared FLEET-5 implementation, current-workflow stage hooks, exact/semantic/structural retrieval, reranking and persisted context bundles. | FK-2 + FK-3 + FK-4. | A fresh worker receives the correct revision, evidence, code snapshot and applicable knowledge within budget, with a reproducible record of what it received. |
| **FK-6 — Learning, correction and retraction** | Trace-to-lesson proposals, evidence-bound acceptance, versioned procedures, later-use outcomes, contradiction handling and invalidation. | FK-1 + FK-2 + FK-3; trusted-evidence qualification for verified promotion. | A later task can reuse a supported procedure; counterevidence narrows or withdraws it; stale graph/cache results cannot bypass native validity. |
| **FK-7 — Complete release qualification and visibility** | Integrated journeys, minimal CLI/Web inspection, local runbook, source import, backup/rebuild and rollback procedure. | FK-5 + FK-6; relevant installed-runtime and evidence gates. | The complete loop and failure cases below pass on the candidate installation; activation follows the existing deployment procedure. |

Start FK-1 and FK-3 as coordinated workstreams after FK-0 establishes their interface. FK-4 and FK-6 can proceed independently once their prerequisites exist. Integrate them through FK-5/FK-7. Do not use package ordering as permission for multiple writers to mutate the same authority or worktree.

The immediate engineering priority is **shared contracts + knowledge and inference service deployment definitions**, alongside completion of any remaining critical stabilization evidence. Generic CPK expansion, broad UI redesign, fleet-wide concurrency and benchmark infrastructure expansion move behind this release. Fix any reproduced reliability blocker in its existing owner rather than hiding it inside the knowledge epic.

## 5. What the first complete release must demonstrate

Use OMP itself for the first integrated case, then qualify media-discovery for the second repository. These are acceptance journeys to implement and run later; none was executed for this roadmap.

1. **Capture real engineering evidence.** A task records its revision, candidate, commands, results, artifacts, audit and outcome. Failed attempts and corrections retain their provenance.
2. **Build the exact code view.** Enola indexes that source snapshot; extraction coverage and unsupported constructs are visible.
3. **Produce a bounded lesson.** The selected engine or proposal component proposes a procedure with preconditions, supporting evidence, limits and failure conditions. Native policy admits only the scope justified by the evidence.
4. **Reuse it in a fresh task.** The context compiler retrieves the procedure, its rationale and the relevant code/evidence. The later worker can inspect source material and records whether it used the procedure.
5. **Learn from the later outcome.** Record support, non-applicability or a counterexample. Retrieval alone is not evidence of usefulness or causality.
6. **Correct it completely.** Withdraw or supersede evidence and ensure subsequent retrieval cannot present the old claim as current. Graph/vector/cache cleanup may be asynchronous; native validity checks are immediate.
7. **Survive operational failure.** Exercise duplicate events, source changes, interrupted publication, GPU/service unavailability and restart. Required evidence failures remain explicit; optional enrichment can degrade under declared policy.

Release acceptance also covers:

- Candidate A and candidate B in the same repository, plus two repository identities with permitted and forbidden cross-repository retrieval.
- A fresh auditor receiving independently inspectable evidence without inheriting the implementer's correctness conclusion as an instruction.
- Rebuilding graph/vector projections from retained sources and accepted records.
- Import completeness and restartability using native exact/cursor reads rather than a possibly capped tree.
- Existing Hindsight records or transcript summaries entering as historical claims with source lineage, without being promoted to verified knowledge merely by import. Hindsight may also become the selected primary engine if later evidence supports it.
- A minimal inspection surface showing the original evidence, acceptance state, applicability, selected context, supersession and ingestion failures.

This is a complete functional release with bounded initial task coverage. Scheduler scale, additional languages and ranking improvements can follow without deferring the central learning lifecycle.

## 6. RTX 5090 scope and engine selection

Use the 5090 as a shared inference service. The source review supports local candidates for repeated embedding, reranking and bounded extraction jobs; it does not establish the best model artifacts or production concurrency.

| Function | Initial treatment | Qualification requirement |
|---|---|---|
| Embeddings | Local GPU candidate; the blueprint's Qwen3-Embedding-0.6B profile remains one option | Pin model identity, revision, query/document preprocessing, pooling and dimensions. A model change requires index migration/rebuild even when dimensions match. |
| Reranking | Local GPU candidate; Qwen3-Reranker-0.6B remains one option | Implement the actual pair-scoring contract and establish acceptable retrieval quality and delay. |
| Routine extraction and lesson proposals | Bounded local generation candidate | Qualify structured outputs, cancellation, source fidelity and context limits. The earlier Qwen3.5-35B-A3B suggestion is unqualified, not a fixed requirement. |
| Difficult synthesis and reflection | Best qualified local or hosted model | Quality takes priority over local residency; provider choice and fallback are explicit. |
| Code parsing, graph operations and authority checks | CPU/data services | Reserve GPU capacity for inference where it helps. |
| Coding and independent review | Existing qualified OMP routes | Change separately when supported by evidence. |

Prioritize active context requests over background backfill. Start with bounded generation concurrency and qualify actual residency on the host. Keep any CPU embedding fallback compatible with the active vector generation. Do not advertise throughput, concurrency or cost savings percentages before measurement.

Select the graph backend for required isolation and operational behavior. Neo4j Community is an option, not a budget-imposed requirement; Enterprise or another supported backend remains eligible. Do not assume arbitrary free multi-database support or create a container per task/snapshot. Keep engine-owned migrations separate from native OMP records. Versioned code publication and scoped retrieval are required adapter work.

The [candidate decision report](sandbox:/workspace/scratch/c5f3674f4c15/OMP_Memory_Candidate_Decision_Report.md) contains primary sources and implementation gaps. Its three primary architectures are native OMP + Cognee + Enola, native OMP + Hindsight + Enola, and native OMP + OpenViking + Enola. MemOS is a procedural-learning specialist; Graphiti a temporal foundation alternative; Local Memory and IAI retain narrower evaluation roles.

Build the Cognee path first without designing a universal plugin framework. Change the lead if its snapshot/provenance integration proves disproportionate, if Hindsight provides better task context with the same structural inputs, or if OpenViking's resource/context approach gives better outcomes. A combined Cognee/Hindsight deployment requires evidence that the second engine contributes more than its reconciliation and operational cost. Comparative benchmarking remains deferred.

## 7. Concrete roadmap edits

Prepare one coherent amendment set against the current checkout and planning source:

| Target | Exact intent |
|---|---|
| `docs/omp-stabilization-plan.md` | Replace the blanket post-promotion development deferral with an explicit allowance for isolated Fleet Knowledge work. Preserve live promotion, trusted evidence, recovery and schema qualification requirements. |
| `docs/bookends-open-work-review.md` | Preserve it as a dated review. Link a new dated amendment rather than rewriting historical statuses. |
| New architecture decision / implementation plan | Record the lead stack, finalist alternatives, selection conditions, authority split, native contracts, complete release definition, development dependencies and deferred benchmark scope. Follow current repository document conventions. |
| `PROGRAM_DEPENDENCIES.md` in the next planning-package revision | Add FK dependencies and factor early NSI/FLEET shared components. Do not imply that early component delivery satisfies full Fleet activation. |
| NSI technical contract and Fleet intake | Unify evidence/source/decision identities; expand FLEET-5 to knowledge lifecycle and snapshot-set context. |
| Model/agent operating policy | Add knowledge roles, local GPU candidates, explicit stronger-model routes, error/fallback behavior and evidence requirements without changing coding/reviewer routing by implication. |
| Actual native Work Ledger items | Amend structured scope, acceptance criteria and relationships using current revision-bound APIs; preserve existing history and delivered work. |
| Package preservation files, if republishing the ZIP | Make a new version with accurate manifest, ancestry/change log and checksums. Keep v9.1 unchanged as historical input. |

Do not call this a released v9.2 package unless that package is actually regenerated and verified. This document is a dated amendment that can be incorporated into the next package version.

## 8. Handoff to the local implementing agent

Continue the OMP fork from the current checked-out source and native Work Ledger. Use this amendment together with the Memory Candidate Decision Report; the older blueprint is supporting design material where consistent with this revision. The lead target is native OMP knowledge/evidence lifecycle plus Cognee, independent Enola analysis and qualified use of the RTX 5090. Preserve Hindsight/OpenViking as primary alternatives. Do not add a second memory engine without evidence of incremental value. Do not run memory benchmarks or model comparison campaigns in this tranche.

First read the effective repository and local instructions. Current root AGENTS.md requires the local Antidote skill before planning, implementation or review. The referenced skill was unavailable in this remote review and was not applied here; resolve it in the actual development environment before executing repository work.

Reconcile the September 8 stabilization guide and subsequent commits with current complete native records and qualification receipts. Do not assume old BACKLOG labels remain current, recreate delivered fixes, or turn this into another broad audit. Read only what is needed to establish the selected slice and its dependencies. Use exact read APIs and complete cursor/export facilities; do not infer completeness from an unmarked tree limit.

Prepare the exact roadmap and structured native-item amendments. Reuse existing authorization, native revision checks and approved scopes. If an actual publication or schema boundary remains, finish the reviewable candidate and unaffected authorized work first. Do not manufacture approval artifacts or treat this document as a deployment receipt.

Implement FK-1 and the independent FK-3 work first, with their interface agreed. Extend the existing WorkService, workflow host, work-client and stage context path. Use a small dedicated knowledge service package following repository conventions. Reuse recovery and operation identity mechanisms; add transactional event publication where required. Keep the controlling installation stable while modifying and testing the candidate checkout.

Build through FK-7 so the release includes later-task reuse and correction, not just ingestion and search. Keep all learning outputs attributed and policy-bound. A model's extraction confidence or a generic tool success cannot grant acceptance. Preserve independent review and accurate evidence.

Run focused behavioral verification as implementations become available and the applicable candidate gates before promotion. Record what was actually executed, skipped, simulated or unverified. No public benchmark suite, comparative memory campaign, paid model trial or live cutover is implied by this handoff.

Return:

- Current base and candidate identities, and the exact native IDs corresponding to FK labels.
- A concise disposition of reused, amended, delivered and genuinely new work.
- Reviewable code/roadmap changes and native amendment readbacks or exact prepared previews.
- Service and model artifact identities, including local GPU roles and declared alternatives; qualification evidence and unresolved feasibility limits.
- Evidence of the full learning loop, correction, snapshot isolation and recovery when implemented.
- The deployment/rollback result and one exact next action for any unfinished work.

## 9. What this plan deliberately does not claim

The lead architecture is an engineering recommendation from a seven-candidate source review, not a measured superiority result. Hindsight and OpenViking remain viable primary alternatives; the review did not establish a quality ranking across deployments. This review did not access the live ledger, user installation or GPU, run application tests or benchmarks, create issues, edit the fork or deploy services. The native blueprint record names and FK labels are proposed contracts/work packages. Final native identities and current completion status come from WorkService.

The immediate next action is to apply the roadmap amendment and prepare FK-1/FK-3 against the current native scope, while closing only the stabilization prerequisites that the present evidence shows remain unresolved.

