# 0001 Autonomous Execution, Default Recipe, and Sustained-Use Qualification Gate

This record documents accepted governance policies for default autonomous execution, operating recipes, and the 72-hour sustained-use qualification gate, incorporating owner interview decisions Q1–Q7.

## Status

- **Autonomy Policy (Q3)**: Accepted
- **Default Recipe Policy (Q4)**: Accepted
- **72-Hour Sustained-Use Qualification Gate (Q5)**: Accepted
- **Responsiveness Baseline and Performance Targets (Q6)**: Accepted
- **Workload Counting, Coverage, and Evidence Rules (Q7)**: Accepted

## Context

During owner interview rounds 1–3, the owner established operating policies, execution defaults, and qualification standards for Oh My Pi (OMP):
- **Ambition Scope vs. Operating Budget (Q2)**: "No limits" describes the ambition and capability scope of the platform, not an authorization for unlimited operating expenditure. Economical operating defaults remain fully aligned with platform ambition.
- **Canonical Programme Specification and Native Authority**: [MASTER.md](/home/thetu/oh-my-pi/MASTER.md) is the canonical programme specification and coordination status index. `WorkService` retains native authority and work state. Current operational status and goal binding remain in [MASTER.md](/home/thetu/oh-my-pi/MASTER.md) alone.
- **Preserved Plans and Gates**: Both full original programme plans—the ECC-centered implementation plan (WP1–WP8) and the Full Autoresearch implementation plan (R00–R19 and M0–M5)—along with all existing qualification gates (including the mandatory 20 consecutive ordinary-work trials) remain in effect and unchanged.
- **Qualification Contract vs. Acceptance Evidence**: This record defines the qualification contract and governance policy. It records accepted policy decisions; it does not claim that qualification runs have passed, that platform installation is complete, or that native work acceptance has occurred. All REQUIRED independent audit, effective audit model, and native acceptance obligations remain in full effect.

## Decision

### 1. Default to Autonomous Execution within Admitted Objective and Grants (Q3)

OMP defaults to autonomous execution within its admitted objective and granted authorities.
- **Autonomous Lifecycle**: The system plans, executes, investigates failures, recovers, and reports without routine confirmation when existing authority covers the work.
- **Proposal vs. Initiation Boundary**: The system may automatically identify and propose follow-up campaigns. Starting them requires either an applicable standing policy or admission through the existing workflow.
- **Standing Policy Contract**: A standing policy must explicitly specify scope, resource budgets, allowed effects, and renewal conditions.
- **Authority Boundary**: Operates strictly within existing grants; does not introduce new authority bypasses or unadmitted execution paths.

### 2. Dependable and Economical Execution as Ordinary-Work Default (Q4)

Ordinary-work execution applies this strict decision order:
1. Preserve quality, correctness, and required acceptance.
2. Minimize unplanned human intervention and workflow repair.
3. Meet the task's responsiveness or completion-time requirement.
4. Minimize total resource consumption among qualifying options.

Operating recipes and resource accounting:
- **Default Recipe**: Choose an economical profile (exemplified by Option A: half model usage, twice elapsed time) by default when its additional elapsed time remains acceptable.
- **Fast Recipe**: Choose a fast profile (exemplified by Option B: half elapsed time, twice model usage) through the Fast recipe when latency or a deadline justifies the extra usage.
- **Recipe Nuance**: Option A and Option B are illustrative examples rather than fixed global recipes. Option A reduces model tokens in the hypothetical tradeoff, but does not necessarily minimize total compute or supporting resources; the 4-step priority hierarchy governs selection among qualifying options based on total resource consumption.
- **Full Resource Accounting**: Measure all supporting activity, retries, and compute—not model tokens alone.

### 3. 72-Hour Sustained-Use Qualification Gate (Q5)

An initial sustained-use qualification gate is established with the following parameters:
- **Duration**: 72 continuous hours on one frozen release and configuration baseline.
- **Workload**: At least 30 ordinary interactive tasks and 100 experiment execution attempts, with a preregistered representative mix.
  - *Starting Parameters*: Workload counts are proposed starting parameters, not statistically established thresholds. Representative workload mix and user-facing performance objectives matter alongside duration (owner referenced Google SRE guidance; no specific source or validation of numeric thresholds was supplied).
- **Concurrent Operation**: Interactive work overlaps research execution and resource contention.
- **Recovery Coverage**: Exercise coordinator restart, worker loss, and a committed operation whose response is lost. Planned recovery from injected faults is distinct from runtime failure.
- **Cancellation Coverage**: Exercise queued, running, and late-returning work.
- **Integrity Criteria**: Zero lost accepted records, duplicate effects, unauthorized admissions, or false acceptance. Incomplete, blocked, or cancelled outcomes must never be treated as successes.
- **Operator Burden**: Zero unplanned workflow repair (manual or automated). Ordinary candidate debugging remains allowed. Planned fault injection and correctly handled failed experiments do not fail the run. Repairing grants, patching the runtime, or manually reconstructing state to continue fails the run.
- **Corrective Releases**: A corrective release requires a new qualification run; it does not automatically authorize starting one.
- **Responsiveness**: Meet predefined interactive latency and cancellation targets, measured against a matched baseline.
- **Hardware Coverage**: Exercise CPU and GPU backends being qualified; unavailable GPU coverage remains explicitly unqualified.
- **Preserved Mandatory Gate**: The existing 20 consecutive ordinary-work trials gate remains a separate mandatory gate.

### 4. Responsiveness Baseline and User-Facing Performance Targets (Q6)

Empirical baseline measurement precedes target setting:
- **Baseline First**: Measure the qualified installation under ordinary and mixed research workloads before freezing targets. A poor baseline triggers system improvement, not justification for relaxed responsiveness targets.
- **Absolute and Slowdown Limits**: Define both absolute user-facing limits and maximum allowable slowdown under research load for each dimension:
  1. *Command acknowledgement*: UI responds promptly and confirms receipt.
  2. *Useful progress*: Observable work advances; repeated heartbeats alone do not count.
  3. *Cancellation admission*: Cancellation is recorded and prevents further job admissions.
  4. *Confirmed termination*: Affected processes and device work have actually stopped, with backend-specific evidence.
- **Measurement Metrics**: Record p50, p95, maximum latency, and sample counts under ordinary and mixed research workloads.
- **Provider Wait Isolation**: Measure model/provider waiting times separately so provider latency cannot conceal an unresponsive interface.
- **Pre-Run Freeze**: Freeze all absolute limits, slowdown limits, workload compositions, measurement methods, and permitted recovery windows before the 72-hour qualification run. Both absolute limits and slowdown ratios must pass.

### 5. Workload Counting, Stratified Coverage, and Shared Evidence (Q7)

Counting, coverage, and evidence rules are structured as follows:
- **30 Ordinary-Work Tasks**:
  - Distinct, preregistered task instances spanning `oh-my-pi` and `media-discovery`, covering multiple task families.
  - Counted once per task instance: retries, review fixes, and resumptions remain part of that single task.
  - Outcomes reported separately: accepted, failed, blocked, and canceled outcomes must be reported distinctly.
- **100 Research Execution Attempts**:
  - Distinct experiment execution attempts executing declared experiments through applicable qualified backends. Generic research jobs (such as literature retrieval, synthesis, planning, or evaluation) cannot fill this denominator.
  - Genuine retries of an execution count toward the 100-attempt tally; polling, duplicate delivery, and reconciliation of the same execution do not.
  - Queued-but-never-started jobs remain recorded separately and do not count toward attempts.
- **Stratified Coverage Requirements**:
  - Minimum coverage by repository, task family, independent campaign, and required recovery/cancellation scenario must be preregistered and frozen before qualification.
  - Reaching 100 attempts cannot compensate for missing coverage in any required stratum.
  - Evidence retention: All failures and retries must be retained with their full resource consumption. Unsuccessful cases may not be swapped for easier tasks after the run starts.
- **Shared Raw Evidence Rules (with 20-Trial Gate)**:
  - Raw execution evidence may be shared between the 72-hour qualification run and the separate 20 consecutive ordinary-work trials gate ONLY when release, configuration, evaluator, workload conditions, and acceptance criteria match its protocol exactly.
  - The consecutive-success requirement of the 20-trial gate must be preserved; consecutive successes cannot be assembled by selecting successes around intervening failures.
  - Shared evidence can satisfy compatible requirements in both gates, but represents a single observation rather than two independent observations.

### 6. Implementation Preregistration and Acceptance Governance

- **Settled Contract**: All seven interview questions (Q1–Q7) are fully settled.
- **Preregistration Scope**: Specific numeric responsiveness targets (Q6) and numeric coverage allocations per stratum (Q7) constitute routine preregistration work rather than open interview questions. These values must be defined and frozen through existing acceptance routes prior to qualification gate execution, without inventing unverified numbers or requiring additional blanket owner interviews.
- **Retained Governance and Audit Obligations**: Qualified release status applies strictly to declared and exercised scope. All REQUIRED independent audit, effective audit model, and native `WorkService` acceptance obligations remain mandatory. No new authority or bypass is created.

## Rationale and Tradeoffs

- **Autonomy with Bounded Initiation (Q3)**: Autonomous execution within admitted grants eliminates unnecessary operator latency, while requiring standing policies or workflow admission for follow-up campaigns preserves explicit scope and resource governance.
- **Dependability and Total Resource Accounting (Q4)**: Prioritizing minimal workflow repair over raw speed prevents fragile shortcuts that incur costly operator interventions. While Option A illustrates reducing model tokens in hypothetical tradeoffs, total resource consumption across all supporting tasks, retries, and compute governs selection. The Fast recipe remains available under existing authority when deadlines or latency requirements justify higher resource consumption.
- **Sustained Verification Scope and Empirical Evidence (Q5)**: The 72-hour qualification run evaluates operational integrity, concurrency handling, and recovery under sustained stress on declared release scope. A bounded run provides empirical evidence of stability under exercised conditions; it cannot guarantee that all latent runtime instability is caught.
- **Baseline-Driven Responsiveness (Q6)**: Measuring baseline performance prior to target freezing prevents arbitrary threshold selection and ensures responsiveness under load is rigorously bounded. Decoupling provider wait times exposes local UI and engine latency bottlenecks.
- **Rigorous Counting and Evidence Integrity (Q7)**: Restricting the 100-attempt gate strictly to experiment execution attempts prevents inflating qualification counts with generic retrieval or planning work. Enforcing stratified coverage and retaining all failure evidence prevents survivor bias and task-swapping. Preserving strict protocol matching and consecutive run requirements for shared evidence prevents false qualification without duplicating test burdens.
