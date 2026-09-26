# ADR 0002: Shared Authority Foundation, Unified Execution Primitives, and Scoped Autoresearch Governance

## Status
Accepted (Architecture and Governance Design Record; No Live Execution Grant Approval or Operational Cutover Authorized)

## Context
Oh My Pi (OMP) executes routine software engineering and autonomous scientific research across `oh-my-pi` and `media-discovery`. Autonomous research workflows—literature synthesis, ML optimization, harness exploration, executable search mechanisms, and recursive meta-research—require significant execution latitude. Without unified authority, research risks authority fragmentation, state drift, memory contamination, and runaway resource consumption. Earlier qualification governance, operating recipes, and the 72-hour sustained-use qualification gate were established in ADR 0001 (`0001-autonomous-execution-and-qualification.md`). Owner decisions Q1–Q35 established product and delivery sequencing.

## Decision
1. **Unified Authority and Shared Execution**: All engineering, ECC asset installation, and research campaigns operate under `WorkService` as the sole authority for native scope, identity, grants, cancellation, evidence, and completion. Native `/intake`, `/plan`, `/execute`, `/summary`, and `/done` ownership is preserved through existing interfaces. Shared execution foundations (sandboxes, leases, idempotency outboxes, cancellation fencing, worker pools) serve both workloads. Shared authority mitigates split-brain states and runaway processes, though physical termination still requires backend verification and bounded observation windows.
2. **Decoupled Roles and Evaluator Integrity**: Candidate generation, selection, and acceptance are strictly separated. Candidates cannot modify acceptance criteria or access private evaluator credentials, though appropriate read-only criteria access is permitted. Evaluators independently issue immutable, content-addressed receipts.
3. **Scoped Learning and Dedicated Recursive Budgets**: General engineering lessons transfer by default subject to permissions; domain knowledge and procedures require demonstrated applicability. Corrections trigger consequential rechecks within existing authority or are proposed to the owner. Recursive meta-research receives a dedicated, capped reservation requiring measured downstream task benefit, preventing silent quota borrowing.
4. **Lifecycle Promotion**: Synthesized mechanisms and harness adaptations must follow the full promotion lifecycle defined in the research plan rather than an ad-hoc ladder, becoming active only through qualified, immutable release packages.

## Rationale
Unifying authority under `WorkService` provides consistent lifecycle and cancellation semantics across tasks. While shared queues cannot guarantee zero contention or that research never impacts latency, campaign reservations and concurrency controls bound resource competition. Requiring downstream verification makes measured benefit a condition for adopting meta-research results.

## Governance Invariants
This record documents governance and architectural rationale; it does not approve live execution cutovers or authorize Q36.
