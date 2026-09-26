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
