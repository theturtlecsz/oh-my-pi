# ADR 0006: On-demand roles

**Status:** Accepted; target forms not built
**Date:** 2026-09-28

## Context

Owner decision D28 keeps ADR 0004 in force. Mandate section 13 ("Fewer persistent agent roles") settles the shape of the standing roles: Programme Design, Loop Auditor, Status Desk, Deep Research and other standing roles become a capability, evaluator, scheduled job, temporary worker, report or query unless persistence has a concrete architectural reason. Roles are not long-lived agents by default. Each role below names the target form to build and whether an LLM agent persists for it.

## Decision

### 1. Ephemeral by default

No role below runs as a standing LLM agent. A role persists only when a recorded reason names the concrete architectural reason persistence buys; absent that record the role is a capability, evaluator, scheduled job, temporary worker, report or query that the control plane starts on demand and ends when the work ends.

### 2. ADR 0004 unchanged

[ADR 0004](0004-hard-padlock-control-plane.md) stands. Its sole-mutator rule — Run Owner holds the mutate grants and exercises them only through WorkService — is unchanged. This ADR records persistence for the roles below; it does not grant or move mutation rights.

## Role target forms

| Role | Target form | Standing LLM agent | Invariant kept | Reason |
| --- | --- | --- | --- | --- |
| Programme Design | Temporary worker: the lifecycle's plan stage | No | Blueprint-only: its output is a plan proposal the control plane stamps | Plans are per mission; nothing needs to persist between them |
| Status Desk | Query (`project.get_status`, `mission.status`) plus report (daily digest, OMP-406) | No | Presents native state only: derived from ledger records (D6) | A query cannot drift from the ledger |
| Loop Auditor | Scheduled job running an evaluator over recent missions, filing findings | No | Read-only: files findings, never mutates | A periodic check needs no standing identity |
| Deep Research | Capability (`research.run`) executed by temporary workers under an admitted campaign | No | Cannot start a campaign alone: admission via WorkService | Research runs per campaign |
| Deterministic supervisor | Service: the mission orchestrator (software, OMP-417) | No | One coherent assignment per cycle; failures preserved unchanged; no model decides stage order or coordinates workers (D33) | A process must claim jobs, but its state is durable, so it is a restartable service, not an agent |
| Native reviewer (`@audit`) | Evaluator, spawned per candidate | No | Maker/checker separation, checked by the control plane's acceptance_semantics check (OMP-421); no new model-family rule | Review is per change |
| Persistent planners | Temporary worker (same as Programme Design) | No | Plan stamped before execution | Plans are per mission; nothing needs to persist between them |

D33 (2026-09-28) supersedes the reconciliation tab's model for supervisor coordination judgment.
