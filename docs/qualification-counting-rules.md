# Qualification counting rules (OMP-620 r2)

Authority: [ADR 0001 §§3,5,6](adr/0001-autonomous-execution-and-qualification.md),
[stabilization S4.3–S4.4](omp-stabilization-plan.md#phase-4--run-ordinary-work-omp-249-qualification),
[ORIGINAL-OBLIGATIONS O9](programme/ORIGINAL-OBLIGATIONS.md#o9--qualification-at-real-boundaries),
and [SCOPE-PRESERVATION](programme/SCOPE-PRESERVATION.md).
Counts grant no qualification authority and cannot prove full O9 acceptance.
Real boundaries, negative cases, independent audits, native WorkService acceptance,
responsiveness, integrity, resource accounting and coverage remain mandatory.
This item starts no trial, run or freeze; introduces no persistence, command,
public schema or approval route. A read-only counter is separate work.

## Binding r2 rules and source citations

The following quotations reproduce ledger item **OMP-620 r2**, acceptance criteria
“Gate 1”, “Gate 2”, “Gate 3” and “Shared evidence”, supplied in the task amendment
of 2026-10-13. File/section citations identify the retained source rules for each
criterion; r2's explicit reset rule is quoted as r2, not attributed to wording absent
from those files.

### Gate 1 — 20 consecutive accepted trials

Sources: [docs/omp-stabilization-plan.md, Phase 4, S4.3–S4.4](omp-stabilization-plan.md#phase-4--run-ordinary-work-omp-249-qualification);
[docs/programme/ORIGINAL-OBLIGATIONS.md, O9](programme/ORIGINAL-OBLIGATIONS.md#o9--qualification-at-real-boundaries);
[docs/adr/0001-autonomous-execution-and-qualification.md, §5, Shared Raw Evidence Rules](adr/0001-autonomous-execution-and-qualification.md#5-workload-counting-stratified-coverage-and-shared-evidence-q7).

> Gate 1, kept separate: 20 consecutive accepted ordinary-work trials without unplanned workflow repair, on a frozen role and routing recipe throughout the streak (stabilization plan S4.3; ORIGINAL-OBLIGATIONS O9). Rules quoted from the sources: normal review and remediation is allowed, reconstructing grants is not; consecutive successes cannot be assembled by selecting successes around intervening failures; a failed trial becomes a reproducible regression before the acceptance streak restarts. Reset: the streak returns to zero at any counted trial that is not accepted, at any unplanned workflow repair, and at any change to the role or routing recipe, release or evaluator.

S4.3's original wording:

> Complete 20 consecutive accepted trials without unplanned workflow
> repair. Normal review/remediation is allowed; reconstructing grants is not.

S4.4's regression requirement:

> A failed trial becomes a
> reproducible regression before restarting the acceptance streak.

### Gate 2 — 72 hours / 30 distinct tasks

Sources: [docs/adr/0001-autonomous-execution-and-qualification.md, §3, Duration, Recovery Coverage, Integrity Criteria, Operator Burden and Corrective Releases](adr/0001-autonomous-execution-and-qualification.md#3-72-hour-sustained-use-qualification-gate-q5);
[§5, 30 Ordinary-Work Tasks](adr/0001-autonomous-execution-and-qualification.md#5-workload-counting-stratified-coverage-and-shared-evidence-q7);
[docs/programme/ORIGINAL-OBLIGATIONS.md, O9](programme/ORIGINAL-OBLIGATIONS.md#o9--qualification-at-real-boundaries).

> Gate 2, kept separate: 72 continuous hours on one frozen release and configuration baseline with at least 30 distinct ordinary tasks across oh-my-pi and media-discovery covering multiple task families (ADR 0001 sections 3 and 5). A task instance counts once; retries, review fixes and resumptions stay part of that task. Accepted, failed, blocked and cancelled outcomes are reported distinctly and never as success. The run fails on any lost accepted record, duplicate effect, unauthorized admission, false acceptance or unplanned workflow repair (repairing grants, patching the runtime or manually reconstructing state); planned fault injection and correctly handled failed experiments do not fail it. A corrective release requires a new run and does not authorize starting one.

### Gate 3 — within-run / 100 execution attempts

Source: [docs/adr/0001-autonomous-execution-and-qualification.md, §5, 100 Research Execution Attempts and Stratified Coverage Requirements](adr/0001-autonomous-execution-and-qualification.md#5-workload-counting-stratified-coverage-and-shared-evidence-q7).

> Gate 3, kept separate: at least 100 actual experiment execution attempts within that run: distinct attempts executing declared experiments through applicable qualified backends. Generic research jobs (literature retrieval, synthesis, planning, evaluation) do not count. Genuine retries of an execution count; polling, duplicate delivery and reconciliation of the same execution do not. Queued-but-never-started jobs are recorded separately and do not count. Reaching 100 cannot compensate for a missing preregistered stratum (repository, task family, independent campaign, required recovery or cancellation scenario).

### Shared evidence, outcomes and retention — all gates

Sources: [docs/adr/0001-autonomous-execution-and-qualification.md, §5, Stratified Coverage Requirements and Shared Raw Evidence Rules](adr/0001-autonomous-execution-and-qualification.md#5-workload-counting-stratified-coverage-and-shared-evidence-q7);
[docs/programme/ORIGINAL-OBLIGATIONS.md, O9](programme/ORIGINAL-OBLIGATIONS.md#o9--qualification-at-real-boundaries);
[docs/programme/SCOPE-PRESERVATION.md, Earlier settled qualification decisions](programme/SCOPE-PRESERVATION.md#earlier-settled-qualification-decisions).

> Shared evidence: raw execution evidence counts toward both gate 1 and gate 2 only when release, configuration, evaluator, workload conditions and acceptance criteria match exactly; it is shown as one observation with both uses named, never two independent ones. Negative science, candidate debugging, infrastructure failure and planned recovery are tracked as separate categories (O9). All failures and retries are kept with their full resource consumption; an unsuccessful case is never swapped for an easier task after a run starts.

## Denominators and reporting

These reporting definitions implement the quoted rules; they confer no authority.

| Gate | Denominator | Required reporting |
| --- | --- | --- |
| 20 consecutive accepted | Ordered ordinary task instances in the frozen role/routing recipe, release and evaluator scope, counted once per task using acceptance-bound outcomes. | Current consecutive accepted streak, every accepted/failed/blocked/cancelled outcome, every reset and its cause, regression evidence before restart. Retries, review fixes and resumptions do not become new tasks. |
| 72 hours / 30 distinct tasks | Distinct preregistered ordinary task instances within one continuous frozen-release/configuration interval; elapsed interval and task count are separate measurements. | Duration, distinct task ids, repository/task-family coverage, separate accepted/failed/blocked/cancelled totals, every integrity failure or unplanned repair, and planned fault/recovery records. Incomplete outcomes never count as success. |
| Within run / 100 execution attempts | Distinct actually started declared-experiment executions through applicable qualified backends within the same run. Genuine retries count by distinct execution identity. | Contributing execution ids, outcomes, retry links, excluded generic jobs/polling/duplicate deliveries/reconciliations, queued-never-started records, backend proof and coverage by preregistered stratum. |

Any role/routing recipe, release or evaluator change ends the frozen streak/run
scope. Configuration, workload or acceptance-criteria changes also end exact
protocol matching. A new scope cannot inherit prior counts. Shared raw evidence
must also match the frozen role/routing recipe; no fuzzy identity, latest release
or implicit inheritance. For every gate, retain the four separate categories quoted
above and full resources, including supporting activity. Keep measured, estimated,
cash, human and unknown resource totals distinct.

## Preregistration and report contract

Freeze minimum allocations by repository, task family, independent campaign and
required recovery/cancellation scenario before qualification. CPU/GPU coverage is
limited to exercised qualified backends; unavailable GPU stays unqualified. Reaching
100 attempts cannot replace a missing stratum. Numeric allocations and responsiveness
absolute/slowdown targets come only from existing preregistration route (ADR §6).
Freeze matched measured baseline, measurement methods, permitted recovery windows
and limits; report p50/p95/max/sample counts and provider wait separately.

Per gate report: status, count (null when not started), denominator definition,
protocol/proof ids and hashes, frozen scope identities, ordered contributing record
ids, every excluded record with reason, task/attempt deduplication ids, separate
outcome totals, queued-never-started records, streak resets/scope endings, run interval
and elapsed time, named shared-evidence uses, stratum allocations and observed coverage,
retained failures/resources, baseline/latency evidence and missing proof. Missing
freeze or release baseline means **not started**, count **null**, never zero. Missing
strata means **not preregistered**, allocations absent, never invented defaults.

## Exact current read-side mapping

`omp_work.qualification_records.decode_evidence` consumes existing service-view
receipt and observation mappings and committed `complete_work.payload.evidence`
(`CompletionEvidence`) supplied by a trusted read adapter. It neither executes the
request nor establishes that arbitrary caller-supplied JSON was committed acceptance.
Every projection is immutable, including canonical JSON copies of payloads.

| Existing field | Read projection / limit |
| --- | --- |
| EvidenceReceipt.receipt_id, payload, payload_sha256 | Receipt identity and canonical payload; recompute canonical SHA-256. Invalid payload/hash is excluded. Duplicate ids are ambiguous and excluded. |
| CompletionEvidence.subject | Work, revision, candidate id, candidate digest and commit; must match every linked receipt exactly. This identifies task/candidate acceptance evidence, not frozen release. |
| CompletionEvidence.artifacts | Resolve each receipt id; kind, payload hash and artifact hash must match. Dangling or mismatched links exclude acceptance evidence. Delivery candidate/remote commits must match subject. Bare PASS is insufficient. |
| ResearchObservation.observation_id, payload, payload_sha256 | Observation identity, immutable payload and verified payload hash. This is evidence availability, not accepted outcome or qualified execution. |
| ResearchObservation.observed_at, source_ref, trial_id, execution_status | Preserve timestamp, source, trial link and status. Timestamp alone supplies neither task order nor run interval; status alone proves neither actual execution nor qualified backend. |
| Managed observation payload.receipt_sha256, verdict, metrics | Existing managed verifier binds these to custody TrialReceipt bytes and trial/job/candidate/evaluator/protocol expectation. The qualification decoder retains them as payload data; it does not replace managed verification. |
| TrialReceipt.protocol_sha256 / campaign protocol | Research evaluation protocol only; never O9 freeze. |

No existing typed payload defines acceptance-bound O9 frozen protocol, release baseline,
configuration/recipe/evaluator/workload/criteria identities, ordered task instances,
run start/end, preregistered allocations or applicable qualified-backend proof.
Their exact current mapping is **unavailable**. Unknown payload keys named `freeze`,
`release` or `allocations` are not an authoritative mapping. Current release metadata
and candidate commit cannot fill these gaps. Internal `FrozenProtocol` describes a
future read projection, not a serialized protocol or invented writer convention.
Its proof must eventually resolve accepted records and hash-bound artifacts covering
all identities, order, interval, allocations and backend scope before any evaluator
can count. No public schema is changed here.

The shared `scope_boundary` compares all six frozen identities exactly for future
streak/run evaluators and shared-evidence reuse. Current `unavailable_report` exposes
missing proof for all three gates. Valid existing acceptance evidence still cannot
enable counting while O9 freeze and baseline remain unavailable.
