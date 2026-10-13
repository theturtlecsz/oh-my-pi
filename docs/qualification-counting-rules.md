# Qualification counting rules (OMP-620 r1)

Authority: [ADR 0001 §§3,5,6](adr/0001-autonomous-execution-and-qualification.md),
[stabilization S4.3–S4.4](omp-stabilization-plan.md#phase-4--run-ordinary-work-omp-249-qualification),
[ORIGINAL-OBLIGATIONS O9](programme/ORIGINAL-OBLIGATIONS.md#o9--qualification-at-real-boundaries),
and [SCOPE-PRESERVATION](programme/SCOPE-PRESERVATION.md).
Counts grant no qualification authority and cannot prove full O9 acceptance.
Real boundaries, negative cases, independent audits, native WorkService acceptance,
responsiveness, integrity, resource accounting and coverage remain mandatory.
This item starts no trial, run or freeze; introduces no persistence, command,
public schema or approval route. A read-only counter is separate work.

## Three separate gates

| Gate | Denominator and contributing evidence | Exclusions and reporting |
| --- | --- | --- |
| 20 consecutive accepted | Ordered ordinary task instances under one frozen recipe, release, configuration and evaluator. Count each task once, using its final acceptance-bound outcome. | Retries, review fixes and resumptions stay within their original task. Every intervening counted failed, blocked or cancelled trial resets streak to zero; never select successes around failures. Report current streak, all task outcomes and reset records. Unplanned workflow repair invalidates streak. |
| 72 hours / 30 distinct tasks | One continuous frozen-release mixed-workload interval of at least 72 hours; at least 30 distinct preregistered ordinary task instances spanning oh-my-pi and media-discovery and frozen task families. | Report distinct tasks and accepted, failed, blocked, cancelled separately; incomplete is never success. Retries, remediation and resumptions cannot inflate denominator. Duration or total tasks alone cannot establish a passing run. Report gaps, contention, recovery/cancellation coverage and integrity/repair failures. |
| Within run / 100 execution attempts | Distinct started attempts executing declared experiments through applicable qualified backends, within that same frozen run. Genuine retries have distinct execution identities and count. | Generic research, retrieval, planning, synthesis, evaluation-only work, polling, duplicate delivery and reconciliation do not count. Queued-never-started jobs reported separately. Report attempt outcomes separately; negative science, candidate debugging, infrastructure failure and planned recovery remain distinct. |

A change to frozen recipe, release or evaluator ends streak/run scope. Configuration,
workload or acceptance-criteria changes also invalidate exact protocol matching.
A corrective release requires a new run through existing authority, not automatic
restart. Preserve all failures, retries, their evidence and full resource consumption;
never swap unsuccessful tasks for easier ones. Keep measured, estimated, cash,
human and unknown resource totals distinct, including supporting activity.

Shared raw evidence between the run and streak is one observation with both uses
named. Release, configuration, recipe, evaluator, workload conditions and acceptance
criteria must match exactly. No fuzzy identity, latest release or implicit inheritance.
Shared evidence never represents two independent observations.

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
