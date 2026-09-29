# ADR 0005: Mission-first architecture

**Status:** Accepted; enforcement not built
**Date:** 2026-09-28

## Decision

### 1. Mission API
Callers submit what to accomplish as a structured mission, not command sequences.

### 2. Control plane validates LLM proposals
LLM reasoning proposes, deterministic software validates and authorizes, Run Owner stays a role and is not a persistent LLM agent.

### 3. Commands become internal operations
`/intake`, `/plan`, `/execute`, `/summary`, and `/done` stay for debugging, development and expert control, and no normal-use step needs them.

## Supersedes

- `## Intake routing (HOME-43, 2026-08-11)` (reconciliation C1)
- `## Routine ledger self-confirmation (OMP-23, owner ruling 2026-08-19)` (reconciliation C2)
- `## Close asymmetry (owner ruling, 2026-08-22)` (part 2, reconciliation C4)
- `## Autonomous execution authority (/execute, owner ruling 2026-08-28)` (reconciliation C6)
- The Question format rule (D26) and HOME-131's "no automatic machinery" clause (D27), both already absent from main (OMP-443, f5840f0b5e)
- D16's approval classes (superseded by D35, per D40)
- D23's every-mission confirmation and D25's no-cancel wording (refined by D29)

## Mission approval rules (D29)

> New or materially changed mission scope requires Chris's confirmation. Routine work within an already approved mission or standing project mandate should not require repeated ceremonial approval. OMP may autonomously pause, retry, reroute and reschedule work according to policy. OMP may not permanently abandon, cancel or materially redefine an approved mission without an explicit policy basis or Chris's approval.

| Rule | Lands in |
| --- | --- |
| New or materially changed mission scope requires Chris's confirmation. | OMP-426 |
| Routine work within an already approved mission or standing project mandate should not require repeated ceremonial approval. | OMP-413 / OMP-418 |
| OMP may autonomously pause, retry, reroute and reschedule work according to policy. | OMP-413 / OMP-417 |
| OMP may autonomously pause, retry, reroute and reschedule work according to policy. | OMP-420 / OMP-417 |
| OMP may not permanently abandon, cancel or materially redefine an approved mission without an explicit policy basis or Chris's approval. | OMP-414 |

## Contract versions (D30)

Chris approves the contract versions of OMP-405 (stop control) and OMP-411 to OMP-426 (mandate); other items keep flood's procedure; OMP-403 and OMP-429 to OMP-431 are Chris's too (OMP-412).

## Run Owner invariants

From MANDATE-VALIDATION.md (2026-09-28). ADR 0004 is unchanged (D28). Under A10, OMP-421 builds these checks before the OMP-417 orchestrator; they are not built yet.

| Invariant | Control-plane check | Lands in | Test |
| --- | --- | --- | --- |
| Single mutation authority | Only WorkService writes; workers hold no write scope and return proposals | OMP-402, OMP-403, OMP-421 | A worker's write command is refused |
| Admission control | Needs approved mission scope, effort field, budget reservation, free capacity | OMP-417, OMP-420, OMP-421 | A job missing scope, effort or reservation is refused |
| Budget policy | Reservation before dispatch; usage per job; threshold event; overrun pauses and raises a decision | OMP-404, OMP-413, OMP-417 | Dispatch without reservation refused; overrun pauses |
| Worker lifecycle | Register, lease, renew, fence; after reclaim read state, re-dispatch once | OMP-400, OMP-417 | Fenced write refused; reclaimed job resumes once |
| Acceptance semantics | Sealed criteria, evidence per criterion, independent reviewer PASS | OMP-417, OMP-420, OMP-421 | Close without PASS or with self-review refused |
| Authoritative state | WorkService only; sessions and pending files are caches | OMP-413, OMP-417 | Restart test reads only WorkService |
| Contract freeze | Mission and stop-control contract versions need Chris's approval (D30) | OMP-405, OMP-413 to OMP-416 | Version without approval record does not activate |

## Unattended safety envelope (D35)

> I would not make "unattended" synonymous with "fully autonomous." The right model is unattended operation inside a pre-approved safety envelope.

### Tier 1: autonomous

- read repository/project state
- create isolated worktrees
- modify files inside the approved repository/path envelope
- run tests, linters, builds, static analysis
- create commits on isolated branches
- perform research
- create/update internal artifacts
- retry/recover workers
- reroute models/providers
- pause/resume work
- update mission state
- emit events
- produce candidate changes for review

### Tier 2: standing policy

- push branches to approved repositories
- create pull requests
- update non-production external systems
- spend beyond a defined mission/project budget threshold
- perform bounded network access required by the mission
- create or delete disposable cloud/dev resources

### Tier 3: explicit high-risk authorization

- merge to protected/default branches
- production deployment
- destructive infrastructure changes
- credential/security-policy changes
- deleting persistent data
- modifying billing/payment/account ownership
- publishing externally as you
- broadening repository/project scope
- accessing secrets outside the approved mission envelope
- disabling safety, audit, or verification mechanisms

D40: D35 supersedes D16's classes where they conflict; an action no tier lists is tier 3.
