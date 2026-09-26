# OMP Fleet Knowledge: orchestrator implementation handoff

Prepared 11 September 2026. Model selection revised after review of Qwen3.8-27B. One self-contained handoff for the OMP fork.

**Current generator recommendation:** Qwen3.8-27B is the lead local generation candidate. Section 4 supersedes the older generator examples in the embedded historical reports. Preserve those reports unchanged and record this model decision in the current implementation record.

**Instruction to the receiving orchestrator:** implement the recommended Fleet Knowledge setup through its complete first release. Save the embedded decision report and roadmap in the repository, reconcile the existing native work, implement and verify the candidate, and complete the applicable release process within your existing authority. Continue through implementation; do not stop after restating a plan.

The active scope is implementation and required correctness qualification. A future comparative benchmark campaign is specified in section 8 and is **optional, disabled by default, and not a prerequisite for this release**.

## 1. Mission and selected direction

The goal is a fleet that can use previous engineering experience: recover the relevant evidence, understand the current code, apply an appropriate procedure, and correct its knowledge when subsequent evidence disagrees.

Implement this initial architecture:

| Responsibility | Selected direction |
|---|---|
| Execution authority, accepted evidence, decisions and procedure lifecycle | Native OMP WorkService and existing workflow contracts |
| Connected engineering knowledge and lesson proposals | Cognee, through a narrow native adapter |
| Structural code analysis | Enola, independently usable and bound to immutable source snapshots |
| Stage context assembly | Native OMP context compiler, shared with the planned Fleet context path |
| Embeddings and reranking | Dedicated retrieval models on the shared RTX 5090 service, subject to qualification |
| Routine extraction, lesson proposals and local synthesis | Qwen3.8-27B as the lead generation candidate, with a qualified quantization and serving runtime |
| Difficult synthesis, coding and independent review | Appropriate qualified model routes; hosted inference remains eligible |

Cognee is the implementation lead based on the source review. It has not been demonstrated to outperform all alternatives. Preserve a practical migration/comparison boundary for **Hindsight + Enola** and **OpenViking + Enola**. Implement the Cognee path first. Adding several production memory engines is not part of the default release.

Hindsight is neither mandatory nor restricted to a legacy role. Existing Hindsight data is a migration input; Hindsight could become the primary engine if later evidence supports it. MemOS is a procedural-learning reference or bounded component candidate. Do not add its whole host runtime merely to obtain learning terminology.

The user permits substantial OMP changes. Knowledge may be durable structured data; earlier guidance that it must live only in files does not constrain this design. Quality is the objective. Local-only inference and free-only database editions are not requirements.

**Complete release outcome:** capture → lesson/procedure proposal → evidence-bound acceptance → reuse by a fresh task → recorded outcome → correction or withdrawal. Searchable transcripts alone do not satisfy the task.

## 2. Save the reports and establish current work

### 2.1 Preserve this handoff and the embedded reports

This file includes the complete source assessment in Appendix A and the revised roadmap in Appendix B. No other attachment or access to the original conversation is needed to carry out the instructions.

Use the following proposed repository locations, adapting the directory to established documentation conventions if necessary:

| File to save | Contents |
|---|---|
| `docs/plans/fleet-knowledge/OMP_Fleet_Knowledge_Orchestrator_Handoff.md` | This complete handoff, including both appendices |
| `docs/plans/fleet-knowledge/OMP_Memory_Candidate_Decision_Report.md` | Exact content inside Appendix A's embedded-file markers, excluding the markers |
| `docs/plans/fleet-knowledge/OMP_Immediate_Roadmap_Amendment.md` | Exact content inside Appendix B's embedded-file markers, excluding the markers |

Preserve the embedded reports as dated source material. If a destination already contains a newer or different document, compare it and preserve both histories instead of blindly overwriting it. Record any adapted paths. Link the reports from the current architecture/implementation record and the native work umbrella.

The original reports contain `sandbox:` links to earlier conversation artifacts. Those links are historical references, not required inputs and not filesystem locations on your host. The full actionable direction is included here. Keep the original reports intact; place any repository-local navigation links in the current implementation record.

Verify the extracted UTF-8 bytes, including the final newline:

| Embedded report | Bytes | SHA-256 |
|---|---:|---|
| `OMP_Memory_Candidate_Decision_Report.md` | 43,276 | `50d6e266ba0219d8fe0c8560e28a5f4dd32501b866105abcb7a840e97bc9369c` |
| `OMP_Immediate_Roadmap_Amendment.md` | 26,194 | `bb4933d5eff9a623537a347364c551d11b0c928f9fcb65713a2651335866fae3` |

Treat the reports as supporting analysis, not as proof of current issue state, deployed code or model performance. The implementation instructions in sections 1–9 consolidate the requested direction; follow the user's current instructions and applicable repository requirements when applying them.

### 2.2 Reconcile only what is necessary to execute

1. Read the current checkout's instructions and applicable local skills. The reviewed root `AGENTS.md` referenced an Antidote skill; verify whether that requirement still applies and load it in the actual development environment. If a required dependency is unavailable, state the exact affected boundary and continue unaffected authorized work.
2. Establish the actual repository root, branch, base revision, candidate/worktree, installed OMP runtime and available service/GPU hosts. The source review used OMP `aacabebf7c9d894ff8dca4b9fff188303b2491f4`; this is a historical reference, not an instruction to downgrade the checkout.
3. Read complete current native work records, criteria, relationships, revisions and qualification evidence. Use exact IDs and complete cursor/export reads. A capped tree listing cannot establish that an item or dependency does not exist.
4. Map FK-0 through FK-7 to existing work wherever possible. These are planning labels, not allocated Work Ledger aliases. Preserve completed work and add only demonstrated missing scope.
5. Record the architecture decision, deployment topology, adapter boundary, selected implementation slice and release criteria. Apply authorized roadmap/native-item amendments through current revision-bound APIs and read back the results.

Reuse NSI source/evidence/decision contracts, FLEET-5 context compilation, relevant CPK event/projection seams, recovery operations and the existing Harbor evaluation direction. The issue aliases in Appendix B are dated retrieval leads, not current statuses. Do not require completion of all CPK, NSI or Fleet scheduling work before the shared knowledge components can operate in the current workflow.

The requested roadmap change allows isolated Fleet Knowledge development alongside remaining stabilization. Preserve real runtime, schema, trusted-evidence and promotion requirements. Respect current execution grants and worktree ownership. Where independent workers are available and authorized, give them bounded work with explicit interfaces and separate ownership; do not let concurrency create multiple writers to the same authority.

## 3. Architecture and contracts to implement

Extend the existing native system. Cognee owns derived processing and retrieval; native OMP records determine what is accepted, applicable and current. Source artifacts and exact code snapshots must remain independently resolvable. There must be a practical rebuild path when derived graph/vector data is lost or changed.

Use existing types when they already express these concepts. The following are required semantics, not claims that these type names or APIs already exist:

| Contract | Required information and behavior |
|---|---|
| Source/evidence reference | Stable repository identity; work/run/stage/attempt identity; source revision and content hash; artifact locator; producer; observation time; scope and acceptance provenance |
| Code snapshot | Repository identity plus exact content identity, including relevant uncommitted content; base commit; extraction tool/version; coverage; complete-publication status |
| Claim/decision | Claim text or structured value; evidence references; applicability; current lifecycle state; supersession/counterevidence; revision-bound changes |
| Procedure version | Preconditions, steps, expected observations, limits and failure conditions; supporting evidence; applicability; acceptance policy/result; supersession/withdrawal |
| Knowledge-use record | Which version was supplied, whether the worker used it, under which conditions, and what independently recorded outcome followed |
| Context bundle | Work/candidate/stage identities; source/snapshot set; selected knowledge versions; compiler/policy/model identities; token budget; delivered content and provenance |
| Ingestion/publication job | Stable operation identity; payload hash; checkpoints; attempts; processing/error state; cancellation; completed publication identity |

Separate a record's revision and validity from when it was observed. Exact historical context must remain inspectable after a later correction. Current retrieval must not mistake that retained history for current accepted knowledge.

Define a small adapter contract for ingestion, scoped retrieval, exact lookup, correction and processing status. Every result needs source/version provenance and explicit processing errors. Keep engine-specific reflection or graph traversal available through optional capabilities; do not pretend all engines have identical semantics.

### 3.1 Capture and replay

Capture committed native changes, tool execution results, stage outcomes, review findings, corrections and failure artifacts. Conversational hooks alone are insufficient. Use a transactional outbox for native committed events; record external observations through durable receipts with clear attribution.

Use at-least-once delivery with idempotent application, stable operation identities and payload-hash conflict detection. Retry must not duplicate an admitted record or silently apply two different payloads under one identity. An event-processing queue is not another workflow scheduler.

Retain original evidence and the derivation chain. Distinguish no useful lesson, failed extraction, cancelled processing and incomplete publication. A Cognee failure that produces an empty result must not be reported as successful learning with nothing to save.

### 3.2 Candidate-specific code views

Run Enola against exact candidate contents and persist a coverage/receipt manifest. Its output must remain usable without Cognee. Incorporate those facts into the selected graph through an explicit snapshot namespace.

The reviewed Cognee importer updates a repository view and removes stale facts. Snapshot metadata alone will not isolate two candidates if they still share mutable node identities or unrestricted queries. Implement separate identities or a genuinely versioned storage/query design, then prove candidate A remains stable while B is ingested.

Stage new graph material and publish a completed manifest atomically from OMP's perspective. Retain snapshots referenced by active work and auditable bundles under the existing retention policy. Failed or partial builds must remain unpublished. Record unsupported constructs; an extracted graph is not proof that all dynamic dependencies or affected tests were found.

### 3.3 Context assembly

Integrate the native compiler with current workflow stages and the planned Fleet path. Resolve mandatory current state and exact evidence first; add structural facts and relevant historical/procedural knowledge within the declared budget. Preserve source links and the distinction between accepted facts, proposals and disputed history.

Apply scope and validity checks at delivery, not just when writing the index. Revalidate material that changed between compilation and dispatch. Persist what was actually delivered and its validity at that time so later corrections do not rewrite historical audit evidence.

Supply independent reviewers with inspectable evidence. A previous agent's assertion of correctness is a claim to examine, not an instruction to accept. Retrieved memories cannot change execution grants, authorization or tool policy.

### 3.4 Learning and correction

Generate procedure proposals from attributed traces, with explicit applicability and expected results. Native policy decides acceptance based on required evidence. Model confidence, textual overlap, a zero exit code or an agent saying “done” cannot substitute for that evidence.

Represent a first supported case without inventing broad generality. Record later independent use and its outcome separately from retrieval. Counterexamples must narrow applicability, trigger reconsideration or withdraw the procedure as appropriate.

When evidence changes, invalidate the affected acceptance and dependent material immediately through native validity controls. Schedule graph/vector/cache cleanup and recomputation with visible status. Already-running work must be able to detect invalidated required knowledge at relevant execution/review boundaries; new bundles cannot silently revive withdrawn claims from stale indexes.

## 4. Service and RTX 5090 implementation

Inspect the actual host before choosing its deployment profile: GPU/driver/runtime compatibility, available memory, CPU/RAM, storage, network access and existing model services. Pin working engine, parser, database, model and serving-runtime artifacts. The reviewed commits in Appendix A are evidence references; choose a qualified version deliberately and document material differences rather than floating on `latest`.

Keep native WorkService schemas/migrations separate from engine-owned metadata and derived indexes. Use a supported graph deployment that satisfies required isolation. Neo4j Community, Enterprise or another supported backend may be appropriate; the choice must account for actual dataset-isolation behavior. Do not assume arbitrary multi-database capability or use a graph container per task.

| Inference role | Starting direction | Required qualification |
|---|---|---|
| Embeddings | Dedicated local embedding model; Qwen3-Embedding-0.6B is an efficiency baseline, with 4B/8B eligible for later quality comparison | Correct query/document handling, model identity, revision, prefixes, pooling, normalization and dimensions |
| Reranking | Dedicated local reranker; Qwen3-Reranker-0.6B is an efficiency baseline, with 4B/8B eligible for later quality comparison | Correct pair-scoring contract, appropriate ordering and acceptable interactive latency |
| Routine extraction/lesson proposals | Qwen3.8-27B, quantized, as the lead local generator | Source fidelity, structured outputs, cancellation and realistic context limits |
| Difficult synthesis/reflection | Qwen3.8-27B with appropriate reasoning, escalating to a stronger qualified route where needed | Evidence quality and explicit routing/fallback; no local-only restriction |
| Coding and independent review | Existing qualified OMP routes initially | Separate qualification before changing these roles |

### 4.1 Revised generator choice and its rationale

Use **Qwen3.8-27B** as the lead local generator. Its official model card documents improved instruction following and agent behavior, with request-level thinking controls. Those capabilities make it a relevant current candidate for extracting evidence, proposing procedures and synthesizing engineering history; their benefit to OMP remains to be qualified. [Official model card](https://huggingface.co/Qwen/Qwen3.8-27B).

The previous Qwen3.5-35B-A3B choice was an efficiency-oriented provisional candidate: it has 35B total parameters and 3B activated per token. That architecture provides a reason to investigate processing efficiency, but active parameter count does not establish memory footprint, measured throughput or best extraction quality. Retaining it without checking newer candidates was an omission. Keep it only as an optional comparison, not the preferred generator. [Earlier model architecture](https://huggingface.co/Qwen/Qwen3.5-35B-A3B).

**First runtime qualification path:** the vLLM project publishes a single-RTX-5090 recipe using `Inferact/Qwen3.8-27B-NVFP4`, a 32,768-token context limit, `--enforce-eager` and the `qwen3` reasoning parser. Its documented CUDA-graph memory issue is specific to that serving configuration. Use the recipe as a reference and pin/verify the actual checkpoint, kernels and runtime on this host. [vLLM single-card recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B).

**Alternative runtime path:** a compatible llama.cpp server with a GGUF quantization. Unsloth lists `UD-Q4_K_M` at about 16.5 GB, `UD-Q5_K_M` at 19.8 GB and `UD-Q6_K` at 22 GB. A 5-bit build is a sensible starting candidate for this shared GPU. These are published artifact sizes, not total running VRAM or measured quality rankings. NVFP4 and GGUF quantizations are distinct deployments. [Quantized artifacts](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF).

Begin service qualification with one generation request at a time, bounded extraction chunks and an approximately 32K context budget. Measure real weight, context-cache, runtime-buffer and retrieval-service allocations together before increasing concurrency or context. Prefer language-only serving for the initial text-memory workload where supported. Do not silently offload to CPU and call the result a fully GPU-resident deployment. These are proposed starting settings, not performance claims.

Configure reasoning by operation. Qwen3.8 defaults to thinking with `xhigh` effort and preserved thinking; do not inherit those settings accidentally for every background extraction. Qualify non-thinking or lower effort for simple structured extraction, and appropriate reasoning for difficult synthesis. Verify the actual runtime's parameter mapping and parse the final structured result correctly. A schema-valid answer still requires source/evidence checks. [Thinking controls](https://huggingface.co/Qwen/Qwen3.8-27B#api-usage).

Embedding and reranking models have separate training objectives from the generator. The Qwen3 specialist families provide 0.6B, 4B and 8B options; changing the generation model to Qwen3.8 does not automatically replace them. The 0.6B choices remain deployment-efficiency baselines, not proven quality optima. Larger specialists remain eligible, with actual co-residency, scheduled execution or another qualified route decided from the service needs. [Embedding family](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), [reranking family](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B).

Run focused installation, memory-residency, structured-output and evidence-fidelity qualification during implementation. Broad model and memory-engine ranking stays in the optional benchmark program. No GPU run or model benchmark was performed when making this correction.

Prioritize active context requests over background extraction/backfill. Bound queue size, batch size, concurrent generation, timeouts and cancellation. Record the route actually used, including fallback. A GPU outage must produce explicit service state and a declared fallback or deferred enrichment, without changing native workflow authority.

Pin embedding identity beyond vector dimensions. A same-dimension model change still requires a compatible migration or rebuild. Do not silently mix vector generations or use an incompatible CPU fallback. Record model and preprocessing identities in index manifests and retrieval traces.

Use the deployment's existing authentication, scope and network conventions. Integrate health, restart, backup and recovery into the actual service environment. Create runbook commands that match the implementation; illustrative names in this handoff are not existing OMP CLI commands or configuration keys.

## 5. Delivery sequence and ownership

Use the detailed roadmap in Appendix B, with this operational sequence:

| Package | Implement | Evidence required before marking complete |
|---|---|---|
| **FK-0** | Save reports, reconcile current work, amend scope and record the architecture/deployment decision | Exact native IDs/revisions and a current ownership/dependency map; source delivery distinguished from live deployment |
| **FK-1** | Shared source/evidence/procedure/context contracts and complete read/artifact resolution | Exact revision resolution, stable repository identities and invalidation of changed acceptance |
| **FK-2** | Capture, durable outbox, artifact retention, retry and status | No lost committed event or duplicate admitted record under restart/replay; conflicting payloads rejected |
| **FK-3** | Cognee adapter and qualified data/model services | Real source-to-retrieval flow, explicit errors and routes, scope/provenance preservation and declared outage behavior |
| **FK-4** | Independent Enola extraction and versioned publication | Two candidate snapshots coexist; no partial publication; extraction coverage is visible |
| **FK-5** | Native context compiler and workflow hooks | Fresh worker receives correct current state, evidence and selected knowledge within budget; exact bundle retained |
| **FK-6** | Procedure proposals, acceptance, later-use outcomes, counterexamples and retraction | Full learning lifecycle operates and stale derived results cannot bypass native validity |
| **FK-7** | Integrated acceptance, minimum inspection UI/CLI, import, recovery and release runbook | Complete release journeys pass on the candidate; applicable installed-runtime checks and deployment evidence are recorded |

After FK-0, agree the FK-1/FK-3 interface and build the native contracts and service path. Enola extraction can proceed independently once its source/snapshot contract is fixed; Cognee publication depends on the relevant storage adapter. Learning implementation can proceed once capture and evidence contracts exist. Integrate through FK-5 and FK-7.

Deliver coherent reviewed changes with focused verification. Do not wait for fleet-wide scheduling, a general plugin rewrite or a large UI redesign. The minimum inspection surface must expose source evidence, acceptance/applicability, selected context, supersession and processing failures through the existing supported interfaces.

If a concrete limitation makes Cognee disproportionately costly to adapt, document the failing requirement and compare a bounded alternative against it. Change the architecture through the current decision process. Do not repeatedly reopen the whole market survey before delivering the first integrated journey.

## 6. Required implementation verification

These checks belong to implementation and should run as the relevant components become available. They are distinct from optional cross-engine/model benchmarking. Reuse existing test infrastructure and actual workflow/evidence contracts.

| Journey or failure case | Required observable result |
|---|---|
| Exact capture | A real task's commands, artifacts, source revision, review and outcome are attributable and independently resolvable |
| Complete historical import | Exact/cursor reads enumerate the intended source range, with checkpoints and explicit gaps |
| Replay and interruption | Replayed events are idempotent; conflicting identities fail; partial attempts and errors remain visible |
| Candidate isolation | Ingesting B does not change A's structural results, evidence or accepted context |
| Scope and worktrees | Repository identity survives checkout paths; cross-repository knowledge is admitted only under justified scope |
| Context budget | Required current state remains present; selected material has provenance; insufficient required evidence is explicit |
| Fresh-task reuse | A later worker receives and uses an accepted procedure under its preconditions, with separate outcome evidence |
| False success signal | Tool success or agent narration alone cannot promote an unsupported procedure |
| Counterexample/retraction | New evidence narrows or withdraws the lesson; stale graph/cache material cannot regain accepted status |
| Independent review | Auditor can inspect the evidence without inheriting the implementer's success assertion as authority |
| GPU/engine failure | Cancellation, outage and backlog behavior follow declared policy; native workflow state remains accurate |
| Rebuild and rollback | Derived stores can be restored/rebuilt from retained sources; the selected installation can recover using a rehearsed procedure |

Use OMP for the first complete journey and then media-discovery to establish behavior across two repositories. Inspect media-discovery's actual topology and available fixtures first. Record anything that could not be exercised; do not claim a two-repository release criterion passed from OMP-only tests.

A demonstration that only retrieves a historical procedure is insufficient for learning acceptance. The later task must be independent, and its evidence must show what was supplied, what was used and what happened. Do not infer causality merely because a recalled lesson appeared before a successful result.

Keep verification focused on these material risks and applicable repository gates. Avoid expanding an implementation slice into an unrelated benchmark campaign or accumulating tests that merely mirror the code.

## 7. Migration, activation and operational completion

Backfill existing native work, artifacts and applicable Hindsight history through resumable source-aware imports. Preserve source dates, versions and original evidence. Imported summaries begin as historical claims with their actual support; importing them cannot grant verified-procedure status.

Route context through one deliberate assembly path. Account for existing memory hooks so repeated capture or injection does not duplicate knowledge or overwhelm prompts. Retain a configuration-controlled rollback path and keep existing data intact until migration completeness and recovery are established.

Separate candidate verification from live installation verification. Apply current release procedures and any existing authorization; do not fabricate a grant, acceptance receipt or approval to complete the rollout. If a genuine boundary requires user input, finish all unaffected work and present the exact candidate, verification and action awaiting approval. Explain the rule that requires it instead of asking for routine reconfirmation.

Completion includes an operator runbook with actual startup/shutdown, configuration, model/index migration, status inspection, failed-job replay, backup/restore and rollback commands. Record component versions, source snapshots, index/model generations and current provider routes. Recovery behavior must be demonstrated where the acceptance criteria require it, not merely described.

## 8. Optional future benchmark program

**Default state: deferred.** Preserve the design and lightweight trace/export seams now. Do not run public suites, bulk model comparisons or a cross-engine campaign as part of this handoff's active implementation scope. If the user later enables this program, execute it without reopening already settled implementation permissions or interpreting the deferral as permanent.

### 8.1 Decision to answer

Find which complete memory architecture most improves OMP's correct task completion, useful reuse and independent review. Measure the incremental benefit of each added component. Keep a separate controlled comparison to explain whether improvements come from the engine, models, code context or a larger context allowance.

Reuse the existing Harbor/evaluation direction and current OMP task runner. Add memory adapters, frozen fixture manifests and trace export where needed. A separate scheduler or general-purpose benchmark platform is not required.

### 8.2 Comparison arms

All architecture arms receive equivalent current native state, evidence access and structural source snapshots. A deliberate Enola-off ablation is a separate experiment.

| Arm | Purpose |
|---|---|
| Native current-state/artifact access + competent keyword/dense retrieval + Enola | Strong baseline establishing whether a full memory engine helps |
| Native OMP + Cognee + Enola | Evaluate the implemented architecture |
| Native OMP + Hindsight + Enola | Compare historical recall/consolidation using the same structural inputs |
| Native OMP + OpenViking + Enola | Compare resource organization and progressive context |
| Cognee + Hindsight, only after the individual arms | Test whether two engines add measurable value beyond either alone |

Add Graphiti or managed Zep, Mem0, Supermemory, Local Memory, MemMachine or a MemOS proposal component when a stated hypothesis warrants it. Do not assign managed-product scores to an OSS edition. Evaluate IAI in its personal-memory role unless a concrete shared-fleet extension is itself the experiment. The full candidate dispositions are preserved in Appendix A.

### 8.3 Suites and fixtures

Use original [LongMemEval](https://github.com/xiaowu0162/LongMemEval) for longitudinal conversational-memory questions. Use [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) for agent-history questions that include workflow knowledge, changing state, environment gotchas and premise awareness. Their retrieved-context/reader results do not establish that OMP can execute a correct code change.

Add OMP task fixtures for historical rationale, superseded decisions, conflicting evidence, parallel candidates, worktree identity, cross-repository applicability, successful later reuse, misleading success signals, withdrawal, architecture changes, interrupted publication and missing evidence. Appendix A supplies the scenario definitions.

For every fixture, freeze the history cutoff, source/candidate identities, expected evidence, scope, prohibited stale material and task/answer rubric. Keep future outcomes, gold answers and evaluator metadata out of memory construction. Keep tuning cases separate from held-out evaluation. Use deterministic checks where appropriate and independently qualified review for outcomes that require judgment.

### 8.4 Two complementary comparison modes

**Controlled mode:** hold the downstream worker/reader/reviewer, history, usable context budget, structural inputs and stopping rules constant. Where engines support equivalent extraction/embedding/reranking configurations, compare them and record unavoidable differences. Report incompatibilities instead of claiming a clean engine-only comparison when model pipelines differ.

**Best achievable mode:** allow each architecture its suitable model stack, including stronger hosted models and managed services. Retain the same task evidence and success rubric. Report its full quality, latency and operational burden. This addresses the user's quality-first objective without imposing a local-only cap.

Run paired tasks with reproducible seeds/settings where possible and repetitions appropriate to variance. Start with a pilot to estimate variability and required sample size, then report uncertainty. Do not manufacture a statistically meaningful winner from a small convenient sample.

### 8.5 Required measurements and adoption rule

| Measure | What to record |
|---|---|
| Correctness gates | Scope leakage, wrong-candidate evidence, stale accepted knowledge, missing provenance and recovery failures; inspect individually |
| Task quality | Accepted task completion, regressions, reviewer findings, repeated mistakes and useful later-task reuse |
| Retrieval/answer quality | Relevant evidence retrieved, cited-answer accuracy, contradiction handling and abstention; keep these distinct from task success |
| Context efficiency | Delivered tokens, redundant material, required-state omissions and additional lookup calls |
| Latency | Ingestion, consolidation and query latency, including tail latency and queue delay |
| Total resource use | Extraction, reflection, embedding, reranking and reader costs; GPU utilization/memory; storage and operational effort |
| Learning contribution | Outcome difference with accepted procedural knowledge enabled versus an otherwise equivalent architecture |

Set promotion thresholds and any acceptable quality/latency tradeoff before the final run. A material scope or validity failure cannot be averaged away by higher recall. Prefer better correct task outcomes; when quality is comparable, consider latency and operational complexity. A second engine must demonstrate incremental benefit through ablation.

A higher retrieval hit rate is not automatically higher answer accuracy, and neither automatically means better engineering. Do not combine vendor headline percentages into a score. Publish exact versions, configuration, dataset variants, traces and limitations so results can be reproduced.

Save the future benchmark protocol, run manifests and results under the repository's evaluation conventions, link the native work record, and issue an architecture decision only when the evidence supports changing the selected setup. No benchmark result is being supplied or claimed by this handoff.

## 9. What to return to the user

Provide a concise implementation result with:

1. Saved report/handoff paths and the current architecture decision.
2. Actual native work IDs, reused/amended scope, candidate identities and reviewable commits/PRs.
3. Implemented services and model routes, exact versions, and qualified 5090 roles.
4. Evidence for the complete learning cycle, candidate isolation, correction and recovery; clearly identified unexecuted or failed checks.
5. Installation/activation status, rollback location and the precise remaining blocker if anything is incomplete.
6. Confirmation that the optional comparative benchmark program remains deferred, or its separately enabled results if the user's scope later changes.

Continue until the requested implementation is complete within the effective authorization. If genuinely blocked, preserve completed work and report the smallest concrete action needed to resume. Do not turn this handoff into another open-ended tool-selection discussion.

## Appendix A — Embedded memory candidate decision report

The following block is the exact dated report to extract and save as directed in section 2. Its role is architecture evidence and comparison rationale.

<!-- BEGIN_EMBEDDED_FILE: OMP_Memory_Candidate_Decision_Report.md -->
# OMP memory candidates: decision report

Prepared 11 September 2026. Source review only; no benchmarks, model trials, installations or repository changes were performed for this assessment.

## 1. Decision

**Build toward native OMP evidence and learning controls, Cognee for connected engineering knowledge, and independently usable Enola code analysis. Use the RTX 5090 for suitable inference jobs. Keep Hindsight + Enola and OpenViking + Enola as the two primary alternative architectures.**

This is a lead implementation choice based on architectural fit, not a demonstrated quality winner. The useful distinction is between choosing an architecture we can implement now and claiming it produces the best engineering outcomes. Source inspection supports the former; controlled use is needed for the latter.

The reason to lead with Cognee is its combination of typed knowledge graphs, a concrete Enola ingestion path, and session-to-lesson processing. These provide reusable components for connecting code, documents, decisions and experience. That breadth fits the proposed OMP fleet. The decision does **not** depend on Hindsight being expensive, on keeping knowledge in files, on using only free database editions, or on forcing every model call onto one GPU.

Hindsight remains a serious candidate for the primary memory role. Its historical recall, observations, reflection and correction APIs could produce a better complete OMP system once paired with the same code analysis and native controls. OpenViking is the alternative when organizing a large resource/skill corpus and progressively assembling context proves more valuable than a unified engineering graph.

Do not install all three as coequal memories. A combined Cognee + Hindsight design is a conditional extension: it must earn its additional ingestion, reconciliation and retrieval complexity by adding measurable value beyond the stronger single-engine architecture.

This report supersedes earlier vendor commitments in the Fleet Knowledge Blueprint and the description of Hindsight as merely optional legacy input. The accompanying revised roadmap preserves the complete first release while making the vendor and inference choices appropriately conditional.

## 2. What OMP actually needs from memory

OMP already owns workflow execution, work records, grants, evidence and supervision. Its reviewed source also has incomplete memory capture: the Hindsight conversational path does not, by itself, capture the full tool and workflow evidence needed for fleet learning. Complete native reads and receipt resolution matter because a bounded tree listing is insufficient for reliable historical ingestion. These findings are documented in the [OMP context architecture review](sandbox:/workspace/scratch/c5f3674f4c15/OMP_Fleet_Context_Architecture_2026-09-11.md) and reconciled against the [current fork snapshot](https://github.com/theturtlecsz/oh-my-pi/tree/aacabebf7c9d894ff8dca4b9fff188303b2491f4).

The desired improvement is that a fresh worker can use previous engineering experience without inheriting stale facts, another candidate's state, or an earlier agent's unsupported conclusion.

| Capability | Concrete benefit | What would establish that benefit later |
|---|---|---|
| Historical recall | Recover why a decision was made, what failed, and which evidence existed at the time. | Correct cited answers across revisions, including abstention when evidence is absent. |
| Structural code context | Find relevant dependencies, boundaries and change impacts across components. | Correct source-linked relationships for the worker's exact candidate. |
| Context selection | Give the next worker useful information within its attention and token budget. | Better task completion without flooding the worker with irrelevant history. |
| Procedural learning | Reuse a successful approach with explicit preconditions and limits. | A later independent task benefits, with attributable supporting evidence. |
| Correction | Stop a disproven lesson or obsolete fact from being treated as current. | Retraction propagates to subsequent context despite stale derived indexes. |
| Fleet operation | Share applicable knowledge across worktrees, runs and agents. | Scope isolation, retry safety, recoverability and independently inspectable evidence. |

These are related but different capabilities. Retrieving a past success is not proof that its procedure generalizes. A graph edge is not necessarily a parsed code relationship. A tool called `validate` may check database consistency rather than software correctness.

The OMP architecture already has enough interacting state, execution and evidence boundaries to justify evaluating structural code context. For media-discovery, this review does not establish its current size, languages or dependency topology. A connected code graph becomes useful when tasks repeatedly cross those boundaries; repository size alone does not establish a benefit.

## 3. Consistent candidate assessment

The seven candidates received targeted review of public implementations and documentation. “Implemented” below means a relevant mechanism is present in the inspected source, not that it has passed OMP acceptance. Local Memory was assessed from official release/API documentation because its engine implementation was not available for inspection. None has been tested on the user's installation.

| Candidate | Distinctive existing mechanism | Code/engineering knowledge | Learning and correction | Decision for OMP |
|---|---|---|---|---|
| **Cognee** | Typed graph retrieval across sources; session distillation | Implemented Enola import and custom graph models | Lesson generation exists; accepted validity and complete retraction need OMP controls | **Lead implementation architecture** |
| **Hindsight** | Historical facts, consolidated observations, reflection and mental models | Store engineering history; add independent structural code service | Implemented fact curation and derived refresh paths; procedure acceptance remains OMP work | **Primary alternative and required quality comparator** |
| **OpenViking** | Hierarchical memories/resources/skills and bounded context assembly | Resource ingestion and code skeletons; add structural graph analysis | Experience/skill templates and lineage; OMP outcome acceptance and candidate publication required | **Second primary alternative** |
| **Graphiti / Zep** | Graphiti: temporal relationship graph with episode provenance | Custom engineering schema and retrieval orchestration required | Temporal invalidation exists; full evidence retraction needs additional semantics | **Foundation alternative; assess managed Zep separately** |
| **MemOS local core** | Traces, policies, feedback and skill promotion | Engineering knowledge comes from captured task experience | Concrete promotion machinery; inspected verifier is heuristic, not an OMP execution verifier | **Procedural-learning specialist** |
| **Local Memory** | Documented observation → learning → pattern → schema lifecycle | General knowledge APIs; structural code layer would be added | Explicit feedback, supersession and integrity tools; internals unverified | **Retain for a bounded product evaluation** |
| **IAI personal memory** | Literal personal history, contradiction-aware recall and local embedding identity | Coding conversations can be retained; no equivalent structural code service established | Preserves changing personal facts; “procedural” tier describes behavior parameters | **Personal-memory option, outside the shared fleet core shortlist** |

### 3.1 Cognee

**What it provides.** Cognee can connect custom typed entities rather than treating every memory as a similar-looking text chunk. Its Enola integration maps parsed code facts into graph nodes and relationships. Its session distillation implementation curates session batches, considers previous lessons, and adds generated lessons back into its ingestion pipeline. This is a useful foundation for linking a failure, the changed component, a decision, and a reusable procedure. [Code graph implementation](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/tasks/code_graph/extract_code_graph.py), [session distillation](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/modules/session_distillation/distill.py).

**Where the implementation stops.** The inspected code importer records a repository snapshot identity and sweeps stale graph facts. That is not automatically an immutable graph for every simultaneously active OMP candidate. OMP needs repository/snapshot namespaces, a completed-publication manifest and retained active views. A partially updated graph must not become a candidate's authoritative structural context. [Importer and snapshot maintenance](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/tasks/code_graph/extract_code_graph.py).

Distillation also has logged failure paths that return empty results or no lesson. OMP must distinguish “nothing useful learned” from “processing failed.” Context-only retrieval is useful for the native compiler, but it does not automatically produce the same agent trace as generated-answer retrieval. The documented provenance cleanup is best effort; source validity cannot depend solely on cleanup of every derived cache or trace. [Distillation source](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/modules/session_distillation/distill.py), [recall](https://docs.cognee.ai/core-concepts/main-operations/recall), [sessions and provenance](https://docs.cognee.ai/core-concepts/sessions-and-caching).

**Operational consequence.** Backend choice needs deliberate qualification. The inspected Neo4j dataset handlers differ: the multi-database handler requires the corresponding database capability, while the Community handler uses separate containers for dataset isolation. Do not assume a cheap single server provides arbitrary independent databases, or create a container per task. Budget is available; choose a backend and isolation design that fit actual trust boundaries. [Dataset handler](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/infrastructure/databases/graph/neo4j_driver/Neo4jDatasetDatabaseHandler.py), [Community handler](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee/infrastructure/databases/graph/neo4j_driver/Neo4jCommunityDatasetDatabaseHandler.py).

**Assessment.** Best source-fit lead for one connected engineering knowledge service. It still needs substantial, clearly bounded OMP work around evidence, snapshots and learning acceptance. Its broad feature set does not establish the best recall quality.

### 3.2 Hindsight

**What it provides.** Hindsight combines several recall methods with consolidated observations, reflection and longer-lived mental models. Its direct benefit is recovering and synthesizing relevant experience across a long history. It can serve as the primary historical knowledge engine while Enola supplies structural code facts. Local model providers are supported; self-hosting Hindsight does not inherently require hosted inference. [Retrieval](https://hindsight.vectorize.io/developer/retrieval), [observations](https://hindsight.vectorize.io/developer/observations), [reflection](https://hindsight.vectorize.io/developer/reflect), [model providers](https://hindsight.vectorize.io/developer/models).

**Correction is more substantial than simple deletion.** The inspected engine supports editing, invalidating and restoring world/experience facts. Invalidation moves a fact out of the live memory table; editing rebuilds relevant derived material. However, causal links are intentionally preserved across edits/invalidation-restoration, and mental-model refresh scheduling is best effort and depends on refresh settings. OMP still needs to invalidate accepted conclusions when their supporting evidence changes. It cannot assume every derived surface becomes synchronously correct. [Curation implementation, `update_memory_unit` and refresh helpers](https://github.com/vectorize-io/hindsight/blob/4cc131c0b238c8f206def60804d7b6591f6a45e7/hindsight-api-slim/hindsight_api/engine/memory_engine.py).

**Integration details matter.** Document identities can control replacement or append behavior. Caller-supplied operation identities support the documented asynchronous retain path; they are not a universal synchronous idempotency guarantee. OMP should retain its own event/payload identity and use source-version-aware document identities. Otherwise an update can replace evidence required for an older candidate. [Retain API](https://github.com/vectorize-io/hindsight/blob/4cc131c0b238c8f206def60804d7b6591f6a45e7/hindsight-docs/docs/developer/api/retain.mdx).

**Assessment.** Hindsight could be the best primary engine if historically grounded answers and useful synthesis dominate OMP's gains. Its existing adapter is convenient, but does not decide the outcome. It is optional in the eventual stack because other architectures cover overlapping roles; it is a required comparator in the selection process because its relevant capabilities are substantial.

### 3.3 OpenViking

**What it provides.** OpenViking organizes resources, memories and skills in a virtual filesystem, with abstract/overview/full-detail levels. Current APIs offer bounded context assembly and richer memory types, including cases, trajectories, experiences, tools and skills. This is more directly aligned with organizing agent working material than a basic preference store. [Project overview](https://github.com/volcengine/OpenViking/tree/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee), [memory API](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/docs/en/api/16-memory.md), [retrieval API](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/docs/en/api/06-retrieval.md).

The documented memory-recall endpoint is now a deprecated preset over the general context-search API. A new OMP integration should target the current search contract. Resource parsing and code skeletons can help navigation, but should not be credited as equivalent to an independently validated cross-file dependency graph.

**It has real sharing and lineage mechanisms.** The inspected memory isolation handler checks allowed memory types and user/peer scope. Experience lineage records source references and observed completed reads. These mechanisms are useful, but a completed read does not prove a worker used a procedure correctly; user/peer scope is not automatically an OMP candidate namespace. [Isolation handler](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/openviking/session/memory/memory_isolation_handler.py), [experience lineage](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/openviking/session/memory/experience_lineage.py).

**Operational caveat.** Its path locks and persistent session-processing queue should not be interpreted as a universal database transaction facility: the transaction document explicitly limits undo/commit semantics. The multi-write storage document separately identifies a limitation around concurrent processes writing to the same primary metadata. This is a specific subsystem limitation, not evidence that every OpenViking deployment is unsuitable for multiple agents. Qualify the intended service topology. [Path locks and recovery](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/docs/en/concepts/09-transaction.md), [multi-write storage](https://github.com/volcengine/OpenViking/blob/0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee/docs/en/concepts/14-multi-write-storage.md).

**Assessment.** Strong alternative when progressive context, reusable resources and skill organization are the dominant need. Use native OMP context authority and snapshot publication rather than assuming its existing pi integration can take over OMP's context lifecycle unchanged.

### 3.4 Graphiti and managed Zep

**What Graphiti provides.** Graphiti exposes temporal relationships backed by episodes, custom entities and scoped graph retrieval. Its temporal fields help separate when a fact was true from when it was recorded or superseded. That is valuable for changing decisions, dependencies and organizational facts. [Project and Zep distinction](https://github.com/getzep/graphiti/tree/c035afb7990b6077331a81e98b04efcfd9bf8184), [edge representation](https://github.com/getzep/graphiti/blob/c035afb7990b6077331a81e98b04efcfd9bf8184/graphiti_core/edges.py), [search filters](https://github.com/getzep/graphiti/blob/c035afb7990b6077331a81e98b04efcfd9bf8184/graphiti_core/search/search_filters.py).

**Where a graph is insufficient.** OMP would build the engineering schema, context compiler connections and procedure lifecycle around it. Group filters support partitioned retrieval but do not replace application authorization. The inspected `remove_episode` implementation deletes edges whose first supporting episode is the removed episode, then eligible nodes and the episode. That operation alone does not establish full evidence-aware recomputation when an edge has multiple supports or earlier facts should become current again. These cases need explicit OMP qualification. [Episode removal](https://github.com/getzep/graphiti/blob/c035afb7990b6077331a81e98b04efcfd9bf8184/graphiti_core/graphiti.py).

**Zep is a separate candidate edition.** Managed Zep adds its own service and context infrastructure. A managed Zep benchmark result cannot be attributed to a local Graphiti installation. Unlimited budget makes the managed product eligible; it does not make those implementations interchangeable. [Maintainer comparison](https://github.com/getzep/graphiti#graphiti-vs-zep).

**Assessment.** Choose Graphiti as the foundation if explicit temporal graph semantics and control over the surrounding application outweigh the benefit of Cognee's broader processing components. Managed Zep belongs in a later service comparison if its quality or operations justify moving that layer off-host.

### 3.5 MemOS local core

**What it provides.** The reviewed local TypeScript core has traces, learned policies, higher-level memory, feedback, reward propagation and skill generation. Eligibility includes evidence support and positive anchors; it is not merely a prompt that says “learn from this.” Its contracts also include namespaces and subagent outcomes. This review concerns `apps/memos-local-plugin`, not an assumption that every MemOS server edition has identical behavior. [Local core overview](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/README.md), [promotion eligibility](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/core/skill/eligibility.ts), [agent contracts](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/agent-contract/dto.ts).

**The important limit is what its verifier establishes.** The inspected skill verifier checks declared tool-name coverage and textual overlap with evidence. The implementation rejects tool coverage below 0.5; it does not require every declared tool despite a stronger introductory comment. Its default evidence-resonance threshold is also 0.5. These are useful screening heuristics, but they do not execute a procedure on OMP's candidate or establish that its claimed result is correct. [Verifier implementation](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/core/skill/verifier.ts).

Feedback propagation changes stored values and retrieval priorities; this should not be described as training the coding model's weights. The local storage connection uses SQLite WAL and a busy timeout. A centralized service can support multiple clients, but fleet deployment, ownership and OMP host adaptation remain engineering work. [Reward propagation](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/core/reward/backprop.ts), [storage connection](https://github.com/MemTensor/MemOS/blob/de8069428a9247bfa7a3d35f59a9b39fa8f231d2/apps/memos-local-plugin/core/storage/connection.ts).

**Assessment.** The strongest procedural-learning specialist among these inspected candidates. Use it as an implementation reference or bounded proposal-generation component. Its feedback machinery does not eliminate the native OMP evidence-acceptance layer, so adopting the entire host plugin is not automatically better than using selected components.

### 3.6 Local Memory

**What it provides.** Official documentation describes a progression from observations to learnings, patterns and schemas, alongside reflection, feedback, supersession and causal tools. Those APIs are relevant to engineering lessons. Existing ownership makes a trial convenient, but sunk purchase cost should not determine the fleet architecture. [Official release distribution](https://github.com/danieleugenewilliams/local-memory-releases/tree/20cdd249321e0406358aa3c85ea5be4b2ff3ad5c), [MCP tool contracts](https://github.com/danieleugenewilliams/local-memory-releases/blob/20cdd249321e0406358aa3c85ea5be4b2ff3ad5c/docs/mcp-tools.md).

**Read tool names carefully.** `evolve` validation accepts caller-provided success feedback. The separate `validate` capability checks graph integrity, including references, relationships and promotion consistency. `resolve` can express supersession, conditional differences or invalidation. These are useful lifecycle operations; none establishes that a software change passed OMP's trusted execution and review requirements. [Tool definitions](https://github.com/danieleugenewilliams/local-memory-releases/blob/20cdd249321e0406358aa3c85ea5be4b2ff3ad5c/docs/mcp-tools.md).

**What remains unknown.** This is a distributed binary with public API documentation, not an engine implementation inspected here. The reviewed update schema does not establish revision-bound compare-and-swap semantics, and domain/agent labels do not prove candidate isolation. Correction propagation, concurrent behavior and export/rebuild completeness require product-level examination. Local providers are documented, but fallback configuration must be explicit. [REST contracts](https://github.com/danieleugenewilliams/local-memory-releases/blob/20cdd249321e0406358aa3c85ea5be4b2ff3ad5c/docs/rest-api.md), [provider configuration](https://github.com/danieleugenewilliams/local-memory-releases/blob/20cdd249321e0406358aa3c85ea5be4b2ff3ad5c/docs/configuration.md).

**Assessment.** Worth evaluating, especially as a compact personal/project service. Current evidence is insufficient to make it the primary fleet core. This is an inspectability and demonstrated-contract issue, not a claim that proprietary software is inherently worse.

### 3.7 IAI personal memory engine

**What it provides.** IAI preserves literal personal episodes and retains changing facts with contradiction/supersession relationships. Recall can return contradictory or superseded records alongside matching records. Its project explicitly targets one person's memory on one machine. These are useful properties for persistent personal assistance and historical fidelity. [Project scope](https://github.com/CodeAbra/iai-personal-memory-engine/tree/479d08057d4fba21c29059befb1a2f43a3f8c651), [reference](https://github.com/CodeAbra/iai-personal-memory-engine/blob/479d08057d4fba21c29059befb1a2f43a3f8c651/docs/REFERENCE.md).

**“Procedural” means something narrower here.** Its documented procedural tier contains ten behavioral parameters; that is different from a versioned engineering runbook with preconditions, execution evidence and later-task outcomes. Turning the product into OMP's shared learning core would require significant service, scope and workflow extensions.

**One especially useful design detail.** The replaceable embedding provider records model identity as well as dimensions and rejects incompatible vector generations. This catches same-dimension model swaps that would otherwise silently corrupt retrieval quality. The documented HTTP provider only accepts unauthenticated loopback URLs, so a separate networked 5090 host is not a direct configuration substitution. [Embedding contract and migration](https://github.com/CodeAbra/iai-personal-memory-engine/blob/479d08057d4fba21c29059befb1a2f43a3f8c651/docs/EMBEDDERS.md).

Its reported LongMemEval retrieval results should not be read as end-to-end answer accuracy or evidence that it outperforms these other systems on engineering tasks. The reference explicitly reports a tie against its matched-embedder comparison. [Benchmark interpretation](https://github.com/CodeAbra/iai-personal-memory-engine/blob/479d08057d4fba21c29059befb1a2f43a3f8c651/docs/REFERENCE.md).

**Assessment.** Preserve it as a personal-memory option and borrow its fidelity/vector-identity lessons. It is not presently the most direct base for OMP's shared fleet requirements.

## 4. Compare complete architectures

Enola can run independently through its own CLI and agent interfaces. It uses source parsing and graph algorithms to identify structural relationships and regressions under declared policies. Cognee's existing importer makes integration easier; it does not give Cognee exclusive access to this capability. All primary architecture comparisons should receive equivalent structural information for the same source snapshot. Parser coverage and dynamic behavior still limit what any extracted graph establishes. [Enola project](https://github.com/enola-labs/enola).

| Architecture | Principal advantage | Main OMP work remaining | What would make it the winner |
|---|---|---|---|
| **Native OMP + Cognee + Enola** | Connect engineering sources and experience in one typed knowledge service | Immutable candidate views, outcome acceptance, explicit capture/trace integration, validity-aware context | Connected retrieval materially helps task completion and independent review |
| **Native OMP + Hindsight + Enola** | Strong dedicated historical recall and consolidation, with independent code facts | Join historical and structural evidence; native procedure lifecycle and context compilation | Better useful recall and synthesis outweigh the extra cross-service joining |
| **Native OMP + OpenViking + Enola** | Organize a large resource/skill corpus and load detail progressively | Map scopes and candidate resources; qualify service topology; integrate native evidence decisions | Workers find and apply the right resources with better outcomes and context efficiency |
| **Native OMP + Graphiti + Enola** | Explicit temporal graph foundation with control over schema and application behavior | More extraction, learning and context orchestration; evidence-aware recomputation | Temporal reasoning and custom control justify the greater application code |
| **Native OMP + SQL/search + Enola** | Small dependency surface and direct domain control | Build consolidation, retrieval quality improvements and procedure proposals | Full engines add no meaningful task benefit over this competent baseline |
| **Native OMP + Cognee + Hindsight + Enola** | Potential complementary connected knowledge and episodic recall | Duplicate ingestion, source mapping, fusion, disagreement handling, more failure modes | Ablation shows the second engine adds a material benefit beyond either alone |

The native component is necessary because OMP's exact acceptance rules, run identity and execution authority are application-specific. It need not become a new generic memory platform. Implement the minimum durable source/claim/procedure model and adapter contract required by real OMP workflows.

The common contract should carry source/version identities, content hashes, scope, processing state, evidence references, candidate snapshot sets and retrieval provenance. It needs explicit ingestion, lookup, query, correction and status operations. Preserve engine-specific capabilities through optional operations rather than flattening reflection, graph traversal and progressive context into one misleading `search()` abstraction.

The context compiler should combine required current native state, exact artifacts, structural facts and selected historical/procedural knowledge. It then checks validity and scope, applies the stage budget, and persists the delivered bundle. An auditor may receive historical claims as evidence to inspect; a prior agent's success conclusion must not silently become the auditor's instruction.

## 5. Disposition of the rest of the original landscape

These options were screened from their public documentation or project material; they did not receive the same implementation-level review as the seven above. They remain eligible if the relevant need or new evidence changes. This table records why they are outside the immediate primary shortlist, rather than silently dropping them.

| Option | Valuable role | Disposition for this OMP update |
|---|---|---|
| **Mem0 OSS / platform** | General-purpose personalized memory with substantial API ecosystem | Useful later recall comparator. Treat OSS and managed quality separately; current published platform claims do not establish local OSS results. [Project](https://github.com/mem0ai/mem0) |
| **Supermemory Local / Enterprise** | Document/memory retrieval with local and managed deployment choices | Retain as a service comparator. Local and Enterprise differ in model stack, access controls and operation; neither was disqualified for being paid or local. [Edition comparison](https://supermemory.ai/docs/self-hosting/local-vs-enterprise) |
| **MemMachine** | Episodic context and profile memory | Historical-memory challenger if Hindsight is weak on preserving original context in the actual workload. [Project](https://github.com/MemMachine/MemMachine) |
| **Honcho** | Modeling participants and their perspectives | Valuable if persistent human/agent relationship modeling becomes central; less direct for evidence-bound code procedures. [Project](https://github.com/plastic-labs/honcho) |
| **EverOS** | Editable memory and separated user/agent material | Portability and inspectability alternative; the user has not required files to remain canonical. [Project](https://github.com/EverMind-AI/EverOS) |
| **memU** | Agent-driven distillation into reusable wiki/skill material | Useful design reference for synthesis and host hooks; does not remove OMP's evidence lifecycle work. [Project](https://github.com/NevaMind-AI/memU) |
| **MCP Memory Service** | Shared memory API and accessible local retrieval | A practical simpler service comparator. Ease of connecting MCP does not establish full fleet learning. [Project](https://github.com/doobidoo/mcp-memory-service) |
| **Claude-Mem / Grok Mem** | Coding-session observation capture and staged retrieval | Host integration ideas are relevant; automatic OMP capture still needs deliberate adaptation. [Project](https://github.com/thedotmack/claude-mem) |
| **Letta / Letta Code** | Persistent agent memory and background dreaming within an agent harness | Consider individual mechanisms or a broader harness redesign only if justified. Importing overlapping orchestration is not inherently an improvement to OMP. [Memory design](https://docs.letta.com/configuration/memory) |
| **LangMem** | Extraction, consolidation and optimization primitives | Possible library components; introducing another framework solely for memory is not currently justified by a demonstrated gap. [Project](https://github.com/langchain-ai/langmem) |
| **OMP local / Mnemopi** | Existing native capture and retrieval paths | Retain as baselines and migration inputs. Their low integration cost is useful evidence, not proof of sufficient fleet capability. [OMP memory documentation](https://github.com/theturtlecsz/oh-my-pi/blob/aacabebf7c9d894ff8dca4b9fff188303b2491f4/docs/memory.md) |

Managed Zep, Mem0 and Supermemory remain legitimate candidates for a later “best achievable quality” service comparison. Their proprietary internals limit source inspection; they can still demonstrate superior outcomes. We should neither award them their vendors' claims nor exclude them because the 5090 exists.

## 6. What benchmarks can and cannot decide

Original LongMemEval tests longitudinal conversational memory, including retrieval-dependent questions, updates, temporal reasoning and abstention. It is useful, but results depend on the reader model, retrieval method, history variant and context budget. A retrieval hit rate is not answer accuracy. [Official LongMemEval](https://github.com/xiaowu0162/LongMemEval).

**LongMemEval-V2 is relevant to the updated plan.** Its 451-question suite covers static state, changing state, workflow knowledge, environment gotchas and premise awareness from web/enterprise agent histories. It evaluates memory supplied to a downstream reader with bounded context and reports accuracy/latency. This is closer to operational learning, but remains different from executing a correct repository change. Its public adapter interface is a useful reference for interoperability. [Official LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2).

No published number reviewed here fairly ranks the seven exact deployments for OMP. Models, extraction settings, datasets, scoring targets and commercial editions differ. Do not average those numbers into a decision score.

The eventual evaluation should have three layers:

1. **Correctness qualification:** can the system preserve identity, scope, source validity and recovery? Failures block promotion regardless of average recall scores.
2. **Controlled memory comparison:** same source history, reader, usable context budget and common baseline, with explicit model/embedding variations. This isolates why one retrieval/consolidation approach helps.
3. **Best achievable OMP architecture:** each finalist gets its appropriate configuration and models, including paid inference where useful. Judge actual task completion, repeated mistakes, independent review, latency and operational cost. This addresses the user's “best options, no limits” objective.

A strong baseline must include native current state, exact artifact lookup, keyword/dense retrieval and Enola where applicable. Comparing a richly equipped graph system with an artificially impoverished transcript baseline would exaggerate its value.

Run paired tasks against frozen histories, preserve chronological cutoffs, and keep answers and future outcomes out of memory construction. Report per-scenario results and uncertainty. Measure both ingestion and query expense: a low-cost recall call may conceal substantial extraction, reflection or backfill work.

### Prepared OMP scenarios — specifications, not executed benchmarks

| Scenario | Required observation / expected behavior |
|---|---|
| Historical rationale | Recover a decision and its exact original evidence without asserting it is still current. |
| Superseded decision | Prefer the current decision; retain the old one as historical context with explicit dates/versions. |
| Conflicting evidence | Surface the disagreement or abstain; do not manufacture a single confident resolution. |
| Parallel candidates | Candidate A's graph and receipts remain stable while candidate B is ingested. |
| Worktree identity | Shared repository lessons survive different checkout paths while candidate state remains separate. |
| Cross-repository applicability | Reuse a justified general procedure; exclude repository-specific instructions outside their scope. |
| Successful later reuse | A fresh worker applies a procedure under its preconditions and records independent outcome evidence. |
| Misleading success signal | A zero exit code or agent “done” message cannot promote a procedure without the required result evidence. |
| Counterexample and withdrawal | A later failure narrows or retracts a lesson; stale search results cannot restore its accepted status. |
| Architecture change | Find relevant changed relationships for the exact snapshot and state extraction coverage limits. |
| Recovery and partial publication | Retry duplicate events safely; reject conflicting payloads; keep incomplete graph views unpublished. |
| Missing evidence / bounded context | Abstain or surface the missing dependency; preserve required current state within the context budget. |

Each future fixture should record the source cutoff, relevant IDs, expected evidence, prohibited stale material, answer/task rubric and applicable scope. For procedure reuse, retrieval, actual use and outcome are separate observations.

Reuse the existing Harbor/evaluation direction for task execution and results. Add memory adapters, fixture manifests and trace export; do not start an unrelated benchmark platform. Benchmarks remain deferred under the current instruction.

## 7. Use the RTX 5090 where it improves the complete system

The GPU should be a shared inference service with explicit job roles, not a reason to select a memory engine or force a small generator to perform every task.

| Work | Initial placement | Selection rule |
|---|---|---|
| Document and query embeddings | Local GPU candidate | Qualify retrieval quality; pin model identity, revision, prefixes, pooling and dimensions. |
| Reranking | Local GPU candidate | Use the actual model scoring contract; measure quality and interactive delay. |
| Bounded extraction and routine lesson proposals | Local GPU candidate | Require adequate structured outputs and source fidelity. Escalate difficult cases to a stronger route when justified. |
| Difficult consolidation and reflection | Best qualified model, local or hosted | Choose by correctness and usefulness rather than local residency alone. |
| Source parsing, graph queries and native validity | CPU/data services | These operations do not inherently benefit from spending GPU capacity. |
| Coding and independent review | Existing qualified OMP routes initially | Keep memory-model changes separate from changes to worker/reviewer quality. |

Local embedding and reranking models are sensible initial uses because they are reusable across the finalist engines and repeated queries. Actual model artifact choice, residency, concurrency and throughput remain unqualified. The previously proposed Qwen configuration is a candidate profile, not a settled optimum.

Prioritize active context requests over bulk backfill; bound concurrency and cancellation. Record every provider route and declared fallback. If the GPU is busy or unavailable, a configured alternative can preserve service; an undeclared paid fallback obscures both quality and cost. A model migration must rebuild or explicitly migrate the affected index, even when vector dimensions match.

Local compute still has energy, availability and maintenance costs. No savings percentage or fleet-throughput claim is supported by this review. The goal is the best complete OMP behavior with productive use of the available GPU.

## 8. Immediate roadmap changes

The revised [OMP Immediate Roadmap Amendment](sandbox:/workspace/scratch/c5f3674f4c15/OMP_Immediate_Roadmap_Amendment.md) retains FK-0 through FK-7 and the full first-release lifecycle.

| Work package | Change from the previous recommendation |
|---|---|
| FK-0: reconciliation and architecture decision | Record Cognee as the lead build, Hindsight/OpenViking as finalists, and specific conditions that would change the selection. Preserve current native ownership. |
| FK-1: shared contracts | Define source/version, evidence acceptance, scope and retrieval provenance once. Do not design a universal plugin framework. |
| FK-2: capture/replay | Capture workflow/tool evidence through durable events and artifacts, beyond conversational memory hooks. |
| FK-3: memory and inference services | Implement the Cognee path first behind the small agreed boundary; preserve finalist adapter seams. Qualify deployment and models without requiring free editions or local-only generation. |
| FK-4: code snapshots | Keep Enola independently usable; publish immutable candidate views. This capability must not depend on Cognee winning the memory comparison. |
| FK-5: context compiler | Combine exact current state, structural facts and historical/procedural retrieval; persist the actual context delivered. |
| FK-6: learning/correction | Use model outputs as proposals. Apply native evidence acceptance, later-use outcomes, counterexamples and retraction. Borrow MemOS mechanisms only where they remove concrete implementation work. |
| FK-7: release qualification | Complete the full learning loop, recovery and visibility. Prepare benchmark-compatible traces; run no benchmark campaign in the current tranche. |

The first release remains capture → proposal → acceptance → fresh-task reuse → outcome → correction. A transcript search service alone would not complete it. Conversely, implementing three competing engines before any integrated journey works would add parallel machinery without establishing value.

**Change the lead architecture if evidence shows:** Cognee cannot provide reliable candidate views and provenance without disproportionate customization; Hindsight produces more useful task context with the same structural inputs; or OpenViking's context/resource approach yields better task outcomes with acceptable correction and recovery. Add a second engine only after isolating its incremental contribution.

## 9. Evidence boundary and source pins

This was a targeted source/contract review, not a comprehensive security audit or a production certification. Repository source was inspected at the following snapshots. Documentation-only and managed-product claims have lower implementation visibility. The Local Memory website was not available through the research route; its official release repository supplied the API documentation. The user's installed product version was not inspected.

| Component | Source snapshot |
|---|---|
| OMP fork | `aacabebf7c9d894ff8dca4b9fff188303b2491f4` |
| Cognee | `c0d18c80e24b7b78918e7642c03f6f128fdd2aee` |
| Hindsight | `4cc131c0b238c8f206def60804d7b6591f6a45e7` |
| OpenViking | `0f77ab5625a5e2ead4f286d4e58ea77e05fcfdee` |
| Graphiti | `c035afb7990b6077331a81e98b04efcfd9bf8184` |
| MemOS | `de8069428a9247bfa7a3d35f59a9b39fa8f231d2` |
| Local Memory release documentation | `20cdd249321e0406358aa3c85ea5be4b2ff3ad5c` |
| IAI | `479d08057d4fba21c29059befb1a2f43a3f8c651` |

The live WorkService, current native issue states, installed OMP release, media-discovery checkout and GPU host were unavailable in this review. The deliverables are this decision report and a revised roadmap artifact. They do not claim deployed services, changed repository code, allocated native issues or measured superiority.
<!-- END_EMBEDDED_FILE: OMP_Memory_Candidate_Decision_Report.md -->

## Appendix B — Embedded revised immediate roadmap

The following block is the exact revised roadmap to extract and save as directed in section 2. Its dated native issue references require current readback.

<!-- BEGIN_EMBEDDED_FILE: OMP_Immediate_Roadmap_Amendment.md -->
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

<!-- END_EMBEDDED_FILE: OMP_Immediate_Roadmap_Amendment.md -->
