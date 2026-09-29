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
