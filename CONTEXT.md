# OMP Autonomous Execution and Autoresearch

Shared domain vocabulary for autonomous work execution, experiment lifecycle, and qualification governance within Oh My Pi.

## Language

### Governance and Objectives

**Admitted Objective**:
A formally authorized goal and scope boundary admitted into the execution system by an authoritative owner or standing policy.
_Avoid_: Prompt goal, open request, unadmitted task

**Standing Policy**:
A persistent, pre-authorized governance rule specifying scope, resource budgets, permitted effects, and renewal conditions for recurring or follow-up autonomous operations.
_Avoid_: Blanket waiver, unconstrained autonomy, permanent bypass

**Campaign**:
A sustained, goal-directed sequence of research activities pursuing an admitted research objective under explicit resource boundaries and governance policy.
_Avoid_: Epic, unbounded exploration, perpetual goal

### Work Execution

**Ordinary-Work Task**:
A routine, bounded engineering, coordination, or operational activity executed within existing authority that prioritizes correctness, low operator burden, and resource efficiency.
_Avoid_: Background daemon, unbounded script

**Research Job**:
A bounded unit of research work admitted under a campaign—such as literature retrieval, synthesis, planning, or evaluation—distinct from an actual experiment execution attempt.
_Avoid_: Unbounded research session, ad-hoc probe

**Experiment Execution Attempt**:
The actual execution of a declared experiment through an applicable qualified backend to evaluate an empirical hypothesis, distinct from generic planning, retrieval, synthesis, or evaluation jobs.
_Avoid_: Generic research job, literature query, planning task

**Candidate Debugging**:
Ordinary human or agent diagnosis and correction of candidate materials rather than authority grants or runtime code; changed candidate bytes require a new frozen identity and cannot retain prior evidence binding.
_Avoid_: Workflow repair, runtime hotpatching

**Planned Fault Recovery**:
The pre-orchestrated recovery from injected system or hardware faults according to qualified recovery procedures, distinct from unplanned workflow repair.
_Avoid_: Workflow repair, manual rescue

**Experiment Failure**:
The abnormal termination or invalid execution of an experimental attempt caused by candidate defects or execution faults, distinct from a valid negative scientific outcome.
_Avoid_: Disproved hypothesis, refuted theory

**Negative Scientific Outcome**:
A valid experimental result where empirical evidence contradicts the hypothesis or demonstrates no detected effect, satisfying measurement validity without system or candidate fault.
_Avoid_: Experiment failure, crashed run, broken trial

**Model-Generation Failure**:
The failure of a model to produce schema-compliant, executable, or syntactically valid candidate material during a research attempt, treated as a candidate flaw rather than infrastructure breakdown.
_Avoid_: Runtime crash, platform outage

**Research Infrastructure Failure**:
The abnormal interruption or termination of an attempt caused by environmental, hardware, or host system breakdown rather than candidate flaws or empirical findings.
_Avoid_: Hypothesis refutation, negative result

**Workflow Repair**:
Unplanned manual or automated intervention required to patch runtime code, reconstruct invalid state, or repair authority grants to allow stalled execution to continue.
_Avoid_: Routine candidate debugging, planned fault recovery, recovery under the frozen qualified policy

### Qualification and Assurance

**Qualification Run**:
A continuous evaluation period executing representative workloads on a frozen software baseline to measure operational integrity, recovery under planned faults, and performance against required gates.
_Avoid_: Ad-hoc test pass, unversioned benchmark

**Trusted Receipt**:
An immutable, signed or system-attested record capturing evaluator observations, execution validity, check results, and content digests, establishing provenance and evidence without constituting work completion or acceptance.
_Avoid_: Acceptance certificate, completion receipt, proof of work

**Qualified Release**:
An immutable package of identified artifacts qualified for their declared scope through applicable verification suites and retained native acceptance routes, without implying universal or all-platform approval.
_Avoid_: Universal release, general certification, blanket approval


### Programme and Milestone Governance

**Programme**:
A multi-phase technical initiative encompassing strategic objectives, canonical specifications, governing policies, and sustained delivery across repositories and releases, transcending any single execution cycle.
_Avoid_: Project, epic, sprint, milestone, campaign

**Milestone**:
A bounded delivery increment within a programme, comprising a closed subset of work packages, explicit prerequisites, observable contracts, and verifiable deliverables.
_Avoid_: Phase, sprint, goal, programme, task

### Scope and Authority Boundaries

**Owner Preference Scope**:
A distinct configuration tier capturing personal operating styles and thresholds that follow the owner across sessions without mutating repository rules or shared project defaults.
_Avoid_: Repository policy, project standard, global override, team rule

**Repository Rule Scope**:
Project-level constraints, conventions, authority definitions, and operational invariants governing all automated and manual execution within a repository, independent of individual user preferences.
_Avoid_: User preference, personal configuration, client option, local alias

### Lifecycle and State Transitions

**Work Pause**:
A temporary operational suspension that stops new admissions and new starts from queued work, with checkpointing for supported jobs and declared grace periods. It is distinct from immediate cancellation.
_Avoid_: Immediate cancellation, task abort, termination, goal blocked, failed run

**Immediate Cancellation**:
A request and state transition that fences admissions and stale effects while requesting prompt execution stop across local and distributed workers. Confirmed physical termination requires backend evidence and bounded observation windows rather than assuming instantaneous termination.
_Avoid_: Work pause, graceful drain, soft stop, checkpoint suspension

### Research Methodology and Architecture

**Bilevel Autoresearch**:
An optimization hierarchy where an outer research loop explores and evaluates executable search logic or harness behaviors to guide inner-loop task candidate generation and evaluation.
_Avoid_: Hyperparameter tuning, prompt engineering, recursive research, meta-prompting

**Recursive Research**:
Research directed at the research methodology and outer-loop machinery itself, evaluating meta-proposals, synthesis, and critique utility against empirical downstream task benefit under separate evaluators and resource limits.
_Avoid_: Bilevel autoresearch, unbounded recursion, self-reflection, prompt tweaking

### Research Artifacts and Verification

**Reproduction Bundle**:
A content-addressed reproduction package retaining required bytes or governed durable access for permitted reproduction, including exact code versions, environment manifests, dependency closures, seeds, receipts, and instructions. It does not provide unconditional private dataset export.
_Avoid_: Run folder, checkpoint archive, build artifact, summary report, log dump

**Evidence Access Depth**:
The explicitly classified degree of inspection applied to a research source or claim, strictly distinguishing abstract-only metadata indexing from full-text analysis, code inspection, or empirical execution.
_Avoid_: Citation count, reading status, search hit, source ranking

### Capacity and Environment

**Campaign Reservation**:
A bounded commitment of compute, token quota, wall-clock time, and worker capacity committed to an admitted research campaign. It does not constitute an exclusive resource that guarantees zero contention.
_Avoid_: Global allowance, open budget, infinite quota, standing allowance

**Recurring Allowance**:
A periodically replenished baseline allocation of compute and model resources approved under standing policy covering approved programme scope, including research, across defined accounting intervals.
_Avoid_: Campaign reservation, one-off grant, emergency budget, unbounded pool

**Connected Backend**:
A physical or virtual instrument, compute cluster, or execution runtime that is actively connected and reachable. Qualification is separate and required before production use.
_Avoid_: Mock backend, declared interface, unsupported device, stub runner
