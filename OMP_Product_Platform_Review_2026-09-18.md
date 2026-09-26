# OMP Product and Platform Review
Prepared 18 September 2026. Review only; no repository, deployment, or backlog changes.

**Recommendation: continue OMP, preserve the full ambition, and reorganize delivery around completed user goals.** The product thesis is coherent. The architecture contains substantial, useful work. Operational usefulness and total economics remain insufficiently demonstrated.

The main danger is that the system becomes exceptionally thorough at governing work while remaining expensive to get useful work through.

**1. DIRECT VERDICT**

| Question | Verdict | Confidence |
|---|---|---|
| Is there a clear product? | **Yes, at the level of purpose:** a persistent engineering and research assistant that accepts goals, produces independently supported results, and improves from experience. Its first dependable user experience is still unfinished. | High on intended purpose; medium on eventual demand and advantage. |
| Is there a clear roadmap? | **Partly.** The latest program preserves scope and dependencies unusually carefully. It is stronger as a capability specification than as a prioritized sequence of usable releases. | High. |
| Is there an executable plan? | **For many bounded engineering changes, yes. For the whole program, not yet.** Current authority, integrated release identity, unavailable evidence, and some provider capabilities still determine whether planned work can proceed. | High on inspected source; lower on the inaccessible live environment. |

The strongest aspects are the separation of work authority from model judgment; preservation of exploratory research; candidate-bound evidence; correction of learned knowledge; and the explicit requirement to measure human effort and all supporting costs. These are substantive architectural advantages.

The principal weaknesses are incomplete end-to-end qualification, excessive integration and supervision overhead, conflicting generations of operating policy, and insufficient evidence that the added machinery beats a much simpler system.

**What I inspected**

The source baseline matters:

- OMP main: **1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1**, dated September 12.
- Newer program branch: **46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b**, 46 commits ahead of main. This includes knowledge and budget infrastructure that a main-only review would miss. [Branch comparison][compare]
- WebUI master: **08a277a542adb6217ba5e6013ae34d5af10f3fff**, dated September 1.
- Current program decisions, original obligations, full ECC and autoresearch plans, Fleet Knowledge and cost-control handoffs, Harbor/reliability material, and the V9.1 archive with its dependency and technical contracts.

I inspected selected implementation and test code, plus actual GitHub CI logs. This was a targeted architectural review, not a line-by-line audit or a new test run.

**Unavailable evidence:** live WorkService records and grants; the actual installed processes and configuration; host-local economy instructions and Antidote; most linked local review receipts; actual provider balances, total usage, human-time records, and GPU operation. An attempted download of the installed qualification archive returned HTTP 403; its CI job logs and artifact metadata were accessible.

These limits make deployment and economic conclusions provisional. They do not prevent identifying concrete source and sequencing problems.

**Capability maturity**

All major ambitions are desired and substantially specified. Their implementation and proof differ:

| Capability | Implementation evidence | Testing evidence | Deployment and demonstrated usefulness |
|---|---|---|---|
| WorkService, isolation, recovery | Substantial merged code. | Main CI records **121 PostgreSQL integration tests** and **22 installed isolation/recovery tests passing**. These are meaningful process tests with controlled provider inputs. | Current user installation and sustained ordinary-work benefit unverified. |
| Native tiered execution and budgets | Substantial candidate-branch code, including durable dispatch and rate-card/quote records. | Local test/review results recorded in MASTER; no Actions run returned for the inspected candidate head. | Latest checkpoint explicitly leaves installation, native acceptance, and further dispatch/accounting integration open. |
| ECC integration | Full detailed specification; the inspected trees do not contain the proposed dedicated mirror/adapter layout. | No installed ECC activation evidence obtained. | Unverified. |
| Knowledge/context/correction | Real candidate package, compiler, adapters, and tests. | Recorded component/integration results; several tests use controlled engines and seeded evidence. | Fresh-task benefit and full two-repository learning journey remain open. |
| Neurosymbolic intake/exploration | Existing intake plus detailed NSI/03B contracts and ownership. | Documentation qualification is explicitly distinguished from runtime qualification. | Full interpreted-claim → constraint-check → ratification journey not demonstrated here. |
| Full autoresearch | Existing native experiment loop and extensive R00–R19 design. | Existing loop tests; full campaign/evaluation/self-improvement qualification unavailable. | Autonomous research usefulness and recursive improvement unproven. |
| WebUI | Real session workbench with daemon, RPC, replay, files, and terminals. | Green CI, with important coverage limits described below. | Unified WorkService/research control surface and current live deployment unverified. |

Sources: [main CI][main-ci], [installed test implementation][installed-tests], [current program status][master], [knowledge implementation][knowledge], [intake addendum][intake], [WebUI CI][web-ci].

Documentation sometimes creates more confidence than its evidence supports:

1. **Many accepted slices do not establish an accepted system.** MASTER carefully labels recent work as source-development scope and still leaves full ECC, research, installation, and native acceptance open. The labels are honest; their volume can obscure the missing usable result. [Current status][master]
2. **A four-role demonstration is not an autonomous engineering qualification.** The recorded journey changes hello.txt, reports out-of-scope generated files being removed before sealing, and contains historical “not yet” statements that conflict with its later live-journey account. It supports limited transport/composition evidence, not unattended useful delivery. [Journey record][journey]
3. **Green WebUI CI overstates some coverage.** Its logged installation is upstream OMP 18.1.2, not the pinned fork. Some daemon tests print a skip message and return successfully when the stub is unavailable; the browser job runs only happy-path and accessibility smoke tests. [Workflow][web-workflow], [early-return test][web-test], [run logs][web-ci]
4. **The two qualification gates remain obligations, not results:** 20 consecutive accepted ordinary tasks, plus the separate 72-hour mixed workload with at least 30 distinct tasks and 100 actual experiment attempts. I found no accessible evidence that the current integrated release passed them. [Preserved qualification requirements][preservation]

**2. PRODUCT PURPOSE AND REAL USEFULNESS**

The clearest product definition is:

> A personal engineering and research system that owns an authorized goal across sessions, investigates uncertainty, delivers reviewable evidence and artifacts, and carries useful corrections into future work.

The initial user is one technically capable owner managing real repositories. Collaborator identities should fit the design, but commercial SaaS, billing, tenancy, and a marketplace are not necessary to establish value. This matches the settled product decisions. [Accepted decisions][decisions]

Three concrete end-to-end outcomes should define usefulness:

| Outcome | User experience | What constitutes success |
|---|---|---|
| **Deliver a real engineering improvement** | Give OMP a bounded media-discovery problem. Answer only material questions. Return to a tested patch, evidence, and an intelligible disposition. | The original problem is solved without workflow reconstruction; review, integration, recovery, and cost are included. |
| **Resolve a consequential technical uncertainty** | Ask whether an embedding, reranking, inference, or implementation alternative improves the actual workload. OMP investigates sources, establishes controls, revises hypotheses, experiments, and reproduces the result. | A defensible decision, including a useful negative or inconclusive result, with reproducible artifacts and explicit uncertainty. |
| **Prevent repeated engineering mistakes** | A supported lesson from one task helps a genuinely new task; later counterevidence corrects its applicability and identifies affected decisions. | Better subsequent outcomes, appropriate cross-repository scope, and effective withdrawal—not merely successful retrieval. |

These form **one platform with two principal workflows: engineering delivery and research**. Learning strengthens both. The IDE/workbench, distributed fleet infrastructure, and general scientific adapters are supporting programs that can each become a separate product-development burden.

There is a credible simpler competitor to your own design: **a capable coding agent using selected ECC methods, Git, existing CI, a basic experiment runner, and a small evidence/notes store**.

| Need | Simple alternative | OMP must demonstrate |
|---|---|---|
| Occasional coding task | Already handles much of this adequately. | Less owner supervision or materially better outcomes. |
| A few experiments | A script, frozen inputs, and independent checks can suffice. | Useful adaptive investigation, persistence, and reproduction beyond scripting. |
| Repeated related work | Notes and repository instructions provide a baseline. | Correct, beneficial, scoped learning with practical correction. |
| Long-running concurrent work | Requires more manual coordination. | Durable goals, coherent cancellation, fair resource allocation, and reliable integration. |

ECC itself advocates proportionate task-local harnesses and selective installation. Substantial reuse is compatible with simplicity. Copying more process does not automatically improve results. [ECC workflow guidance][ecc-dynamic], [installation profiles][ecc-profiles]

**OMP’s strongest potential advantage is continuity and trustworthy learning across completed goals.** A fixed cast of named models is an implementation choice, not the product’s distinguishing benefit.

**3. UNIFIED ROADMAP AND COMPLEXITY AUDIT**

Keep existing work identities. The following is a proposed disposition of existing scope, not a new backlog. Delivery numbers refer to section 6.

| Existing initiative | Recommendation | Benefit, tradeoff, and simplest adequate implementation |
|---|---|---|
| SQL-0, WorkService, H0–H4; stabilization OMP-233/246/249 and related fixes; R00/WP1 | **Keep; consolidate qualification.** Delivery 1. | One authority for work and effects, with generated clients and an admitted release. Reuse merged recovery code. Verify retirement of any legacy Linear/Robomp work-writing paths; preserve provenance and unrelated SQLite stores. |
| Native runner, Fleet execution primitives, 03B E1/E2, cost PR3, R02/R03 | **Combine.** Deliveries 1–2. | One shared job/admission/recovery implementation with model, CPU, and later GPU adapters. Retire duplicate launch/retry logic after qualification. Shared implementation does not require every workload to have the same research process. |
| ECC WP1–WP3 and R01 | **Keep substantial reuse; simplify activation.** Delivery 1. | Complete pinned source mirror, small OMP adapter, task packs, dependency closure, ownership-aware update/remove/rollback. Retire replaced bespoke instructions asset by asset. Do not build a second general installer or activate the entire mirror. |
| Typed audit WP4, H2/H3, Fleet verification, R06 | **Combine evidence contracts; keep independence.** Deliveries 1–2. | A candidate-bound verification route shared by delivery and research. Separate acceptance policy from scientific scoring. Retire prose-as-proof and duplicate parsing; retain diagnostic history. |
| NSI-1–NSI-6 and 03B/OMP-267–270 | **Keep intelligence; simplify initial domain coverage.** Deliveries 1–2. | Interpret requests into claims, evidence, constraints, gaps, and acceptance criteria. Add formal solvers only for encodings that improve actual decisions. Preserve exploration and the admitted valid option pool. |
| Fleet Knowledge FK-1–FK-7, FLEET-5, ECC WP7A, R04/R12 | **Combine around one learning loop.** Delivery 3. | Shared source/evidence identities and one context compiler. Keep Cognee/Enola candidate code behind replaceable interfaces. An evidence store plus exact retrieval remains the comparison baseline. |
| Graph/vector backends and local inference | **Keep the selected path provisionally; defer breadth.** Delivery 3. | Qualify one storage/serving composition first. Preserve alternative adapters without operating them all. Retire unused active services after migration proof. More backends increase patching, backup, and compatibility work. |
| Usage/accounting WP5, cost PR1–PR3, research resource budgets | **Keep; finish the consumption path before more policy machinery.** Deliveries 1–2. | Connect reservations to actual dispatch, outcomes, unknown usage, and useful-goal reporting. Retire conflicting enforcement copies. A bounded approved account/route is enough for the first release. |
| Cheap Best-of-N and frontier consultations | **Keep as selectable recipes; test mandatory use.** Deliveries 1–2. | Compare one worker with bounded cheap sampling at equal total resources. Starting N=1 for routine work would change the earlier default recommendation; it preserves Best-of-N capability and needs an explicit policy decision. |
| Advisor, Observer, ECC WP6/WP7B, H5 | **Keep required coverage; simplify presentation and duplication.** Deliveries 1–3. | Database/data-system advice remains the first specialization. One finding identity, deduplication, actionable notifications, and scoped lessons. Retire a prompt/observer mechanism only after its replacement proves equivalent coverage. |
| CPK-0–CPK-6 | **Keep its invariants; defer general framework expansion.** | Version identity, bounded capabilities, lifecycle, and replay are useful now. Ordinary modules and existing extension seams are the adequate starting point. Retire bridge code only when the actual replacement works; avoid maintaining two plugin runtimes. |
| Native autoresearch, R05–R11/R17 | **Keep full research ambition; deliver a narrow complete journey first.** Delivery 2, then domain expansion. | Preserve adaptive actions, hypothesis revision, negative findings, independent evaluation, and reproduction. Label old agent-entered results as unmanaged; remove their eligibility to serve as trusted promotion evidence. |
| Bilevel/harness improvement R13/R14; recursive R19 | **Keep; gate promotion on fresh-task benefit.** After Deliveries 2–3. | Generated executable mechanisms and broader runtime candidates remain in scope. Start with one search/context mechanism and a protected confirmation set. Retire losing versions; do not accumulate permanent experiments. |
| OWEB, WFM, R16, desktop/Paseo | **Keep one product surface; sequence capabilities.** Minimal controls in Delivery 1; workbench later. | Reuse session replay and presentation. Work/campaign controls consume native APIs. Keep local terminals separate from remote supervisory permissions. Monaco, anchored review, desktop packaging, updates, and mobile work remain retained later obligations. |
| Harbor OMP-250/252, R18 | **Keep evaluation portability; simplify initial execution.** | Use existing installed-process fixtures for the first pilot. Harbor remains an external adapter, not runtime authority or a prerequisite for useful evaluation. Retire redundant test harnesses only once their behavioral coverage transfers. |

This reconciles the preserved V9.1 families with ECC WP1–WP8, FK-0–FK-7, and research R00–R19. The complete contracts remain the requirements source; package IDs are not evidence of live backlog state. [Scope crosswalk][preservation], [ECC specification][ecc-plan], [research work packages][research-plan]

**The most consequential overlaps and conflicts**

- **Program status versus work authority.** MASTER is described as canonical program status while WorkService owns native work. This can work if MASTER is an index/projection with explicit freshness. Hand-maintaining several “current” handoffs invites divergence. The tracker itself eventually calls for becoming an index. Preserve immutable historical material; stop duplicating current state. [MASTER][master]
- **Model policy has multiple generations.** The September 15 OMP_Cost_Control_Implementation.md handoff proposes low/medium ordinary effort and cheap review (sections “Decisions carried forward” and “Initial operating policy”). The candidate native profile pins Fable and Gemini at high effort, while its audit code enforces Sol medium; older handoffs describe Kimi native auditing. External development and native acceptance are distinct policies, but the reader must currently reconcile them manually. Generate an effective role-policy view and mark superseded recipes. [Native profile][profiles], [audit policy][audit-policy]
- **Knowledge is connected before relevance is established.** The inspected compiler queries using the stage name, requests ten results, and sorts facts by ID before packing. The documented reranker only reorders the already limited candidate set, and its order does not affect compiled bundles. Stable serialization is useful; suppressing relevance effects is not an outcome requirement. [Compiler][compiler], [documented reranking limitations][knowledge]
- **Integration can invalidate component success.** MASTER records migration-history collisions and repairs tested against different source compositions. This supports an explicit integration owner, exact dependency baselines, and integration tests at merge boundaries. More reviewers per isolated slice will not solve mismatched compositions. [Integration history][master]
- **Some dependencies are stronger than their practical rationale.** The newer scope still defers WebUI/Paseo until nine core gates. Basic observation and reliable controls would help qualify those gates. Bring forward a thin supported surface, while retaining later workbench and remote-security requirements. This is a sequencing amendment, not permission to expose unfinished mutation APIs. [Retained gates][preservation]

**Migration and upstream maintenance**

Keep OMP’s fork, ECC adaptation, WorkService schema, and WebUI protocol as separately versioned compatibility boundaries. A qualified release must bind their actual combination.

Preserve existing upstream inventory, but make each maintained divergence carry a reason, protecting behavior, and retirement condition. Monthly candidate preparation is already accepted; activation remains separate. Avoid replacing the fork with another harness without evidence that the maintained boundary costs more than the value it supplies. [Accepted update policy][decisions]

Freeze the accepted release while integrating candidates elsewhere. Use additive schema changes and compatible readers; retain original migration bytes. Migrate historical knowledge as historical claims, not newly verified facts. Retire source-linked production loading only after an installed release and rollback path are qualified.

Keeping every historical design does not require keeping every historical mechanism active.

**4. MISSING PIECES AND UNTESTED ASSUMPTIONS**

Most essential ideas already have a design. The larger gaps are implementation integration, empirical validation, and deciding which requirements deserve universal enforcement.

| Priority | Gap | Type | Evidence that would resolve it |
|---|---|---|---|
| 1 | A useful owner goal completed through the integrated installation with low supervision | Integration + validation | Representative tasks, full failure denominator, independent acceptance, total owner minutes and resources. |
| 1 | Recoverability when a provider request has an unknowable result | Design/policy decision + integration | Fault tests for each effect class, conservative usage accounting, bounded continuation, and external-effect reconciliation. |
| 1 | Trustworthy measurement of correctness and research conclusions | Partial implementation + validation | Seeded false positives, altered evaluators, wrong-candidate evidence, held-out tasks, independent reproduction. |
| 1 | Exact installed/current evidence and usable release path | Operational qualification | Runtime identity readback, current native records, approved compatibility, restore and promotion evidence. |
| 2 | Context that improves decisions | Product validation + implementation | Task-relevant retrieval compared with exact search/notes, measured downstream benefit, and negative-transfer tests. |
| 2 | Integration of concurrent work | Design detail + operating discipline | Two changes pass separately, conflict or interact, then integrate correctly against a frozen combined candidate. |
| 2 | Economic allocation between delivery, research, and meta-research | Specified, insufficiently measured | Shared-account reservations plus owner-time, queue delay, failed work, and maintenance reporting. |
| 2 | Goal-to-outcome traceability | Integration | Every consequential change and conclusion links to the original goal, active criterion, evidence, and actual user-visible result. |

**Recovery needs effect-specific semantics.** The current provider-observation classifier reports all configured native routes as unsupported for authoritative outcome lookup. A requirement that every uncertain preflight must obtain such lookup can produce a permanent stop after an ordinary transport failure. More schemas cannot create a provider capability. [Capability implementation][provider-observation]

My proposed policy distinction:

- For **pure inference without external tools**, retain an uncertainty record and its conservative resource hold. Permit a separately reserved replacement request within an authorized retry envelope; never pretend the earlier request was free or absent.
- For **isolated candidate execution**, verify worker termination and quarantine late output before retrying from an immutable input.
- For **GitHub writes, deployments, database changes, or instruments**, reconcile the real effect or require an authorized recovery decision.

This openly relaxes a universal “never repeat an uncertain billable request” interpretation. It preserves strict control of consequential effects and bounded total spending. It is a recommendation for policy review, not a change performed here.

**Evaluation must test the evaluator.** The native autoresearch logger accepts the agent’s metric and keep/discard choice; discrepancies and kept scope deviations can become warnings. That is useful experiment history, but an inadequate acceptance oracle. Managed research needs independently obtained measurements bound to exact inputs and candidate bytes. [Experiment logger][experiment-log]

Also distinguish:

- Tests passing from the user’s problem being solved.
- An authentic receipt from a scientifically valid measurement.
- Independent model context from independent underlying evidence.
- Repeatability from generalization.
- Missing evidence from a negative scientific result.

Use code checks for enforceable invariants, calibrated semantic review for meaning, and human sampling for quality calibration. Making every grader deterministic would weaken evaluation of open-ended research. [Evaluation guidance][eval-guidance]

**Constraints worth revising explicitly**

1. Standardize user interactions, operation envelopes, and authority checks. Let research choose and revise methods within those boundaries.
2. Keep runtime authority and evaluator versions fixed during a trial. Permit evolving hypothesis/search graphs as campaign data. A frozen plugin dependency graph must not become a frozen research strategy.
3. Preserve generation/critique separation for initial diversity and independent acceptance. Permit collaboration, synthesis, and hypothesis combination afterward.
4. Keep owner ratification for product meaning. Consider a standing bounded exploration policy later; the current 03B addendum still requires explicit authorized use. Do not silently interpret general autonomy as amending that rule. [Exploration policy][intake]
5. Give implementers safe development-test feedback in a sandbox or through a bounded host runner. Protect acceptance graders and credentials separately. A universal lack of executable feedback makes cheap workers need more supervision.
6. Treat fixed model names, per-slice frontier planning, and repeated frontier reassimilation as recipes to test. Preserve current obligations until an explicit replacement policy is accepted.

**Essential now:** exact identity, durable progress, basic containment, independent verification, cancellation, effect-aware recovery, complete outcome accounting, and a usable inspection path.

**Needed primarily at scale:** broad plugin composition, many graph backends, distributed scheduling, general instrument support, rich desktop/mobile packaging, and extensive automated mechanism search. Their eventual scope is preserved.

**5. THE 10× PRODUCT/PLATFORM THESIS**

The strongest credible thesis is:

> OMP can reduce the human effort of carrying a consequential engineering or research goal to a trustworthy result by an order of magnitude, while accumulating evidence that makes subsequent goals easier.

A concrete experience: you submit a media-discovery improvement goal with a quality floor, resource window, and allowed effects. OMP asks one consequential question if needed, investigates, tests alternatives, revises a weak hypothesis, survives a worker failure, reproduces its result, and returns a concise decision plus a reviewable patch or justified negative conclusion. A later task benefits from a supported lesson; counterevidence corrects it.

The mechanisms creating advantage are durable goal ownership, appropriate context, adaptive experimentation, reliable recovery, and selective human attention. More model roles are useful only insofar as they improve those mechanisms.

**What “10×” would mean**

| Dimension | Baseline and metric | Requirement |
|---|---|---|
| Human effort | Total active owner minutes per useful goal versus capable agent + ECC + basic experiment loop | At most one tenth, including intake, supervision, recovery, review, and allocated maintenance. |
| Throughput | Comparable accepted outcomes per wall-clock period | Report separately, under a fixed resource envelope. |
| Economics | Total attributable cost divided by useful outcomes | Include failed attempts, helpers, compute, subscriptions allocated consistently, and maintenance; report unknown components as unknown. |
| Quality | Correctness, user acceptance, false acceptance, reproduction, and scope fidelity | Meet predefined non-inferiority requirements; never exchange away critical integrity. |
| Research quality | Valid, decision-relevant conclusions and fresh-task generalization | Separate scientific validity from engineering throughput; negative conclusions can count when they resolve the question. |

For illustration only, reducing active owner time from 50 minutes to 5 minutes per comparable goal would meet the human-time ratio. It says nothing by itself about throughput or dollar savings. A defensible 10× claim needs a preregistered task population, frozen quality thresholds, complete failures and exclusions, and enough independent tasks/repeats for an uncertainty interval that supports the claimed ratio. A zero-intervention pilot should be reported with its sample size, not as an infinite gain.

Maintain both an operating view and an investment view: incremental cash/quota/compute now, and total ownership cost including development and upkeep over an explicit usage horizon. Existing subscription cost can be sunk for a marginal routing decision but still belongs in a replacement/retention decision.

The research plan already separates these metrics well. Its internal ECC–OMP baseline is necessary for ablations; add the simpler external alternative so the product does not only prove superiority over its own complexity. [Existing measurement plan][research-plan]

**Highest-value bets and cheap falsification experiments**

These are proposed studies, not authorization to spend or run them.

| Bet | Cheapest meaningful experiment | Falsifying evidence |
|---|---|---|
| Durable goal ownership removes the coordination tax | A small matched pilot, around 8–12 representative tasks, using the simpler baseline and one fixed OMP recipe; record every intervention and failure. | Owner time is unchanged/worse, or repair merely moves into setup and maintenance. |
| Scoped knowledge improves fresh work | Freeze a small corpus of resolved cases and later tasks. Compare exact retrieval, current compiler, and task-relevant context; include misleading and withdrawn lessons. | Retrieval increases tokens without outcome benefit, or stale/incorrect transfer increases defects. |
| Adaptive research beats fixed sampling | A few real performance/retrieval investigations, compared with a basic loop and cheap Best-of-N at the same total budget. | Improvement disappears under matched budgets, fresh workloads, or independent reproduction. |
| Self-improvement repays its own cost | Generate and qualify one executable search/context mechanism on development tasks, then evaluate unseen tasks with a separately frozen evaluator. | No downstream gain, gain only on training cases, or amortized research/maintenance cost exceeds benefit. |

These pilots can reject bad bets cheaply. They cannot, by themselves, substantiate a general 10× claim.

Bilevel research supports investigating executable improvements to search mechanisms. Its published experiment is narrow, and recursive self-application is presented as further work. It does not establish OMP’s productivity multiplier. [Bilevel paper][bilevel]

**Present assessment:** 10× lower supervision is a plausible, unvalidated hypothesis for repeated, interruption-prone work. A general 10× gain in research quality or total economic efficiency remains aspirational.

**6. A CORRECTED DELIVERY PLAN**

Use the existing program and work owners. Change the unit of release to a usable journey, while retaining the complete program’s acceptance obligations.

**First three useful deliverables**

| Delivery | Reconciled scope and dependencies | Observable acceptance and decision gate |
|---|---|---|
| **1. One dependable goal-to-reviewed-change workflow** | R00/WP1 installed baseline; applicable OMP-233/246/249 and existing recovery fixes; a selected ECC pack from WP2/WP3; typed audit and minimum complete accounting from WP4/WP5; bounded intake; thin inspect/pause/stop surface. | Complete a real media-discovery task through the intended native path. Include restart/lost-response behavior, correct cancellation, independent evidence, actual release identity, and owner-time/cost reporting. Proceed only when it is useful without reconstructing workflow state. Retain the existing 20-trial adoption gate. |
| **2. One reproducible adaptive engineering research campaign** | Reuse Delivery 1; R01–R07 plus initial R10 and R17; shared 03B/Fleet job primitives; no full knowledge-service dependency. | Establish baseline, investigate alternatives, revise a hypothesis from evidence, retain a failed candidate, independently evaluate, restart, cancel, and reproduce in a clean environment. Deliver a useful patch or defensible conclusion. Proceed if it beats or meaningfully extends the basic loop. |
| **3. One closed learning-and-correction journey across two repositories** | FK contracts/capture/context/correction; FLEET-5; WP7A/WP7B; R04/R12; the selected Cognee/Enola/inference composition. | Real task → supported lesson → native admission → different task uses it → outcome recorded → counterexample changes applicability → future context respects correction. Demonstrate candidate isolation, permitted/forbidden transfer, optional-service outage, and rebuild. Retain graph complexity only where it improves the result. |

Delivery 1 is deliberately useful before the entire platform is complete. It is not a catalog, manifest collection, or “all services start” milestone.

I recommend advancing Delivery 2 before completion of the full knowledge stack. This changes the earlier recommendation that Fleet Knowledge should be the next major complete release. It preserves all knowledge obligations while obtaining a real research user and workload sooner. The accepted decisions already allow an initial ML workflow independently of full Fleet Knowledge. [Product decisions][decisions]

**Then reach the full ambition through the same owners**

1. Expand the first research workflow into repository-grounded literature synthesis and one embedding/reranking or inference experiment: R08–R11 and R17. Preserve one dependable workflow per domain before breadth.
2. Complete the combined learning, trusted evaluation, and release path, then evaluate generated executable mechanisms: R12–R14.
3. Add continuous campaigns, scheduled/regression triggers, bounded distributed workers, and resource fairness: R15. Preserve cumulative allowances across renewal and a separate capped recursive-research allocation.
4. Extend the existing UI through R16/OWEB/WFM: first coherent monitoring/control, then authoring, candidate-bound review, Monaco, activity drill-down, layout, reconnect-safe terminals in local scope, desktop/mobile packaging, and qualified updates.
5. Execute R18 qualification throughout promotion; advance R19 recursive research only when fresh downstream benefit can pay for it.

The full-system qualification still includes the 72-hour mixed workload, 30 distinct ordinary tasks, 100 actual experiment attempts, coverage requirements, and the separate 20-consecutive-task gate. Shared raw evidence counts only when the protocols actually match. These are adoption requirements, not statistical proof of universal reliability. [Qualification contract][preservation]

**Break the circular dependencies**

- Repair and package OMP with an independent development tool; production OMP need not repair itself.
- Use already defined, ratified task contracts for initial execution; do not wait for every future NSI domain.
- Reuse shared components early without claiming full Fleet activation.
- Supply minimal inspection before the core is declared complete.
- Qualify learning on real delivery/research tasks; do not require a complete learning system to produce the first task.
- Prepare missing deployment/recovery interfaces without fabricating live authority.

**Effort and capacity**

I would not endorse a fresh whole-program calendar estimate without installed-state and owner-time evidence. The existing autoresearch plan estimates 28–48 engineer-weeks and assumes experienced engineering capacity; it is not a forecast of your remaining work or of agent-assisted calendar speed. [Existing effort assumptions][research-plan]

For prioritization only, assume one experienced engineer-equivalent of focused capacity, a working development environment, reusable candidate code, no hosting rewrite, and prompt access to required qualification decisions:

- Delivery 1: roughly **2–5 engineer-weeks** after current-state reconciliation.
- Delivery 2: roughly **3–6 additional engineer-weeks**.
- Delivery 3: roughly **3–6 additional engineer-weeks**, with high uncertainty around the recorded local integrations.

These are judgmental planning ranges, not observed velocity. Unresolved provider semantics, live-state recovery, or a substantially different local candidate can move them materially. Reforecast after Delivery 1; agent count is not an engineer-equivalent count.

**Stop doing now**

- Adding general control-plane abstractions without naming the next user journey they unblock.
- Treating each small implementation slice as requiring an invariant full frontier committee; propose a risk-based recipe change while preserving current gates until accepted.
- Expanding backend/model combinations ahead of one qualified composition.
- Recopying complete historical plans into every active work packet. Preserve immutable requirements and load the relevant contracts with an explicit coverage map.
- Reopening delivered reliability fixes from stale issue descriptions.
- Equating component test counts, model PASS verdicts, or documentation completeness with useful release progress.
- Making broad UI or plugin redesign a prerequisite for the first research outcome.

**7. WHAT I HAVE NOT ASKED**

The settled Q1–Q35 product preferences need not be interviewed again. The consequential unresolved questions concern policies and evidence that change this strategy.

| Question | Provisional recommendation | Consequence of choosing differently |
|---|---|---|
| Is this primarily a productivity tool, or also a research hobby whose complexity is part of its value? | Measure productivity on external repository goals, and give platform experimentation its own explicit allocation. | A research-first project can rationally accept negative near-term ROI, but should stop describing that investment as demonstrated efficiency. |
| May pure inference retry under a bounded allowance when the earlier request cannot be authoritatively looked up? | Yes, with conservative accounting, separate attempt identity, and no relaxation for consequential effects. | Retaining universal lookup requirements may require different providers or recurring operator intervention. |
| Is the fixed model hierarchy a product requirement or an initial operating recipe? | Treat it as a versioned recipe. Preserve independent acceptance and test when frontier planning/escalation earns its cost. | A permanent hierarchy trades predictable staffing for latency, provider fragility, and potentially higher total cost. |
| Can minimal monitoring/control precede completion of the entire knowledge stack? | Yes, through already qualified backend operations. | Keeping the current broad UI dependency delays usability evidence and makes qualification harder to supervise. |
| What minimum advantage justifies operating OMP? | Require lower owner effort at comparable quality, then evaluate whether extra cost is justified; do not demand 10× before recognizing smaller useful gains. | Without an explicit threshold and usage horizon, every attractive addition can justify itself indefinitely. |
| What happens to the currently unresolved live authority state? | Require current readback and a concrete recovery/cutover proposal through the existing route. | A generic “continue” instruction cannot establish safe resumption or deployment. Q36 remains separately withheld, and this review does not authorize it. |

I am least certain about the actual live installation, the completeness of host-local candidate work, and real supervision economics. Current native records, an exact installed manifest, accessible raw qualification receipts, and a short task-level time/usage cohort would materially change those conclusions. They could show the project is closer to a useful release than the remotely visible evidence establishes.

**Prioritized decisions**

1. **Preserve:** substantial ECC reuse, useful neurosymbolic interpretation, full adaptive and recursive research, independent evidence, scoped learning/correction, and predictable user controls.
2. **Change:** release sequencing, duplicated policy/status representations, effect-insensitive recovery requirements, and mechanisms that prevent relevance or collaboration without a demonstrated benefit.
3. **Validate next:** the installed goal-to-result journey, task-relevant context, complete owner-time/cost accounting, and the value of the fixed model hierarchy.
4. **Single most valuable next action:** turn one bounded media-discovery problem into the acceptance journey for the existing program—using current work owners, a frozen candidate, the required independent checks, explicit recovery cases, and a complete owner-time/resource record. Produce a useful result before widening the platform.

[compare]: https://github.com/theturtlecsz/oh-my-pi/compare/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1...46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b
[master]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/MASTER.md
[decisions]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/docs/programme/ACCEPTED-DECISIONS.md
[preservation]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/docs/programme/SCOPE-PRESERVATION.md
[ecc-plan]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/docs/programme/ECC-IMPLEMENTATION-SPEC.md
[research-plan]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md
[main-ci]: https://github.com/theturtlecsz/oh-my-pi/actions/runs/34694918256/job/103557027447
[installed-tests]: https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/python/omp-work/tests/test_installed_execution_recovery.py
[knowledge]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/python/omp-knowledge/README.md
[compiler]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/python/omp-knowledge/src/omp_knowledge/context/compiler.py
[profiles]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/session-system/extensions/workflow/native-stage-profile.ts
[audit-policy]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/session-system/extensions/workflow/audit-policy.ts
[provider-observation]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/session-system/extensions/workflow/provider-observation.ts
[journey]: https://github.com/theturtlecsz/oh-my-pi/blob/46068d9f95ee5fe3faee8599a21a9a0f0d8aa76b/native-stage-integration-r1-evidence.md
[intake]: https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/omp-intake-exploration-implementation-plan.md
[experiment-log]: https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/autoresearch/tools/log-experiment.ts
[web-ci]: https://github.com/theturtlecsz/omp-webui/actions/runs/33562465646/job/100037949640
[web-workflow]: https://github.com/theturtlecsz/omp-webui/blob/08a277a542adb6217ba5e6013ae34d5af10f3fff/.github/workflows/ci.yml
[web-test]: https://github.com/theturtlecsz/omp-webui/blob/08a277a542adb6217ba5e6013ae34d5af10f3fff/packages/daemon/test/model-commands.test.ts
[ecc-dynamic]: https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/skills/dynamic-workflow-mode/SKILL.md
[ecc-profiles]: https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/manifests/install-profiles.json
[eval-guidance]: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
[bilevel]: https://arxiv.org/html/2603.23420v2
