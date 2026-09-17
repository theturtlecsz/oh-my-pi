# Oh My Pi: Programme Implementation Goal and Operational Contract


## Authorized external development lane — September 16, 2026

The owner explicitly authorizes Codex development outside OMP for the full programme. Native execution admission, a live OMP grant, and an owner OMP host session are not prerequisites for development in this lane. This authorization supersedes earlier instructions or checkpoint conclusions requiring those prerequisites for development; it does not change native production authority or remove programme scope.

- MASTER.md remains the sole authoritative programme status record. Track each bounded milestone there with objective, existing owner/work ID and last observed revision, preserved criteria, dependencies, candidate identity, evidence links and status. Search existing work first; no competing backlog or authority database. If no native identity exists, explicitly record pending native reconciliation rather than inventing one.
- Use a separate development checkout. Preserve the installed checkout and live native records. Reference existing native ownership and criteria; record proposed additions separately as pending native reconciliation. For OMP-233, preserve all six existing criteria while engineering the proposed recovery prerequisites.
- Retain exact economy roles, repository instructions, Task Observer startup, bounded packets, resource accounting, deterministic checks, frozen candidate/evidence and fresh independent review. External development is not permission to change model obligations or let candidate workers issue acceptance receipts.
- Exercise authority changes only against disposable test state during development. No live grant changes, direct authority-store edits, deployment, authority migration, broadened data access or fabricated receipts are authorized by this lane. Q36 remains separately unapproved.
- Label reviewed development accurately: externally verified, awaiting native reconciliation and installation qualification. External review is not native acceptance or proof of installed behavior.
- Before deployment or native completion, reconcile scope and evidence through supported native interfaces, read back the applicable records, bind immutable candidate/diff/test/review evidence, and satisfy existing authorization and qualification gates. Preserve cumulative attempts, no-progress history, historical work and acceptance criteria; never reset enforcement during reconciliation.

Implementation remains resumed. Continue dependency-ready external development without repeating the native-host access request. First development milestone is OMP-233 recovery prerequisites, preserving its existing criteria and tracking proposed additions separately. Complete ECC WP1–WP8, autoresearch R00–R19, Fleet/two-repository work, deferred UI, stabilization and all complete-system qualification obligations remain required; “first” changes delivery order only.


## 1. Canonical Status, Snapshot Appendices, and Operational Invariants

`MASTER.md` is the canonical programme specification and the sole authoritative coordination status record for the Oh My Pi (OMP) programme. `WorkService` is the sole native authority governing native work scope, work item identities, execution grants, dependency resolution, cancellation, evidence collection, and formal completion. `MASTER.md` mirrors native receipts and never replaces them. Snapshot appendices (`docs/programme/ECC-IMPLEMENTATION-SPEC.md` and `docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md`) are required full exports of canonical `MASTER.md`, not independent authoritative status records.

Completing documentation does not itself resume implementation or approve Q36. Preserve existing session authorization, valid native scope, and explicit pause/resume instructions. Preparing this handoff is documentation work; execute the implementation goal under applicable session authorization without adding a blanket resume requirement when work is already authorized.

---

## 2. Core Sequencing Principle: "First" Means Delivery Order

Across all thirty-five accepted product decisions (Q1–Q35) and all work packages (WP1–WP8, R00–R19), the term **"first" strictly defines delivery and sequencing order**. It never removes, depreciates, or permanently narrows any capability from the complete intended ECC foundation or autoresearch system. Subsequent capabilities remain active, mandatory commitments in the programme backlog.

---

## 3. Complete Intended Architecture and System Scope

The intended Oh My Pi system is a unified, self-improving, autonomous software engineering and scientific autoresearch platform operating across `oh-my-pi` and `media-discovery`:

### A. Complete ECC Foundation (WP1–WP8)
- Pinned upstream mirror of ECC at commit `8321021c54d670126ce3b2969d5deb880b4b0c2a`.
- Rechecked approved baseline with classified catalogs: Engineering, Domain, Maintenance, and Research packs.
- Deterministic, small-footprint OMP adapter isolating upstream assets from native runtime state.
- Source and transformed content hashing, dependency closure verification, and exact provenance tracking.
- Ownership-aware asset preview, install, validate, update, remove, and rollback operations.
- Typed audits, full resource accounting, read-only qualified database advisors (WP6), and lifecycle management.
- Qualified runtime assets are treated as immutable releases; mirroring upstream content never implies automated activation.

### B. Multi-Domain Autoresearch Platform (R00–R19)
- **Engineering Harness Research**: Managed experiment mode, reproducible test fixtures, mutation testing, and candidate workspace isolation across both repositories.
- **Literature and Evidence Synthesis**: Source discovery, citation-family tracking, claim-evidence matrix construction, contradiction search, and synthesis distinguishing full-text analysis from abstract-only indexing.
- **Machine Learning and Simulation**: Data split integrity checks, data leakage detection, CPU/GPU training manifests, checkpoint resumption, metric adapters, and compatible upstream optimization tooling where applicable.
- **GPU Acceleration and Connected Backends**: Qualified local/remote CPU and GPU execution runtimes. A connected backend is connected; qualification is separate and required before production use. External instruments are supported without falsely claiming hardware connectivity.
- **Continuous and Distributed Research Programmes**: Durable campaigns, question queues, standing-policy renewals, heterogeneous worker pools, fairness guarantees, and coordinator failover resilience.
- **Bilevel Executable Search Mechanisms**: Executable search logic generated, isolated behind a strict data protocol, and evaluated against frozen baselines.
- **Broader Harness and Runtime Candidates**: Generation, review, and qualification of runtime candidates for OMP itself, requiring independent audit and schema-safe migration/rollback.
- **Recursive Researcher**: Meta-research on research workflows under separate parent objectives, explicit recursion depth bounds, and downstream empirical benefit verification.

### C. Fleet Knowledge and Two-Repository Programme
- Full retention of the two-repository programme (`oh-my-pi` and `media-discovery`), Cognee, Enola, and RTX 5090 Fleet Knowledge.
- The delivery and setup of Fleet Knowledge must not block the delivery and execution of the first ML workflow (embedding/reranking quality and inference efficiency using available permitted datasets and infrastructure).

### D. Capability Implementation vs. Measured 10x Improvement
- Distinguish implementing full system capability from claiming measured 10x productivity improvements.
- Productivity claims must be empirically measured against matched-resource controls with complete denominators and all failures retained. Unsupported multiplier claims are prohibited.

---

## 4. Execution Policy and Economy Model Routes

All execution adheres to `/home/thetu/.codex/workflows/economy/POLICY.md` and external model configurations in `MODELS.md`.

| Role | CLI | Exact model selection |
| :--- | :--- | :--- |
| Coordinator | Codex | `gpt-6-astra`, effort `low` |
| Planner | Claude CLI | `claude --model claude-fable-5-1` |
| economy-worker | agy CLI | `agy --model gemini-3.8-flash-high` |
| economy-reviewer | Kimi CLI | `kimi --model kimi-code/k3-256k` |

- External CLI roles execute via installed CLIs; native Codex subagents cannot select these providers and must never be substituted.
- Kimi reviewer uses `kimi-code/k3-256k` (`max_context_size = 262144`); do not use the 1M variant.
- Gemini high effort is embedded in the model ID. No silent provider, model, tier, or effort substitutions.
- Premium consultation requires a named unresolved question, concrete evidence, explicitly configured reserve, and bounded envelopes. The current profile has no premium reserve.
- Concurrency is limited to at most two implementation writers concurrently; no nested tournaments.
- Bounded work packets define objective, contract, base revision, allowed paths, required context, trusted checks, resource envelope, and artifact destination.
- Deterministic checks run before semantic review. Diffs and exits must be frozen; changed bytes invalidate old evidence.
- At most one evidence-driven repair round before reassessing scope; after two unsuccessful repair rounds, checkpoint and diagnose without spawning replacement agents.
- Honest accounting tracks parent, workers, planner, reviewer, helpers, rejected attempts, preflight checks, and mechanism research, separating measured usage, estimated cost, observed cash, human intervention, and unknown/unpriced items (unknown is never zero). Distinct subscription allowances are tracked. `usage-record.json` is supporting evidence, not an authoritative budget ledger.

---

## 5. Authority, Native Interfaces, and Lifecycle Governance

- `WorkService` is the sole native authority governing work scope, identities, grants, cancellation, evidence, and completion.
- Preserve native `/intake`, `/plan`, `/execute`, `/summary`, and `/done` ownership through existing interfaces.
- Mandatory Task Observer startup must be verified; the selected durable memory backend must be maintained without silent switching. Narrative memory and UI projections cannot authorize work.
- Separation of powers decouples candidate generation, selection, and acceptance. Candidates cannot control acceptance criteria, though appropriate read-only criteria access is permitted. Candidate workers must not possess private evaluator credentials or control protected scoring or receipt issuance. Evaluators issue immutable, content-addressed receipts.
- Work Pause halts new task admissions and unstarted queued tasks, checkpoints supported jobs, and observes declared grace periods under the applicable lifecycle contract.
- Immediate Cancellation is a request and state transition that fences admissions and stale effects and requests prompt stopping. Confirmed physical termination requires backend evidence and bounded windows rather than assuming instantaneous termination.

---

## 6. Traceability Inventory: The Ten Original Goal Obligations

### O1 — Preserve the Existing Programme
Read stored goal and `MASTER.md` before making changes; extend rather than replace. `MASTER.md` is the canonical programme specification and sole authoritative coordination status record mirroring native receipts; `WorkService` is native work authority. Preserve scope, backlog, historical evidence, unfinished obligations, historical IDs, and acceptance criteria. Retain Work Ledger, native execution, stabilization, recovery, qualified installation/deployment, Cognee, Enola, RTX 5090 Fleet Knowledge, and two-repository learning across `oh-my-pi` and `media-discovery`. Defer WebUI/Paseo until prerequisites are met. Retain ECC WP1–WP8 and Full Autoresearch R00–R19. Snapshot appendices are required full exports of canonical `MASTER.md`, not independent authoritative status records. Historical plans, code, checkmarks, or CI runs do not prove current installation or acceptance. Progress through bounded milestones; never equate one milestone to goal completion. Preserve current pause/resume context; completing documentation does not resume implementation or approve Q36.

### O2 — Repository and Economy Contracts
Read current `AGENTS.md`, mandatory Antidote instructions, Task Observer session-start, `/home/thetu/.codex/workflows/economy/POLICY.md`, and `MODELS.md`. Exact external roles: coordination code/Astra low; planning Fable 5.1 Claude CLI; mechanical/stateful implementation Gemini 3.8 Flash high agy CLI economy-worker; fresh independent Kimi K3 256K Kimi CLI economy-reviewer. Resolve exact identifiers from `MODELS.md`; no silent provider, model, effort, native-subagent, or premium substitution. No premium without configured reserve. One bounded milestone, at most two implementation writers, no nested tournaments, only concrete authorized delegation, no status relaying.

### O3 — Current Reality and Ownership
Start R00 plus installed-baseline/ownership WP1 only, reusing verified completed work. Inspect actual installed CLI tools, extensions, background services, database schemas, auditors, and instructions. Distinguish development boundaries from qualified releases. Map effective roles, effort levels, fallbacks, tools, advisors, memory stores, discovery mechanisms, and optional hooks. Verify native work descriptions, revisions, criteria, grants, dependencies, and receipts. Explicitly inspect shared job, trusted execution, evaluation, artifact storage, and deployment interfaces across ECC, stabilization, exploration, accounting, Fleet, and UI owners. Distinguish source, configured, discovered, enabled, invoked, tested, qualified, and accepted. Treat missing access as unknown. Search existing work before creating new items; preserve IDs and criteria; read back native records before binding execution; never edit authority stores directly. Advance milestone only after baseline evidence is verified.

### O4 — Complete ECC Foundation
Pinned upstream mirror at commit `8321021c54d670126ce3b2969d5deb880b4b0c2a`. Recheck approved baseline. Prefer compatible upstream unchanged, then minimal metadata/path/tool adaptation, then reviewed semantic overlays. Record exact exclusions and deviations. Reuse upstream profiles, modules, components, planning routines, link rewriting, ownership models, lifecycle patterns, and existing installer interfaces; do not build a parallel installer without concrete evidence of inability. Deliver complete mirror and classified catalogs (Engineering, Domain, Maintenance, Research). Build a small, deterministic OMP adapter. Enforce source/transformed content hashing and dependency closure. Provide ownership-aware preview, install, validate, remove, update, and rollback operations. Deliver typed audits, full accounting, and qualified read-only database advisors (WP6). Maintain existing-owner learning and lifecycle contracts. Provide separately qualified security diagnostics and production release evidence. Runtime assets are immutable qualified releases; mirroring never implies automated activation. Fulfill WP1–WP8 full contracts.

### O5 — Complete Research Platform
Execute R00–R19 dependencies and acceptance criteria. Deliver durable campaigns, hypotheses, search graphs, shared jobs, campaign reservations (bounded commitments, not exclusive zero-contention guarantees), content-addressed artifacts, verified sources, isolated execution sandboxes, independent evaluation, trusted receipts, native candidate mappings, and reproducible reports. Cover engineering and OMP harness research, literature synthesis (distinguishing full-text from abstract-only indexing), ML/simulation, GPU acceleration, and connected backends (qualification is separate and required before production use). Support adaptive branch search, comparative evaluation, hypothesis evolution, scoped learning, generated executable search mechanisms, and broader harness/runtime candidates. Support continuous programmes, distributed workers, CLI/WebUI monitoring, bilevel improvement, and recursive research on research itself. Deliver the first actual experiment-to-reproduction vertical slice before expanding policy counts: actual experiment, failed candidate, trusted evaluation, restart, cancellation, and reproduction bundle (retaining required bytes or governed durable access, not unconditional private dataset export). Reuse exploration-owned shared bounded jobs; do not create a competing research scheduler or authority database.

### O6 — Authority and Independence
`WorkService` is authoritative for scope, identity, grants, cancellation, evidence, and completion. Preserve native `/intake`, `/plan`, `/execute`, `/summary`, and `/done` ownership through existing interfaces. Enforce mandatory Task Observer startup. Retain selected durable memory backend. Enforce required independent audit with an effective audit model. Maintain immutable candidate and evidence bindings and strict command gates. Preserve separation of generation, selection, and acceptance. Preserve intake valid-pool and owner-presentation semantics. Agent keep-metric fields, narrative memory, UI state, and heuristic scores cannot authorize work or approve completion. Candidate workers cannot control acceptance criteria, though appropriate read-only criteria access is permitted; candidate workers must not possess private evaluator credentials or control protected scoring or receipt issuance. Isolate development checkouts from qualified production installations. No deployment, authority migration, data broadening, or model changes without existing applicable authorization.

### O7 — Bounded Execution and Review
Persist a complete work packet before execution: objective, observable contract, native work ID and revision, base git revision, allowed file paths, ownership rules, required context, trusted checks, acceptance routes, resource envelopes, artifact destinations, and remaining dependencies. Retain full logs in artifacts; maintain current status and evidence links in `MASTER.md`. Workers must complete only with changed files, unified diffs, actual terminal exits, verified evidence, and remaining requirements. Execute deterministic checks before semantic review. Freeze candidate diffs and evidence prior to review; changed bytes invalidate prior evidence. Allow one evidence-driven repair before reassessing scope; after two unsuccessful repair rounds, checkpoint and diagnose. Do not reset budgets by spawning replacement agents.

### O8 — Honest Accounting
Count all resource consumption: parent, workers, planning, review, helpers, discarded candidates, selection, preflight, retries, repairs, retrieval, summaries, experiments, and mechanism research. Distinguish measured usage, estimated prices, observed cash, human intervention, and unknown/unpriced items (unknown is never zero). Distinctly track subscription allowance consumption under the economy policy. Treat `usage-record.json` as supporting evidence, not as an authoritative ledger. Recurring allowance covers approved programme scope including research, not just maintenance.

### O9 — Qualification at Real Boundaries
Validate through existing infrastructure and contract-level tests; use real process and service tests where mocks are insufficient. Exercise negative cases: wrong candidate identity, forged metrics, missing evidence, cancellation races, stale workers, lost responses, resource races, artifact-path escapes, source injection, data leakage, version incompatibility, and rollback. Preserve installed-identity, recovery, and acceptance-negative stabilization routes. Preserve the mandatory 20 consecutive accepted ordinary-work trials gate without unplanned workflow repair, maintaining a frozen role and routing recipe throughout the streak.

Preserve the accepted 72-hour sustained-use qualification gate on a frozen release baseline: at least 30 ordinary interactive tasks across `oh-my-pi` and `media-discovery`, and 100 experiment execution attempts with a preregistered representative mix. Maintain concurrent interactive and research execution under resource contention. Require zero lost accepted records, zero duplicate external effects, zero unauthorized admissions, zero false acceptances, and zero unplanned workflow repairs. Candidate debugging and planned fault recoveries are tracked separately. Freeze absolute responsiveness limits and maximum slowdown under research load for command acknowledgement, useful progress, cancellation admission and confirmed termination. Record p50, p95, maximum latency and sample counts; separate provider wait from local responsiveness. Exercise CPU/GPU backends being qualified; unavailable GPU coverage remains unqualified. Preregister coverage by repository, task family, independent campaign and recovery/cancellation scenario; retain all failures and consumption. Count ordinary task instances once across retries; count genuine experiment execution retries, not polling, duplicate delivery or reconciliation. Queued-never-started jobs are separate. Share raw evidence with the20-trial gate only when release, configuration, evaluator, workload and criteria protocols match; never select successes around failures or treat shared evidence as independent twice. A poor baseline requires improvement, not weakened thresholds.

### O10 — Checkpoint and Full Completion
At every checkpoint return: exact source and release identities, changed contracts and files, actual test commands and exits, independent review findings, unresolved compatibility or authority gaps, measured usage, and the next executable work item. Never fabricate receipts or label proposed content as installed. Routine authorized work continues without repeated per-asset permission. Genuine changes to policy, authority, models, deployment, or data scope require a concrete proposal and evidence before seeking an explicit owner decision. On pause, persist the milestone packet and stop workers; no polling, reminders, or misuse of goal complete/blocked. Full completion requires that every retained original core, Fleet, UI, and qualification obligation AND the full research §28 complete-system criteria pass real native acceptance in `WorkService`. Partial delivery remains partial; no unbounded autonomous replacement goals.

---

## 7. Accepted Programme-Wide Product Directions (Q1–Q35)

| Question | Accepted decision |
| :--- | :--- |
| Q1 — Users | Single owner initially. Design identities and permissions for collaborators from the beginning. |
| Q2 — Experience | Conversation for direction, durable queue for execution, dashboard for oversight—all reflecting the same state. |
| Q3 — First engineering campaign | A bounded media-discovery problem with permitted fixtures, followed by OMP harness research. |
| Q4 — Literature | Start with technical decisions that repository experiments can test. Broader research remains in scope. |
| Q5 — ML/GPU | Embedding/reranking quality and inference efficiency. Use available datasets and infrastructure; Fleet Knowledge’s delivery should not block this first workflow. |
| Q6 — Domain depth | One dependable, reproducible workflow per domain first, then broader configuration. |
| Q7 — ECC installation | Task-oriented packs with exact previews and ownership protection. Existing installation authorization should cover routine execution without repeated per-asset confirmations. |
| Q8 — ECC visibility | Integrate methods into native commands. Make provenance and effective instructions inspectable; name specialized workflows explicitly. |
| Q9 — Advisor | Database/data systems first, with the read-only scope and accounting already specified in WP6. |
| Q10 — Updates | Monthly candidate preparation after qualification, plus relevant fixes. Activation follows the release process. |
| Q11 — Customizations | Preserve owned local behavior; present conflicts and the smallest compatible adaptation. |
| Q12 — Knowledge transfer | General engineering lessons by default, subject to permissions. Domain knowledge and procedures need demonstrated applicability. |
| Q13 — Preferences | Personal preferences follow the owner through a distinct scope. They do not silently become repository rules. |
| Q14 — Corrections | Correct retrieval and identify affected decisions. Perform consequential rechecks within existing authority; otherwise propose them. |
| Q15 — Memory visibility | A compact explanation when memory materially influences a decision, with expandable evidence and contradictions. |
| Q16 — Degradation | Continue without optional services. Preserve mandatory Observer/instruction requirements and never silently switch durable memory backends. |
| Q17 — Triggers | Schedules and regressions first. Material new evidence should also trigger work where standing policy covers it. Backlog opportunities initially generate proposals. |
| Q18 — Renewal | Calendar and resource limits. Renew automatically when standing policy permits; renewal must not reset cumulative consumption or attempt limits. |
| Q19 — Pause | Stop new admissions and new starts from already queued work. Checkpoint supported jobs; allow declared grace periods. Keep immediate cancellation separate. |
| Q20 — Discoveries | Preserve out-of-scope discoveries and propose successor campaigns. Changing methods within the existing objective should remain autonomous. |
| Q21 — Search ambition | Efficient conclusions with deliberate allowance for alternatives, counterexamples, and replication. Deep campaigns allocate more exploration. |
| Q22 — Inconclusive results | Conclude honestly with the unresolved question and costed next experiment. Continue automatically only under applicable standing policy. |
| Q23 — Research data | Authorized repository code and permitted public/synthetic data first. Explicitly classify additional private data; access alone does not establish permission for every research use. |
| Q24 — Model exposure | Decide by data class and existing provider permissions. Enforce local-only processing where required. |
| Q25 — Literature access | Open sources plus approved licensed collections. Clearly distinguish full-text review from abstract-only access. |
| Q26 — Instruments | Prioritize an actual use case and available backend. Otherwise deliver the interface without claiming connected hardware. |
| Q27 — Retention | Preserve acceptance evidence, required reproduction material, and useful failure records. Reclaim temporary material through policy; hashes alone cannot replace required evidence bytes. |
| Q28 — Competition | Protect ordinary-work responsiveness while providing research a minimum share when capacity permits. Record contention in comparisons. |
| Q29 — Allowance | Recurring allowance for approved program scope, with campaign reservations. Propose concrete resource limits after baseline measurement. |
| Q30 — Recursive research | A separate capped allocation, with downstream benefit measured. No silent borrowing from ordinary campaigns’ reservations. |
| Q31 — Unattended work | Run within authorized windows and qualified recovery boundaries. Queue nonurgent questions; pause affected work when a genuinely blocking decision arises. |
| Q32 — WebUI/Paseo | Monitoring and reliable inspect/pause/stop first, then authoring. Every client uses the same native semantics; verify compatibility before assuming it. |
| Q33 — Notifications | Interrupt for actionable blockers, integrity failures, unexpected exhaustion, and requested completions. Routine experimental failures and expected recovery go into the record/digest. |
| Q34 — Reports | Concise decision report linked to the complete technical dossier and reproduction bundle. Publication is another rendering. |
| Q35 — Rollback | Prepare standing authority for predefined rollback to a qualified, compatible release. If persisted state makes rollback unsafe, stop and diagnose rather than force it. |

---

## 8. Explicitly Withheld Authority Decision Q36 and Dependency Recovery Route

Decision Q36 is explicitly withheld. The 28 preserved claims share the workspace; their relevance to the affected grant, work, execution targets and possible effects remains unresolved. They are not yet established as either related or unrelated. Existing report snapshots are not revalidated by documentation.

Prerequisites for any future Q36 consideration must proceed in strict sequence:
1. **Operation Diagnostics**: Inspect original operation UUID, header, and contract diagnostics before repeating lookup at `/v1/operations/{operation_id}`. Diagnose 400 validation and potential 409 contract mismatches; diagnostic leads must not be assumed.
2. **Preserved Workspace Claims**: Account for the 28 claims sharing the workspace with unresolved relevance, ensuring no conflation with approved grants or completed work.
3. **Authoritative Quiescence**: Verify authoritative local AND remote quiescence across all background jobs, runtime processes, and external CLI sessions, not merely process scans.
4. **Cumulative Limit Enforcement**: Supported successors must enforce close-attempt and consecutive-no-progress history without resetting cumulative limits.
5. **Auditor and Judge Verification**: Verify effective `openai-codex/gpt-5.6-sol:medium` auditor execution and the required judge identity; do not redefine as merely obtaining another review.
6. **Failure and Reconciliation Tests**: Test edge cases including stop-success/admission-failure, committed admission lost responses, original-operation reconciliation without duplicate admission, preserved cumulative limits, and stale execution rejection.
7. **Exact Proposal Formulation**: Present an exact approval proposal specifying expected grant versions, candidate boundaries, operation sequences, irreversible stops, and failure reconciliation.

Ready-for-owner-review can precede authorization; never conflate ready vs approved. All prerequisites remain unresolved until concrete evidence is presented.

---

## 9. Implementation Milestones and First Vertical Slice

The first bounded milestone is **R00 + installed-baseline/ownership WP1 only**, not the entire M0 milestone. Full milestone progression follows the dependency table:

| Milestone | Required packages | User-visible outcome |
|---|---|---|
| M0: native foundation | R00–R04 plus applicable ECC/stabilization prerequisites | Reproducible source/runtime identity and durable research contracts/jobs |
| M1: real experiment system | R05–R07, initial R17 | OMP can run, evaluate, recover, and reproduce an engineering investigation |
| M2: full research workflow | R08–R12, R17; domain work can overlap | Literature, ML/simulation, adaptive search, comparative learning, useful reports |
| M3: self-improving research | R13–R14 with qualified evaluator and release interfaces | Generated search mechanisms and broader harness candidates receive empirical qualification |
| M4: continuous research service | R15–R16 and relevant R18 | Persistent distributed campaigns with CLI/WebUI control and complete accounting |
| M5: recursive improvement | R19 plus continuing R18 | Research methods themselves improve against fresh downstream evidence |

R18 begins with each milestone's applicable checks. R16 can deliver views incrementally as backend contracts stabilize. R08 and R09 overlap is permitted after shared contracts and runners exist.

The mandatory first vertical slice requires demonstrating:
1. An actual engineering experiment running in an isolated candidate workspace.
2. An actual candidate failure correctly detected and recorded.
3. A trusted evaluation producing an immutable receipt with deterministic metrics.
4. A clean coordinator restart and fault recovery during execution.
5. An immediate cancellation fencing admissions and stale effects, requesting prompt stop, with confirmed physical termination verified.
6. A reproduction bundle retaining required bytes or governed durable access for permitted reproduction (not unconditional private dataset export), verified in a clean secondary environment.

---

## 10. Complete System Verification Definition

The Oh My Pi programme is complete when and only when all retained original core, Fleet, UI, and qualification obligations (O1–O10) AND the full research §28 complete-system criteria in the canonical specification pass real native acceptance in `WorkService`. The 17 research criteria alone do not constitute the entire programme, and full completion requires satisfying the complete, exact §28 requirements without reduction or substitution.