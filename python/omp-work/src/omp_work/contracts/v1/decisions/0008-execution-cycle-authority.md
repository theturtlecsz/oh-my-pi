# 0008 — One-command autonomous delivery cycle authority (OMP-180, owner-approved plan 2026-08-28)

## Problem

Delivery requires repeated owner relay through `/plan` → `/summary` → remediation → `/summary` → `/done`. When the owner desires fully autonomous execution of a single backlog item or a finite same-project queue snapshot, no single owner authorization exists that can govern planning, criteria sealing, execution, remote push, native audit, and atomic completion within bounded budgets and fail-closed safety constraints.

## Decision

1. **Ledger-owned execution grants.** Autonomous execution is governed by `omp_work.execution_grants` and `omp_work.execution_grant_items` tables rather than transcript booleans. A grant binds an immutable literal-owner authorization hash, workspace/owner/repository identity, mode (`single | queue`), sealed judge identity (`judge_sha256`), seven-day expiry, and fixed limits: `max_continuations=8`, `max_close_attempts=5`, `max_no_progress=3`.
2. **Owner-only scope `work.execute` and commands.** WorkService exposes six commands under scope `work.execute`:
   - `begin_execution`: Consumes once a host-only provenance envelope, verifies judge fingerprint, checks clean Git baseline, locks focus, and records grant with ordered claims.
   - `activate_execution_item`: Revalidates grant version, verifies clean Git baseline, sets focus to the active item, and advances phase to `criteria_pending`.
   - `seal_execution_criteria`: CAS-validates original description digest, creates a new immutable revision with structured criteria without modifying description, and advances phase to `planning`.
   - `stamp_execution_plan`: Allocates planned candidate, writes plan evidence receipt with plan stamp, and advances phase to `executing`.
   - `set_execution_state`: Transitions grant between `active`, `paused`, `stopped`, `canceled`, incrementing grant version and cascading terminal states to grant items.
   - `complete_execution_item`: Requires verified push receipt and PASS audit receipt on matching active grant; mints closeout receipt, completes work item to `DONE`, completes grant item, and completes grant when all items are done.
3. **Immutable original request as audit yardstick.** Execution audit manifests (`manifest_version = 3`) include an `Original request` section carrying the original description verbatim as indented data. The plan's verification list never serves as fallback criteria for execution grants.
4. **No-progress and close attempt bounding.** For NEEDS_FIX audit settlements on execution grants, the service compares candidate tree SHA and canonical findings hash against prior reviewed baselines: if either is unchanged, consecutive no-progress increments; if both change, it resets. The third consecutive no-progress result or fifth close attempt immediately stops the grant.
5. **Sealed judge TCB.** The audit judge is sealed as SHA-256 over canonical JSON of the auditor agent definition, workflow extension sources, executor transport, contract digest, and service runtime fingerprint (`code_fingerprint` + `migration_set_sha256`). Mismatches return `execution_judge_drift` without modifying the grant.
6. **Contract-changing safety.** Candidates that alter the Work contract pause execution before freeze/push/audit, requiring external interactive owner approval of the exact prospective contract digest before resuming.

## Consequences

- The contract digest changes; owner approval under issue `OMP-180` is required before deployment.
- `/execute` becomes a first-class workflow mapping for autonomous delivery cycles while existing manual `/plan`, `/summary`, and `/done` paths remain intact.
